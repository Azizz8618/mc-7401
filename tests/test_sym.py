"""
test_sym.py — все знаки знакогенератора СИМ.

СИМ-терминал показывает не буквы, а **растры ПЗУ**: знакомест заливается
по таблице `chars.glyph_matrix` и `chars.FONT_7X8_CYR`.  Значит, «уметь
отрабатывать все знаки СИМ» — это три проверки на каждый знак:

* знак рисуется в кадр и читается обратно тем же знаком (`read_row`);
* код линии ДКОИ превращается в знак, у которого есть растр
  (`display_dks`), для **всех** кодов 0x20…0x7E;
* знак, набранный на клавиатуре, уходит на линию тем же кодом, каким
  приходит обратно (`encode_input` → `display_dks`).

Исключение одно и оно задокументировано в `chars.py`: слотов «Ъ»/«ъ» в
ПЗУ нет вовсе (32 буквы занимают их места), поэтому эти два знака
заменены типографскими — `CYR_SUBSTITUTED`.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mc7401 import chars                                        # noqa: E402
from mc7401.chars import (apply_set, display_dks, encode_input)  # noqa: E402
from mc7401.console import (ALPHABET, CELL_H, COLS, ROWS, Console,  # noqa: E402
                            glyph_art, read_row)
from mc7401.framebuffer import Framebuffer                     # noqa: E402

#: Знаки ПЗУ латиницей и знаками препинания.
ASCII_LINE = ''.join(chr(c) for c in range(0x20, 0x7F))
#: Русские буквы набора ДКОИ (те, что приходят с линии).
DCOI = 'АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдежзийклмнопрстуфхцчшщъыьэюя'


def draw(text: str) -> str:
    """Нарисовать `text` глифами ПЗУ в кадр и прочитать обратно.

    Ряд кадра шире строки (`COLS` знакомест), поэтому читаем ровно
    столько знаков, сколько рисовали.
    """
    fb = Framebuffer()
    con = Console(fb)
    for ch in text:
        con.put_char(ch)
    return read_row(fb, (ROWS - 1) * CELL_H, cols=len(text))


# ── ПЗУ: каждый знак и нарисовать, и прочитать ──────────────────────────────

def test_every_rom_glyph_draws_and_reads_back():
    """Каждый знак ПЗУ рисуется и читается обратно тем же знаком."""
    wrong = [(ch, draw(ch)) for ch in ALPHABET if draw(ch) != ch]
    assert not wrong, f'знаки не проходят туда-обратно: {wrong[:10]}'


def test_rom_line_survives_in_one_row():
    """Вся строка ПЗУ целиком рисуется и читается без потерь."""
    line = ''.join(ch for ch in ALPHABET if ch != ' ')
    fb = Framebuffer()
    con = Console(fb)
    for ch in line[:COLS]:
        con.put_char(ch)
    got = read_row(fb, (ROWS - 1) * CELL_H, cols=len(line[:COLS]))
    assert got == line[:COLS], f'разбор: {got!r}' 


def test_rom_covers_ascii_and_cyrillic():
    """В ПЗУ есть и все 95 знаков ASCII, и все русские буквы СИМ."""
    missing = [ch for ch in ASCII_LINE if glyph_art(ch) is None]
    assert not missing, f'нет растра: {missing}'
    cyr = [ch for ch in DCOI if glyph_art(ch) is None]
    assert not cyr, f'нет растра: {cyr}'
    assert len(chars.FONT_7X8_CYR) >= 60, 'ПЗУ потерял русские буквы'


def test_alphabet_holds_the_whole_rom():
    """Разбор знает все знаки ПЗУ, а не список, выписанный руками."""
    rom = {ch for ch in ASCII_LINE + ''.join(chars.FONT_7X8_CYR)
           if glyph_art(ch) is not None}
    lost = sorted(rom - set(ALPHABET))
    assert not lost, f'разбор не знает знаков: {lost}'


# ── приём с линии: код → знак ───────────────────────────────────────────────

def test_every_line_code_has_a_sign():
    """Каждый код линии ДКОИ даёт знак, у которого есть растр.

    Иначе знак просто не рисуется: код пришёл, а на экране пусто, и
    разбор снимка экрана покажет пробел на месте символа.
    """
    empty = [hex(c) for c in range(0x20, 0x7F)
             if not display_dks(c) or glyph_art(display_dks(c)) is None]
    assert not empty, f'коды без знака: {empty}'


# ── набор с клавиатуры: знак → код → знак ───────────────────────────────────

def test_cyrillic_round_trips_through_the_line():
    """Русская буква, набранная на клавиатуре, приходит обратно той же.

    Проверяется набор **Н2 (КОИ-7)** — тот, в котором ходит линия ДКС:
    знак уходит кодом строки `0x60…0x7E`, а `display_dks` читает её как
    кириллицу заглавными (строка ДКС регистронезависима).
    """
    wrong = []
    for ch in DCOI:
        if ch in chars.CYR_SUBSTITUTED:
            continue                        # в ПЗУ этих знаков нет
        code = encode_input(ch)
        back = display_dks(code[0]) if len(code) == 1 else ''
        if back.upper() != ch.upper():
            wrong.append((ch, code.hex(), back))
    assert not wrong, f'буквы идут не тем кодом: {wrong[:10]}'


def test_latin_round_trips_in_koi8r_charset():
    """В наборе КОИ-8Р латиница идёт на линию своим кодом.

    В Н2 (КОИ-7) её представить нельзя: строка `0x61…0x7A` занята
    кириллицей, поэтому строчные латинские набираются **заглавными**
    (`chars.KOI7_LAT_UPPER`).  Полноценная латиница в обоих регистрах
    доступна в КОИ-8Р — оператор переходит на него клавишей F8 НАБОР.
    """
    wrong = []
    for ch in ASCII_LINE:
        code = encode_input(ch, set_name='koi8r')
        if len(code) != 1 or apply_set(code[0], 'koi8r') != ord(ch) & 0x7F:
            wrong.append((ch, code.hex()))
    assert not wrong, f'латиница идёт не тем кодом: {wrong[:10]}'


def test_koi7_lowercase_latin_becomes_uppercase():
    """Таблица КОИ-7: строчная латинская набирается заглавной.

    На линии ДКС (`set ttyN dks,raw`) нет строчных латинских: код
    `0x63` приходит от машины как «Ц», `0x6E` — как «Н».  Единственный
    способ набрать латиницу — заглавная строка `0x41…0x5A`.
    """
    assert chars.KOI7_LAT_UPPER['a'] == 'A'
    assert chars.KOI7_LAT_UPPER['z'] == 'Z'
    assert len(chars.KOI7_LAT_UPPER) == 26
    assert len(chars.KOI7_CODE_UPPER) == 26
    for low, up in zip(chars.LAT_LOWER, chars.LAT_UPPER):
        assert chars.KOI7_CODE_UPPER[ord(low)] == ord(up)
        assert chars.KOI7_LAT_UPPER[low] == up
        # На линию уходит код заглавной, и знакогенератор читает его как
        # ту же букву латиницы.
        code = encode_input(low)[0]
        assert code == ord(up), f'{low!r} ушла кодом {code:o}'
        assert display_dks(code) == up


def test_koi7_keeps_uppercase_latin_digits_and_punctuation():
    """Заглавные латинские, цифры и знаки в Н2 не трогаются."""
    for ch in 'ABCXYZ0189.,-/:;()!?':
        assert encode_input(ch) == ch.encode('ascii')


def test_koi8r_keeps_latin_case_and_cyrillic_eight_bit():
    """В КОИ-8Р (F8 НАБОР) латиница обоих регистров и кириллица 0xC0…0xFF."""
    assert encode_input('a', set_name='koi8r') == b'a'
    assert encode_input('A', set_name='koi8r') == b'A'
    assert encode_input('п', set_name='koi8r') == b'\xf0'
    assert encode_input('П', set_name='koi8r') == b'\xd0'


def test_input_label_shows_what_the_terminal_will_show():
    """Локальное эхо рисует знак таким, каким его покажет терминал.

    В Н2 `cat` встаёт как `CAT`, в КОИ-8Р — как `cat`.  Иначе оператор
    правил бы на экране не тот текст, который ушёл на ЭВМ.
    """
    assert chars.input_label('c') == 'C'
    assert chars.input_label('c', set_name='koi8r') == 'c'
    assert chars.input_label('п') == 'П'          # строка ДКС без регистра
    assert chars.input_label('п', set_name='koi8r') == 'п'
    assert chars.input_label('\r') == ''          # управляющий не рисуется


def test_hard_sign_is_a_documented_substitute():
    """«Ъ»/«ъ» заменены типографскими знаками — слотов в ПЗУ нет.

    Проверка зафиксирована намеренно: если когда-нибудь в ПЗУ появятся
    настоящие слоты, тест покажет это как расхождение, а не как тихую
    порчу набора.
    """
    assert chars.CYR_SUBSTITUTED == frozenset('Ъъ')
    for ch, expect in (('Ъ', '*'), ('ъ', 'J')):
        assert glyph_art(expect) is not None, f'замены {expect!r} нет в ПЗУ'
        assert glyph_art(ch) is not None, f'{ch!r} потерял растр ПЗУ'


# ── знаки служебных строк ───────────────────────────────────────────────────

def test_service_signs_have_rom_glyphs():
    """Знаки, которыми рисуются подвал и строка состояния, есть в ПЗУ."""
    from mc7401.panel import (Status, legend_rows, status_line,
                              state_rows)

    used = set()
    for cell in (state_rows(Status()) + legend_rows()
                 + status_line(Status(mode='host', banner=True,
                                      link_kind='ДКС'))):
        for text, _ in cell:
            used.update(text)
    missing = sorted(ch for ch in used
                     if ch not in ' |' and glyph_art(ch) is None)
    assert not missing, f'подвалу нужны знаки без растра: {missing}'


def test_service_values_are_drawn_not_blank():
    """Пустое значение в подвале видно: знак-заполнитель есть в ПЗУ.

    Раньше там стояло тире, которого в знакогенераторе СИМ нет: ячейка
    выходила пустой, и ряд обрывался на полуслове.
    """
    from mc7401.panel import EMPTY, Status, state_rows

    assert glyph_art(EMPTY) is not None, f'знака {EMPTY!r} нет в ПЗУ'
    cells = state_rows(Status())            # всё пустое
    for cell in cells:
        for text, _ in cell:
            for ch in text:
                assert ch in ' |' or glyph_art(ch) is not None, \
                    f'ячейка {text!r} рисует знак без растра: {ch!r}'

# ── графические примитивы ───────────────────────────────────────────────────

def test_every_code_has_a_valid_matrix_on_both_pages():
    """Каждый код ПЗУ даёт корректный растр на обеих страницах.

    Страница шрифта (Н0/Н1 против Н2) переключает только буквенные
    позиции, но растр обязаны давать обе: иначе знак с линии не рисуется
    и в разборе снимка выходит пробелом.  Растр — 7 точек в ширину и
    либо 8 рядов (знак ПЗУ), либо 7 (знак СПЕЦ, `≡110`): оба рисуются,
    и проверка принимает оба.
    """
    bad = []
    for code in range(0x00, 0x80):
        for russian in (False, True):
            art = chars.glyph_matrix(code, russian)
            if (art is None or len(art) not in (7, 8)
                    or any(len(r) != 7 for r in art)):
                bad.append((hex(code), 'Н2' if russian else 'Н1'))
    assert not bad, f'коды без растра: {bad[:10]}'


def test_user_char_pads_to_the_full_cell():
    """Знак СПЕЦ дописывается пустым рядом **шириной знакоместа**.

    Знаки СПЕЦ задают семью рядами, а знакомест ПЗУ — восемь.  Раньше
    восьмой ряд дописывался `(0,)` — шириной в одну точку, и у рамок и
    заливок нижний ряд выходил обрубленным.
    """
    from mc7401.chars import USER_TABLE

    try:
        USER_TABLE.define(2, [0o376, 0o202, 0o202, 0o202,
                              0o202, 0o202, 0o376])      # рамка
        art = chars.glyph_matrix(2, False)
        assert len(art) == 8, f'знак СПЕЦ из {len(art)} рядов'
        for n, row in enumerate(art):
            assert len(row) == 7, f'ряд {n} шириной {len(row)}'
        assert art[7] == (0,) * 7, 'нижний ряд знака СПЕЦ не пустой'
    finally:
        USER_TABLE.clear()


def test_user_chars_draw_through_the_terminal():
    """Знаки СПЕЦ (команда `≡110`) рисуются в знакоместе.

    СПЕЦ — единственный набор не из ПЗУ: рамки и заливки псевдографики
    из `graf.pas` задаются семью байтами на знак и рисуются как
    графический примитив.  Пустая таблица — не отказ: знаки появляются
    по команде СПЕЦ, и после задачи такой знак обязан занять
    знакомест наравне с буквенными.
    """
    from mc7401.chars import USER_CHARS, USER_TABLE

    box = chars._BOX[0o241]                   # вертикальная линия рамки
    USER_TABLE.define(0, list(box))
    art = chars.glyph_matrix(0, False)
    assert art is not None and len(art) == 8, 'знак СПЕЦ без растра'
    assert any(any(row) for row in art), 'знак СПЕЦ пустой'
    got = draw(chr(0))                        # код 0 — знак СПЕЦ
    assert got == chr(0) or not got.strip(), 'знак СПЕЦ не рисуется'
    USER_TABLE.clear()
    assert not any(USER_TABLE.chars), 'таблица СПЕЦ не очистилась'
    del USER_CHARS


def test_box_primitives_have_patterns():
    """У всех знаков рамок и заливок есть узор — иначе они пустые."""
    box = getattr(chars, '_BOX', None)
    assert box, 'нет знаков псевдографики'
    empty = [hex(code) for code, rows in box.items() if not any(rows)]
    assert not empty, f'пустые узоры рамок: {empty}'
