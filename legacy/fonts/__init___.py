"""
fonts/__init__.py — пакет работы с кодировкой и знакогенератором
терминала «Электроника МС 7401».

Реэкспортирует ключевые сущности из `koi8_r` и `glyphs`, чтобы
пользователю не приходилось помнить точные имена модулей.

Пример:
    from fonts import decode_with_switching, render_text, S0

    text = decode_with_switching(b"AB" + bytes([S0]) + b"\\xc1\\xc2")
    bitmaps = render_text(text, font="RU", size=1)

Автор: <ваше имя>
Лицензия: MIT
"""

from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────────
# Кодировка КОИ-8Р
# ─────────────────────────────────────────────────────────────────────────────

from fonts.koi8_r import (
    # кодеки
    KOI8_R,
    KOI8_U,

    # спецкоды терминала
    VK,
    S0,
    S1,
    LF,
    END,
    NUL,
    BEL,
    BS,
    TAB,
    NL,
    VT,
    FF,
    CR,
    ESC,
    SP,

    # отображение спецкодов
    SPETSCODES,
    spetscode_name,

    # диапазоны
    CYRILLIC_RANGE,
    CYRILLIC_LOWER_RANGE,
    CYRILLIC_UPPER_RANGE,
    SPECIAL_CYRILLIC_BYTES,

    # множества байтов
    CYRILLIC_UPPER_BYTES,
    CYRILLIC_LOWER_BYTES,
    CYRILLIC_SYMBOL_BYTES,
    CYRILLIC_BYTES,
    LATIN_UPPER_BYTES,
    LATIN_LOWER_BYTES,
    LATIN_BYTES,
    DIGIT_BYTES,
    PUNCTUATION_BYTES,
    BOX_DRAWING_BYTES,
    CONTROL_BYTES,

    # предикаты
    is_spetscode,
    is_control,
    is_russian_upper,
    is_russian_lower,
    is_russian_letter,
    is_latin_upper,
    is_latin_lower,
    is_latin_letter,
    is_digit,
    is_punctuation,
    is_box_drawing,
    is_printable,
    has_cyrillic,
    has_latin,

    # декодирование/кодирование
    decode_byte,
    decode_bytes,
    decode_with_switching,
    encode_char,
    encode_string,
    detect_font,

    # отладочные утилиты
    byte_table,
    dump_table,
)


# ─────────────────────────────────────────────────────────────────────────────
# Знакогенератор
# ─────────────────────────────────────────────────────────────────────────────

from fonts.glyphs import (
    # типы
    Bitmap,

    # константы
    GLYPH_W,
    GLYPH_H,
    MAX_SCALE,

    # таблицы
    FONT_8X8,
    DOT_GLYPHS,
    SPETS_CODES,

    # рендеринг
    get_glyph,
    get_fallback_glyph,
    render_glyph,
    render_text,

    # восьмеричные утилиты
    bitmap_to_octal,
    octal_to_bitmap,
    parse_graf_bitmap,
    render_bitmap_ascii,
)


# ─────────────────────────────────────────────────────────────────────────────
# Публичный API
# ─────────────────────────────────────────────────────────────────────────────

__all__ = [
    # кодеки
    "KOI8_R",
    "KOI8_U",

    # спецкоды
    "VK", "S0", "S1", "LF", "END",
    "NUL", "BEL", "BS", "TAB", "NL", "VT", "FF", "CR", "ESC", "SP",
    "SPETSCODES", "spetscode_name",

    # диапазоны и множества
    "CYRILLIC_RANGE",
    "CYRILLIC_LOWER_RANGE",
    "CYRILLIC_UPPER_RANGE",
    "SPECIAL_CYRILLIC_BYTES",
    "CYRILLIC_UPPER_BYTES",
    "CYRILLIC_LOWER_BYTES",
    "CYRILLIC_SYMBOL_BYTES",
    "CYRILLIC_BYTES",
    "LATIN_UPPER_BYTES",
    "LATIN_LOWER_BYTES",
    "LATIN_BYTES",
    "DIGIT_BYTES",
    "PUNCTUATION_BYTES",
    "BOX_DRAWING_BYTES",
    "CONTROL_BYTES",

    # предикаты
    "is_spetscode",
    "is_control",
    "is_russian_upper",
    "is_russian_lower",
    "is_russian_letter",
    "is_latin_upper",
    "is_latin_lower",
    "is_latin_letter",
    "is_digit",
    "is_punctuation",
    "is_box_drawing",
    "is_printable",
    "has_cyrillic",
    "has_latin",

    # декодирование
    "decode_byte",
    "decode_bytes",
    "decode_with_switching",
    "encode_char",
    "encode_string",
    "detect_font",

    # отладочные утилиты
    "byte_table",
    "dump_table",

    # знакогенератор
    "Bitmap",
    "GLYPH_W",
    "GLYPH_H",
    "MAX_SCALE",
    "FONT_8X8",
    "DOT_GLYPHS",
    "SPETS_CODES",
    "get_glyph",
    "get_fallback_glyph",
    "render_glyph",
    "render_text",
    "bitmap_to_octal",
    "octal_to_bitmap",
    "parse_graf_bitmap",
    "render_bitmap_ascii",
]


# ─────────────────────────────────────────────────────────────────────────────
# Самотест
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    print("Проверка пакета fonts")
    print("=" * 60)
    print(f"  KOI8_R = {KOI8_R!r}")
    print(f"  VK     = 0o{VK:03o} (0x{VK:02X}) = {spetscode_name(VK)}")
    print(f"  S0     = 0o{S0:03o} (0x{S0:02X}) = {spetscode_name(S0)}")
    print(f"  S1     = 0o{S1:03o} (0x{S1:02X}) = {spetscode_name(S1)}")
    print(f"  END    = 0o{END:03o} (0x{END:02X}) = {spetscode_name(END)}")
    print()

    print(f"  Всего глифов в FONT_8X8: {len(FONT_8X8)}")
    print(f"  Точечных глифов СПЕЦ:    {len(DOT_GLYPHS)}")
    print(f"  Кодов СПЕЦ:              {len(SPETS_CODES)}")
    print()

    # Простой сценарий: декодируем строку и рендерим её.
    data = b"AB" + bytes([S0]) + b"\xc1\xc2" + bytes([S1]) + b"CD"
    text = decode_with_switching(data)
    print(f"  Вход:   {data!r}")
    print(f"  Текст:  {text!r}")
    print(f"  Шрифт:  {detect_font(data)}")
    print()

    print("  Рендер 'A' (EN):")
    bm = render_text("A", font="EN", size=1)[0]
    for row in bm:
        print("    " + "".join("#" if px else "." for px in row))
    print()

    print("  Рендер 'Ж' (RU):")
    bm = render_text("Ж", font="RU", size=1)[0]
    for row in bm:
        print("    " + "".join("#" if px else "." for px in row))
    print()

    print("  Разбор восьмеричной записи:")
    sample = "376В,202В,202В,202В,202В,202В,376В"
    parsed = parse_graf_bitmap(sample)
    print(f"    {sample}")
    print(f"    → {parsed}")
    print()
    print(render_bitmap_ascii(parsed))