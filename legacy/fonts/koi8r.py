"""
fonts/koi8_r.py — таблицы и утилиты для работы с кодировкой КОИ-8Р,
использовавшейся в терминале «Электроника МС 7401».

Хотя Python имеет встроенный кодек `koi8-r`, этот модуль предоставляет:

    - явные таблицы соответствия байт ↔ символы (для документации);
    - быстрые множества байтов для классификации (верхний регистр,
      нижний регистр, знаки препинания, псевдографика);
    - спецкоды терминала (ВК, S0, S1, END, LF) и их имена;
    - предикаты «это русская буква / латинская / цифра / спецкод»;
    - декодирование одиночных байт и последовательностей с учётом шрифта;
    - эвристику определения шрифта по содержимому.

Ключевая особенность КОИ-8Р: буквы расположены «вразнобой» относительно
их алфавитного порядка, поэтому одной проверкой диапазона не обойтись —
нужны явные множества байтов.

Пример:
    from fonts.koi8_r import decode_byte, is_russian_letter, S0

    is_russian_letter(0xC1)      # True — это 'а'
    decode_byte(0xE1, "RU")      # 'А'
    decode_byte(0x41, "EN")      # 'A'

Автор: <ваше имя>
Лицензия: MIT
"""

from __future__ import annotations

import logging
import unicodedata
from typing import Iterable, Optional

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Основные константы
# ─────────────────────────────────────────────────────────────────────────────

# Имя кодека в терминах Python.
KOI8_R = "koi8-r"

# Имя альтернативного кодека (не используется, но оставлено для справки).
KOI8_U = "koi8-u"


# ─────────────────────────────────────────────────────────────────────────────
# Спецкоды терминала «Электроника МС 7401»
# ─────────────────────────────────────────────────────────────────────────────
#
# Это управляющие байты, которые встречаются в потоке команд и внутри
# текстовых строк. Они НЕ являются обычными символами и обрабатываются
# отдельно reader'ом и parser'ом.

VK  = 0o125   # ВК — конец команды (85 / 'U')
S0  = 0o160   # S0 — переключение на русский шрифт (112)
S1  = 0o161   # S1 — переключение на латинский шрифт (113)
LF  = 0o175   # LF — смещение окна ('}')
END = 0o377   # END — конец текста (255)

# Стандартные ASCII-управляющие (для удобства).
NUL = 0o000
BEL = 0o007
BS  = 0o010
TAB = 0o011
NL  = 0o012   # \n
VT  = 0o013
FF  = 0o014
CR  = 0o015   # \r
ESC = 0o033
SP  = 0o040   # пробел

# Отображение спецкодов в человекочитаемые имена.
SPETSCODES: dict[int, str] = {
    VK:  "VK",
    S0:  "S0",
    S1:  "S1",
    LF:  "LF",
    END: "END",
    ESC: "ESC",
    CR:  "CR",
    NL:  "NL",
    SP:  "SP",
    NUL: "NUL",
}


# ─────────────────────────────────────────────────────────────────────────────
# Диапазоны КОИ-8Р (для документации и быстрых проверок)
# ─────────────────────────────────────────────────────────────────────────────
#
# В КОИ-8Р:
#   0x00–0x7F — стандартный ASCII;
#   0x80–0xBF — псевдографика, знаки, некоторые кириллические буквы
#               (ё, Ё, украинские і, ї, є);
#   0xC0–0xDF — строчные русские буквы (в «перепутанном» порядке);
#   0xE0–0xFF — прописные русские буквы (в том же порядке).

CYRILLIC_RANGE       = (0xC0, 0xFF)
CYRILLIC_LOWER_RANGE = (0xC0, 0xDF)
CYRILLIC_UPPER_RANGE = (0xE0, 0xFF)

# Специальный случай из graf.pas: помимо обычных букв, в РУСЛИТ
# включён байт 0o135 (93 = ']' в ASCII). Это, по-видимому, «ё» или
# похожий символ в конкретной реализации терминала.
SPECIAL_CYRILLIC_BYTES = frozenset({0o135})


# ─────────────────────────────────────────────────────────────────────────────
# Построение таблиц классификации байтов
# ─────────────────────────────────────────────────────────────────────────────

def _classify_byte(b: int) -> str:
    """
    Определить категорию байта b в КОИ-8Р.

    Возвращает одну из строк:
        'ascii-control', 'ascii-digit', 'ascii-upper', 'ascii-lower',
        'ascii-punct', 'ascii-other',
        'cyrillic-upper', 'cyrillic-lower', 'cyrillic-symbol',
        'symbol', 'box', 'unknown'.
    """
    if b < 0x80:
        if b < 0x20 or b == 0x7F:
            return "ascii-control"
        if 0x30 <= b <= 0x39:
            return "ascii-digit"
        if 0x41 <= b <= 0x5A:
            return "ascii-upper"
        if 0x61 <= b <= 0x7A:
            return "ascii-lower"
        if 0x21 <= b <= 0x7E:
            return "ascii-punct"
        return "ascii-other"

    # Верхняя половина.
    try:
        ch = bytes([b]).decode(KOI8_R)
    except UnicodeDecodeError:
        return "unknown"

    name = unicodedata.name(ch, "")
    if "CYRILLIC" in name:
        if ch.isupper():
            return "cyrillic-upper"
        if ch.islower():
            return "cyrillic-lower"
        return "cyrillic-symbol"

    category = unicodedata.category(ch)
    if category.startswith("L"):
        # Прочие буквы (например, украинские вне CYRILLIC-блока).
        return "cyrillic-symbol"
    if category.startswith("S"):  # symbols
        return "symbol"
    if category in ("Po", "Pd", "Ps", "Pe", "Pi", "Pf"):
        return "ascii-punct"
    if name.startswith("BOX DRAWINGS"):
        return "box"
    return "unknown"


def _build_byte_sets() -> dict[str, frozenset[int]]:
    """Собрать множества байтов по категориям."""
    buckets: dict[str, set[int]] = {
        "ascii-control": set(),
        "ascii-digit": set(),
        "ascii-upper": set(),
        "ascii-lower": set(),
        "ascii-punct": set(),
        "ascii-other": set(),
        "cyrillic-upper": set(),
        "cyrillic-lower": set(),
        "cyrillic-symbol": set(),
        "symbol": set(),
        "box": set(),
        "unknown": set(),
    }
    for b in range(256):
        buckets[_classify_byte(b)].add(b)
    return {k: frozenset(v) for k, v in buckets.items()}


_BYTE_SETS = _build_byte_sets()

# Явные множества — публичный API.
CYRILLIC_UPPER_BYTES = _BYTE_SETS["cyrillic-upper"]
CYRILLIC_LOWER_BYTES = _BYTE_SETS["cyrillic-lower"]
CYRILLIC_SYMBOL_BYTES = _BYTE_SETS["cyrillic-symbol"]
CYRILLIC_BYTES = (
    CYRILLIC_UPPER_BYTES
    | CYRILLIC_LOWER_BYTES
    | CYRILLIC_SYMBOL_BYTES
)

LATIN_UPPER_BYTES = _BYTE_SETS["ascii-upper"]
LATIN_LOWER_BYTES = _BYTE_SETS["ascii-lower"]
LATIN_BYTES = LATIN_UPPER_BYTES | LATIN_LOWER_BYTES

DIGIT_BYTES = _BYTE_SETS["ascii-digit"]
PUNCTUATION_BYTES = (
    _BYTE_SETS["ascii-punct"]
    | _BYTE_SETS["cyrillic-symbol"]
    | _BYTE_SETS["symbol"]
)
BOX_DRAWING_BYTES = _BYTE_SETS["box"]

CONTROL_BYTES = (
    _BYTE_SETS["ascii-control"]
    | frozenset({VK, S0, S1, LF, END})
)


# ─────────────────────────────────────────────────────────────────────────────
# Предикаты
# ─────────────────────────────────────────────────────────────────────────────

def is_spetscode(b: int) -> bool:
    """Является ли байт спецкодом терминала (ВК, S0, S1, LF, END)."""
    return b in (VK, S0, S1, LF, END)


def is_control(b: int) -> bool:
    """Управляющий байт (ASCII-управление или спецкод)."""
    return b in CONTROL_BYTES


def is_russian_upper(b: int) -> bool:
    """Прописная русская буква (А..Я, Ё, украинские)?"""
    return b in CYRILLIC_UPPER_BYTES or b == 0o135  # 0o135 — спец. случай graf.pas


def is_russian_lower(b: int) -> bool:
    """Строчная русская буква (а..я, ё, украинские)?"""
    return b in CYRILLIC_LOWER_BYTES


def is_russian_letter(b: int) -> bool:
    """Любая русская буква (прописная, строчная или спец. случай)?"""
    return (
        b in CYRILLIC_BYTES
        or b in SPECIAL_CYRILLIC_BYTES
    )


def is_latin_upper(b: int) -> bool:
    """Прописная латинская буква (A..Z)?"""
    return b in LATIN_UPPER_BYTES


def is_latin_lower(b: int) -> bool:
    """Строчная латинская буква (a..z)?"""
    return b in LATIN_LOWER_BYTES


def is_latin_letter(b: int) -> bool:
    """Любая латинская буква?"""
    return b in LATIN_BYTES


def is_digit(b: int) -> bool:
    """Цифра 0..9?"""
    return b in DIGIT_BYTES


def is_punctuation(b: int) -> bool:
    """Знак препинания или символ?"""
    return b in PUNCTUATION_BYTES


def is_box_drawing(b: int) -> bool:
    """Символ псевдографики (рамки, стрелки)?"""
    return b in BOX_DRAWING_BYTES


def is_printable(b: int) -> bool:
    """Печатаемый символ (не управляющий и не спецкод)?"""
    return not is_control(b)


def has_cyrillic(data: bytes) -> bool:
    """Содержит ли последовательность хотя бы одну русскую букву?"""
    return any(is_russian_letter(b) for b in data)


def has_latin(data: bytes) -> bool:
    """Содержит ли последовательность хотя бы одну латинскую букву?"""
    return any(is_latin_letter(b) for b in data)


# ─────────────────────────────────────────────────────────────────────────────
# Декодирование
# ─────────────────────────────────────────────────────────────────────────────

def decode_byte(b: int, font: str = "EN") -> str:
    """
    Декодировать один байт в символ.

    Параметры:
        b    — значение байта (0..255);
        font — 'EN' (ASCII/latin-1) или 'RU' (koi8-r).

    Для шрифта 'EN' используется latin-1, чтобы не терять байты
    0x80..0xFF (они сохранятся как U+0080..U+00FF).
    """
    if not 0 <= b <= 0xFF:
        raise ValueError(f"Недопустимый байт: {b}")
    if font == "RU":
        return bytes([b]).decode(KOI8_R, errors="replace")
    return bytes([b]).decode("latin-1", errors="replace")


def decode_bytes(data: bytes, font: str = "EN") -> str:
    """Декодировать последовательность байтов с одним шрифтом."""
    if font == "RU":
        return data.decode(KOI8_R, errors="replace")
    return data.decode("latin-1", errors="replace")


def decode_with_switching(
    data: bytes,
    *,
    initial_font: str = "EN",
    auto_switch: bool = True,
) -> str:
    """
    Декодировать текст с учётом спецкодов S0/S1.

    Параметры:
        initial_font — начальный шрифт ('EN' или 'RU');
        auto_switch  — если True, реагировать на S0/S1;
                       если False, шрифт не меняется.

    Возвращает строку Unicode.
    """
    font = initial_font
    out: list[str] = []
    for b in data:
        if b == END:
            break
        if auto_switch and b == S0:
            font = "RU"
            continue
        if auto_switch and b == S1:
            font = "EN"
            continue
        out.append(decode_byte(b, font))
    return "".join(out)


def encode_char(ch: str, font: str = "RU") -> int:
    """
    Закодировать символ в байт.

    Для шрифта 'RU' используется koi8-r; если символ не входит
    в кодировку — возвращается код '?' (0x3F).
    Для 'EN' — latin-1.
    """
    enc = KOI8_R if font == "RU" else "latin-1"
    try:
        return ch.encode(enc)[0]
    except (UnicodeEncodeError, IndexError):
        return 0x3F  # '?'


def encode_string(text: str, font: str = "RU") -> bytes:
    """Закодировать строку в байты указанным шрифтом."""
    enc = KOI8_R if font == "RU" else "latin-1"
    return text.encode(enc, errors="replace")


# ─────────────────────────────────────────────────────────────────────────────
# Эвристика определения шрифта
# ─────────────────────────────────────────────────────────────────────────────

def detect_font(
    raw: bytes,
    *,
    threshold: float = 0.3,
) -> str:
    """
    Определить доминирующий шрифт по содержимому.

    Возвращает 'RU', если доля кириллицы среди «буквенных» байтов
    превышает threshold, иначе 'EN'.

    Параметры:
        threshold — доля кириллицы (0..1), при которой считаем,
                    что текст русский.
    """
    cyr = 0
    lat = 0
    for b in raw:
        if is_russian_letter(b):
            cyr += 1
        elif is_latin_letter(b):
            lat += 1
    total = cyr + lat
    if total == 0:
        return "EN"
    return "RU" if cyr / total > threshold else "EN"


# ─────────────────────────────────────────────────────────────────────────────
# Экспорт таблиц для документации
# ─────────────────────────────────────────────────────────────────────────────

def byte_table() -> dict[int, str]:
    """
    Вернуть полную таблицу «байт → символ» для КОИ-8Р.

    Используется для документации и отладки. Для байтов 0x00..0x7F
    возвращается соответствующий ASCII-символ, для 0x80..0xFF —
    символ КОИ-8Р.
    """
    return {b: bytes([b]).decode(KOI8_R, errors="replace") for b in range(256)}


def dump_table(
    *,
    only_cyrillic: bool = False,
    show_names: bool = False,
) -> None:
    """
    Распечатать таблицу КОИ-8Р в человекочитаемом виде.

    Параметры:
        only_cyrillic — печатать только байты кириллицы;
        show_names    — добавлять Unicode-имена символов.
    """
    print("KOI-8R — таблица символов")
    print("=" * 72)

    rows = []
    for b in range(256):
        if only_cyrillic and not is_russian_letter(b):
            continue
        ch = bytes([b]).decode(KOI8_R, errors="replace")
        if b < 0x20 or b == 0x7F:
            display = f"\\x{b:02x}"
        elif b == 0x20:
            display = "·"  # визуализация пробела
        else:
            display = ch
        name = ""
        if show_names:
            name = unicodedata.name(ch, "")
        rows.append((b, display, name))

    for i in range(0, len(rows), 4):
        chunk = rows[i:i + 4]
        parts = []
        for b, display, name in chunk:
            if show_names:
                parts.append(f"{b:02X}:{display:2s} {name[:20]:20s}")
            else:
                parts.append(f"{b:02X}:{display:2s}")
        print("  ".join(parts))


def spetscode_name(b: int) -> Optional[str]:
    """Вернуть имя спецкода или None, если это обычный байт."""
    return SPETSCODES.get(b)


# ─────────────────────────────────────────────────────────────────────────────
# Самотест
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    print("Проверка предикатов:")
    tests = [
        (0x41, "is_latin_upper", is_latin_upper),      # 'A'
        (0x61, "is_latin_lower", is_latin_lower),      # 'a'
        (0x30, "is_digit",       is_digit),            # '0'
        (0xC1, "is_russian_lower", is_russian_lower),  # 'а'
        (0xE1, "is_russian_upper", is_russian_upper),  # 'А'
        (0xFF, "is_russian_upper", is_russian_upper),  # 'Ъ'
        (VK,   "is_spetscode",   is_spetscode),
        (S0,   "is_spetscode",   is_spetscode),
        (0x20, "is_punctuation", is_punctuation),
    ]
    for b, name, fn in tests:
        mark = "✓" if fn(b) else "✗"
        print(f"  {mark} {name}(0x{b:02X}) = {fn(b)}")

    print()
    print("Декодирование по байтам (RU):")
    for b in (0xC1, 0xC2, 0xC3, 0xE1, 0xE2, 0xE3, 0xFF):
        ch = decode_byte(b, "RU")
        print(f"  0x{b:02X} → {ch!r}")

    print()
    print("Декодирование с переключением шрифтов:")
    # AB<RU>ВГ<EN>DE
    data = b"AB" + bytes([S0]) + b"\xc1\xc2" + bytes([S1]) + b"DE"
    text = decode_with_switching(data)
    print(f"  {data!r}")
    print(f"  → {text!r}")

    print()
    print("Определение шрифта:")
    print(f"  русский поток:  {detect_font(b'\\xc1\\xc2\\xc3\\xc4')}")
    print(f"  латинский поток: {detect_font(b'ABCD')}")
    print(f"  смешанный:       {detect_font(b'AB\\xc1\\xc2CD')}")

    print()
    print("Спецкоды:")
    for b in (VK, S0, S1, LF, END, ESC, CR, NL, SP):
        name = spetscode_name(b)
        print(f"  0o{b:03o} (0x{b:02X}) = {name}")

    print()
    print("Таблица кириллицы (только буквы):")
    dump_table(only_cyrillic=True)

    print()
    print("Размеры множеств байтов:")
    for name, s in (
        ("CYRILLIC_UPPER_BYTES", CYRILLIC_UPPER_BYTES),
        ("CYRILLIC_LOWER_BYTES", CYRILLIC_LOWER_BYTES),
        ("CYRILLIC_BYTES",       CYRILLIC_BYTES),
        ("LATIN_UPPER_BYTES",    LATIN_UPPER_BYTES),
        ("LATIN_LOWER_BYTES",    LATIN_LOWER_BYTES),
        ("DIGIT_BYTES",          DIGIT_BYTES),
        ("PUNCTUATION_BYTES",    PUNCTUATION_BYTES),
        ("BOX_DRAWING_BYTES",    BOX_DRAWING_BYTES),
        ("CONTROL_BYTES",        CONTROL_BYTES),
    ):
        print(f"  {name:22s} — {len(s):3d} байт")