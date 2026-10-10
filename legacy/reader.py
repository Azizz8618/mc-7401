"""
reader.py — чтение потока команд терминала «Электроника МС 7401».

Поток — последовательность байт, в которой команды разделены символом
ВК (octal 125 = 85 = 'U'). Внутри команд могут встречаться спецкоды:

    ВК  = 0o125 — конец команды
    S0  = 0o160 — переключение на русский шрифт
    S1  = 0o161 — переключение на латинский шрифт
    END = 0o377 — конец текста
    LF  = 0o175 — смещение окна (в некоторых командах)

Модуль предоставляет:

    CommandStreamReader — разбивает поток на отдельные команды;
    split_by_font       — разбивает строку по шрифтовым сегментам;
    decode_text         — декодирует строку с учётом S0/S1;
    dump_commands       — отладочная печать команд.

Автор: <ваше имя>
Лицензия: MIT
"""

from __future__ import annotations

import logging
from typing import Iterator, Optional

# ─── Спецкоды ────────────────────────────────────────────────────────────────

VK      = 0o125   # конец команды (ВК)
S0      = 0o160   # русский шрифт
S1      = 0o161   # латинский шрифт
END     = 0o377   # конец текста
LF      = 0o175   # смещение окна

CR      = 0o015   # возврат каретки
NL      = 0o012   # перевод строки
SP      = 0o040   # пробел

DEFAULT_ENCODING_RU = "koi8-r"
DEFAULT_ENCODING_EN = "latin-1"

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# CommandStreamReader
# ─────────────────────────────────────────────────────────────────────────────

class CommandStreamReader:
    """
    Разбивает бинарный поток на отдельные команды, разделённые ВК.

    Каждая команда — последовательность байт до ВК (не включая ВК).
    Пустая команда (два ВК подряд) допустима и возвращается как b"".

    Пример:
        reader = CommandStreamReader(data)
        while True:
            cmd = reader.read_raw_command()
            if cmd is None:
                break
            ...
    """

    __slots__ = ("_data", "_pos", "_cmd_index", "_truncated")

    def __init__(self, data: bytes | bytearray):
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError(
                f"CommandStreamReader ожидал bytes, получено {type(data).__name__}"
            )
        self._data: bytes = bytes(data)
        self._pos: int = 0
        self._cmd_index: int = 0
        self._truncated: bool = False

    # ─── Свойства ────────────────────────────────────────────────────────────

    @property
    def size(self) -> int:
        """Размер входного потока в байтах."""
        return len(self._data)

    @property
    def position(self) -> int:
        """Текущая позиция в потоке."""
        return self._pos

    @property
    def remaining(self) -> int:
        """Сколько байт осталось до конца потока."""
        return len(self._data) - self._pos

    @property
    def command_index(self) -> int:
        """Номер последней прочитанной команды (считая с 1)."""
        return self._cmd_index

    @property
    def eof(self) -> bool:
        """Достигнут ли конец потока."""
        return self._pos >= len(self._data)

    @property
    def truncated(self) -> bool:
        """Был ли поток оборван без завершающего ВК."""
        return self._truncated

    # ─── Низкоуровневые операции ─────────────────────────────────────────────

    def _next_byte(self) -> Optional[int]:
        if self._pos >= len(self._data):
            return None
        b = self._data[self._pos]
        self._pos += 1
        return b

    def _peek_byte(self) -> Optional[int]:
        if self._pos >= len(self._data):
            return None
        return self._data[self._pos]

    # ─── Чтение команд ───────────────────────────────────────────────────────

    def read_raw_command(self) -> Optional[bytes]:
        """
        Прочитать одну команду — байты до ВК (не включая ВК).

        Возвращает:
            bytes    — сырые байты команды (может быть b"");
            None     — поток исчерпан.

        Если поток оборван без завершающего ВК, остаток возвращается как
        команда, а флаг `truncated` устанавливается в True.
        """
        if self.eof:
            return None

        start = self._pos
        while True:
            b = self._next_byte()
            if b is None:
                # конец потока без ВК
                if self._pos > start:
                    self._truncated = True
                    self._cmd_index += 1
                    cmd = self._data[start:self._pos]
                    log.warning(
                        "Команда #%d оборвана без ВК (последние %d байт)",
                        self._cmd_index, len(cmd),
                    )
                    return cmd
                return None

            if b == VK:
                self._cmd_index += 1
                # _pos уже за ВК, вычитаем 1
                return self._data[start:self._pos - 1]

    # Псевдоним для краткости
    read_command = read_raw_command

    def read_exact(self, n: int) -> bytes:
        """
        Прочитать ровно n байт (или меньше, если поток кончился).
        Используется для двоичного кодирования параметров.
        """
        if n <= 0:
            return b""
        end = min(self._pos + n, len(self._data))
        chunk = self._data[self._pos:end]
        self._pos = end
        return chunk

    def peek_byte(self) -> Optional[int]:
        """Заглянуть в следующий байт, не двигая указатель."""
        return self._peek_byte()

    def skip_byte(self) -> None:
        """Пропустить один байт."""
        self._next_byte()

    def skip_whitespace(self) -> None:
        """Пропустить пробелы, CR, LF и нулевые байты."""
        while True:
            b = self._peek_byte()
            if b is None or b not in (SP, CR, NL, 0):
                return
            self._pos += 1

    def seek(self, pos: int) -> None:
        """Перейти к позиции pos."""
        self._pos = max(0, min(pos, len(self._data)))

    def reset(self) -> None:
        """Сбросить указатель в начало."""
        self._pos = 0
        self._cmd_index = 0
        self._truncated = False

    # ─── Итерация ────────────────────────────────────────────────────────────

    def __iter__(self) -> Iterator[bytes]:
        """Итерировать по сырым командам."""
        while True:
            cmd = self.read_raw_command()
            if cmd is None:
                return
            yield cmd

    def __len__(self) -> int:
        """Общее число команд (требует прохода; кэшируется)."""
        # быстрый подсчёт по числу ВК
        return self._data.count(VK)

    def __repr__(self) -> str:
        return (
            f"CommandStreamReader(size={self.size}, "
            f"pos={self._pos}, cmd={self._cmd_index})"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Разбор спецкодов внутри команды
# ─────────────────────────────────────────────────────────────────────────────

def split_by_font(
    raw: bytes,
    *,
    initial_font: str = "EN",
) -> list[tuple[str, bytes]]:
    """
    Разбить сырую строку на сегменты по активному шрифту.

    Возвращает список пар (font, bytes), где font ∈ {"RU", "EN"}.
    Спецкоды S0/S1 не включаются в результат; END обрывает разбор.

    Пример:
        >>> split_by_font(b"abc" + bytes([S0]) + b"xyz")
        [('EN', b'abc'), ('RU', b'xyz')]
    """
    result: list[tuple[str, bytes]] = []
    font = initial_font
    buf = bytearray()

    for b in raw:
        if b == S0:
            if buf:
                result.append((font, bytes(buf)))
                buf.clear()
            font = "RU"
        elif b == S1:
            if buf:
                result.append((font, bytes(buf)))
                buf.clear()
            font = "EN"
        elif b == END:
            break
        else:
            buf.append(b)

    if buf:
        result.append((font, bytes(buf)))

    return result


def decode_text(
    raw: bytes,
    *,
    encoding_ru: str = DEFAULT_ENCODING_RU,
    encoding_en: str = DEFAULT_ENCODING_EN,
) -> str:
    """
    Декодировать текст с учётом переключения S0/S1.

    Русские сегменты декодируются в encoding_ru (по умолчанию koi8-r),
    латинские — в encoding_en (по умолчанию latin-1, чтобы не терять байты).
    """
    parts: list[str] = []
    for font, chunk in split_by_font(raw):
        enc = encoding_ru if font == "RU" else encoding_en
        parts.append(chunk.decode(enc, errors="replace"))
    return "".join(parts)


def has_special_codes(raw: bytes) -> bool:
    """Проверить, содержит ли строка спецкоды S0/S1/END."""
    return any(b in (S0, S1, END) for b in raw)


# ─────────────────────────────────────────────────────────────────────────────
# Отладочные утилиты
# ─────────────────────────────────────────────────────────────────────────────

def dump_commands(
    data: bytes,
    *,
    limit: int = 0,
    show_raw: bool = True,
    show_text: bool = True,
) -> None:
    """
    Распечатать команды в читаемом виде (для отладки).

    Параметры:
        data      — сырой поток;
        limit     — максимум команд (0 = без ограничения);
        show_raw  — печатать repr сырых байт;
        show_text — печатать декодированный текст.
    """
    reader = CommandStreamReader(data)
    n = 0
    for cmd in reader:
        n += 1
        parts = [f"#{n:04d}", f"[{len(cmd):3d}]"]

        if show_raw:
            # компактное представление: печатаем только printable ASCII,
            # спецкоды — в угловых скобках
            parts.append(_format_raw(cmd))

        if show_text:
            text = decode_text(cmd)
            parts.append(f"→ {text!r}")

        print("  ".join(parts))

        if limit and n >= limit:
            remaining = reader.size - reader.position
            print(f"... (показано {limit} команд; осталось ~{remaining} байт)")
            break

    if reader.truncated:
        print("!! ВНИМАНИЕ: поток оборван без завершающего ВК")


def _format_raw(raw: bytes) -> str:
    """Компактное представление сырых байт со спецкодами в <...>."""
    out: list[str] = []
    for b in raw:
        if b == S0:
            out.append("<S0>")
        elif b == S1:
            out.append("<S1>")
        elif b == END:
            out.append("<END>")
        elif b == VK:
            out.append("<VK>")
        elif 0x20 <= b < 0x7F:
            out.append(chr(b))
        else:
            out.append(f"\\x{b:02x}")
    return "".join(out)


# ─────────────────────────────────────────────────────────────────────────────
# Самотест
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")

    # Простейший поток: три команды
    stream = (
        b"J1" + bytes([VK]) +
        b"S" + bytes([VK]) +
        b"4 280 200" + bytes([VK]) +
        b"> 1 150" + bytes([VK]) +
        b"9 1" + bytes([VK]) + b";0 " + b"HELLO" + bytes([VK])
    )

    print("=== dump_commands ===")
    dump_commands(stream)

    print("\n=== split_by_font ===")
    mixed = b"AB" + bytes([S0]) + b"CD" + bytes([S1]) + b"EF" + bytes([END]) + b"GH"
    print(split_by_font(mixed))

    print("\n=== decode_text ===")
    print(repr(decode_text(mixed)))
