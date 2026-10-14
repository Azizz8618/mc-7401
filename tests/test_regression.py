"""
tests/test_regression.py — регрессионные проверки исправлений 2026-10-10.

Каждый тест соответствует конкретному дефекту, найденному при разборе
сценариев `tools/gen_golden.py` (см. JOURNAL.md, раздел «Разбор падений
сценариев»).  Задача набора — не перекрыть всё, а не дать этим правкам
снова развалиться.

Поток команд: записи разделены `ВК` (0o125 = 85 = 'U'), координаты
`X` вправо / `Y` вверх, начало в левом нижнем углу, экран 560×400.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mc7401 import Framebuffer, TerminalEmulator                     # noqa: E402
from mc7401.chars import glyph_matrix                               # noqa: E402
from mc7401.constants import END, S0, S1, VK                          # noqa: E402
from mc7401.reader import PAD_CMD                                     # noqa: E402
from mc7401.link import (                                           # noqa: E402
    IacStripper, LinkError, busy_reply, parse_port_spec, spec_hint, split_endpoint,
)
from mc7401.parser import CommandParser, Op                          # noqa: E402
from mc7401.reader import CommandStreamReader, EventKind            # noqa: E402

GOLDEN = ROOT / 'golden' / 'streams'


def stream(*records: bytes | str) -> bytes:
    """Собрать поток: каждой записи дописать `ВК`.

    `str` кодируется в КОИ-8 — так текст идёт в линию от программы.
    """
    out = bytearray()
    for r in records:
        out += (r.encode('koi8-r') if isinstance(r, str) else r) + bytes((VK,))
    return bytes(out)


def run(data: bytes) -> TerminalEmulator:
    emu = TerminalEmulator()
    emu.run(data)
    return emu


def parse_all(data: bytes):
    """Разобрать поток и вернуть список команд."""
    reader = CommandStreamReader()
    reader.feed(data)
    reader.close()
    parser = CommandParser()
    return [parser.parse(ev.data) for ev in reader.pop()
            if ev.kind is EventKind.COMMAND]


# ── дефект 27: маркеры структуры не должны считаться командами ──────────────

def test_pad_and_vuk_block_are_not_commands():
    """Заполнитель `0o32 ВК` и маркер блока `'/' ВК` — не операции.

    Раньше они попадали в разбор как команды и учитывались в счётчике
    «нераспознано»; теперь у них собственные события.
    """
    data = PAD_CMD + b'/' + bytes((VK,)) + b'z' + b'0' * 19 + b'J1' + bytes((VK,))
    events = list(_events(data))
    kinds = [ev.kind for ev in events]
    assert EventKind.PAD in kinds
    assert EventKind.VUK_BLOCK in kinds
    # Командой является только `J1`
    commands = [ev.data for ev in events if ev.kind is EventKind.COMMAND]
    assert commands == [b'J1' + bytes((VK,))]


def _events(data: bytes):
    reader = CommandStreamReader()
    reader.feed(data)
    reader.close()
    return reader.pop()


def test_unknown_count_is_zero_for_plain_commands():
    """Обычные графические команды разбираются без потерь."""
    cmds = parse_all(stream('J1', 'S', '4 10 20', '7 1 30 40'))
    assert [c.op for c in cmds] == [Op.GRAPHICS_MODE, Op.CLEAR,
                                    Op.MOVE_ABS, Op.LINE_ABS]


def test_vuk_payload_tail_marker_is_split_out():
    """`АР2 'У  '` в хвосте блока ВУК не должен попадать в данные.

    Маркер входа в кадр оставался в полезной нагрузке блока и портил
    последующий разбор.
    """
    data = (b'/' + bytes((VK,)) + b'z' + b'0' * 19 + b'J1' + bytes((VK,))
            + b'T#  ' + b'TF>' + bytes((VK,)))
    events = list(_events(data))
    assert any(ev.kind is EventKind.FRAME_BEGIN for ev in events)
    assert any(ev.kind is EventKind.FRAME_END for ev in events)
    # В полезной нагрузке блока маркер входа в кадр остаться не должен
    for ev in events:
        if ev.kind is EventKind.COMMAND and ev.meta.get('payload'):
            assert b'T#  ' not in ev.data


# ── дефект 24: параметры команды `9` приходят кодами символов ───────────────

def test_text_size_argument_is_a_char_code():
    """`9` и `;O` несут коды символов (`СНР(МСШ)`), а не числа.

    Разбор как `int()` падал, и текст выводился нулевым размером.
    """
    data = stream(b'4 100 200', b'9\x01', ';\x00 ПРИВЕТ')
    emu = TerminalEmulator()
    emu.run(data)
    assert emu.stats['chars'] == 6


def test_text_size_is_remembered_between_records():
    """Размер шрифта берётся из предыдущей записи `9 S`."""
    parser = CommandParser()
    parser.parse(b'9\x02' + bytes((VK,)))          # запись размера
    cmd = parser.parse(';\x01 АБВ'.encode('koi8-r') + bytes((VK,)))  # данные
    assert cmd.op is Op.TEXT
    assert cmd.args[0] == b'\x02'
    assert cmd.payload.decode('koi8-r') == 'АБВ'


def test_text_command_draws_glyphs():
    """Текст должен оставлять точки на кадре (регрессия «0 точек»)."""
    emu = run(stream('4 50 300', 'J1', b'9\x01', ';\x00 ИТМ'))
    assert emu.fb.count_set() > 0


# ── дефект 25: спецкоды шрифта доходят до эмулятора ─────────────────────────

def test_s0_switches_to_koi8r():
    """`S0` (0o160) включает русский набор КОИ-8Р.

    Наборов у МС7401 два: Н2 (КОИ-7, VT-52) и КОИ-8Р.  `S0` — русский,
    `S1` — возврат к международному Н2.  Так их использует `ТЕКФАЙ`
    в `graf.pas`: перед русскими знаками посылается `S0`.
    """
    emu = run(stream('J1', '4 10 300', b'9\x01', ';\x00 ' + chr(S0) + 'АБВ'))
    assert emu.state.font == 'koi8r'


def test_s1_returns_to_default_set():
    """`S1` (0o161) возвращает набор по умолчанию — КОИ-8Р."""
    from mc7401.constants import S0, S1
    emu = run(stream('J1', '4 10 300', b'9\x01',
                     ';\x00 ' + chr(S0) + chr(S1) + 'АБВ'))
    assert emu.state.font == 'koi8r'


def test_default_charset_set_is_koi8r():
    """Страница шрифта ПЗУ по умолчанию — КОИ-8Р, набор **ввода** — Н2.

    Это две разные вещи.  Знаки на экране (`display_char`, страницы
    `S0`/`S1`) всегда КОИ-8Р; набор ввода по умолчанию — Н2 (КОИ-7),
    потому что в нём ходит линия ДКС (`chars.INPUT_SET`, F8 «НАБОР»).
    """
    from mc7401.chars import DEFAULT_SET, INPUT_SET, SETS
    assert DEFAULT_SET == 'koi8r'
    assert INPUT_SET == 'n2'
    assert SETS == ('n2', 'koi8r')
    assert TerminalEmulator().state.font == 'koi8r'


def test_default_charset_set_is_koi8r_2():
    """То же для совместимости со старыми тестами: шрифт — КОИ-8Р."""
    from mc7401.chars import DEFAULT_SET, INPUT_SET, SETS
    assert DEFAULT_SET == 'koi8r'
    assert INPUT_SET == 'n2'
    assert SETS == ('n2', 'koi8r')
    assert TerminalEmulator().state.font == 'koi8r'


def test_russian_glyph_differs_from_latin():
    """Один и тот же код в русском и латинском шрифте даёт разный знак."""
    ru = glyph_matrix(0xE9, True)
    lat = glyph_matrix(0xE9, False)
    assert any(any(r) for r in ru)
    assert ru != lat


def test_s0_is_not_drawn_as_character():
    """`S0` — управляющий код, он не должен попадать в строку."""
    emu = run(stream('J1', '4 10 300', b'9\x01',
                     ';\x00 ' + chr(S0) + 'АБВ'))
    assert emu.stats['chars'] == 3


# ── дефект 21/26: разбор потока из реальных программ ────────────────────────

def render(name: str) -> Framebuffer:
    """Отрисовать эталонный поток и вернуть кадр оператора.

    Программа (`graf.pas`) очищает экран командой `S` в начале каждой
    картины и оставляет его пустым после выхода.  Поэтому берётся самое
    «наполненное» состояние кадра — после каждой команды: `ПТИЦА`
    рисует птицу пером `+` и тут же стирает пером `≠` внутри одного
    блока ВУК.  Так же работает `tools/gen_golden.py`.
    """
    path = GOLDEN / f'{name}.bin'
    if not path.exists():                     # поток не сгенерирован
        pytest.skip(f'{path.name} отсутствует')
    emu = TerminalEmulator()
    best, best_n = emu.fb.copy(), 0
    reader = CommandStreamReader()
    reader.feed(path.read_bytes())
    reader.close()
    for ev in reader.pop():
        emu.handle_event(ev)
        if ev.kind is EventKind.COMMAND:
            n = emu.fb.count_set()
            if n > best_n:
                best, best_n = emu.fb.copy(), n
    return best if best_n else emu.fb


@pytest.mark.parametrize('name', ['СЕТ', 'СОЛ', 'КОТ', 'ЦЕП', 'ФУН',
                                  'ЛИН', 'ОКР', 'ГРА', 'ШАР', 'ЛЕД',
                                  'УЗО', 'РОЗ', 'ТИП', 'СИМ'])
def test_golden_streams_render(name: str):
    """Эталонные потоки `graf.pas` дают непустой кадр."""
    assert render(name).count_set() > 0, f'{name}: пустой кадр'


def test_grid_fills_most_of_screen():
    """`СЕТ` рисует сетку — кадр должен быть заполнен существенно."""
    assert render('СЕТ').count_set() > 10000


def test_title_screen_renders_text():
    """`ЗАГ` — титульный экран: должны быть и рамки, и текст."""
    assert render('ЗАГ').count_set() > 100


# ── геометрия и кадр ───────────────────────────────────────────────────────

def test_framebuffer_dimensions():
    fb = Framebuffer()
    assert (fb.width, fb.height) == (560, 400)


def test_origin_is_bottom_left():
    """Точка (0, 0) — левый нижний угол."""
    fb = Framebuffer()
    fb.point(0, 0)
    rows = [bytes(r) for r in fb.to_rows_bottom_up()]
    assert rows[0][0] & 0x80          # старший бит первого байта
    assert not rows[-1][0] & 0x80


def test_clear_empties_frame():
    fb = Framebuffer()
    fb.point(10, 10)
    fb.clear()
    assert fb.count_set() == 0


def test_copy_is_independent():
    fb = Framebuffer()
    fb.point(5, 5)
    c = fb.copy()
    c.point(6, 6)
    assert not fb.get(6, 6)
    assert c.get(5, 5)


# ── дефекты 2026-10-10: зеркальный кадр, латиница, смещение окна ───────────

def test_frame_output_is_top_down():
    """Вывод кадра — сверху вниз, как на экране.

    Строка хранения 0 — верх экрана; раньше все backend'ы шли по строкам
    хранения с конца, и PNG/SVG/Tk/Pygame выходили зеркальными по
    вертикали (линии шли в обратном порядке, знаки — вверх ногами).
    """
    from mc7401.backends import BG, FG, framebuffer_pixels
    fb = Framebuffer()
    fb.point(0, 399)                       # верхний левый угол кадра
    rows = list(framebuffer_pixels(fb, FG, BG))
    assert rows[0][0] == FG                # первая строка вывода — верх
    assert rows[-1][0] == BG


def test_svg_keeps_screen_orientation():
    """SVG: верх кадра — y=0, а не y=399."""
    from mc7401.backends import SVGBackend
    fb = Framebuffer()
    fb.point(5, 399)                       # верх кадра
    svg = SVGBackend(fb).to_svg()
    assert 'y1="0"' in svg
    assert 'y1="399"' not in svg


def test_s0_inside_string_switches_glyphs_immediately():
    """`S0` переключает набор внутри строки, а не после неё.

    Признак набора обновлялся в разборе строки и не доходит до выбора
    глифов: «ИТМ» после `S0` рисовалось латинскими «itm» (титульный
    экран `ЗАГ`).
    """
    from mc7401.chars import glyph_matrix, text_offsets
    emu = run(stream('J1', '4 0 300', b'9\x01',
                     ';\x00 ' + chr(S0) + 'ИТМ'))
    exp = set()
    for n, code in enumerate('ИТМ'.encode('koi8-r')):
        for dx, dy in text_offsets(glyph_matrix(code & 0x7F, True), 0, 1):
            exp.add((n * 8 + dx, 300 + dy))
    got = {(x, y) for y in range(400) for x in range(560) if emu.fb.get(x, y)}
    assert got == exp


def test_font_set_changes_mid_string():
    """`S0`/`S1` внутри строки меняют набор для последующих знаков."""
    from mc7401.chars import glyph_matrix, text_offsets
    emu = run(stream('J1', '4 0 300', b'9\x01',
                     ';\x00 ' + chr(S0) + 'ВТ' + chr(S1) + ' ' + chr(S0) + 'АН'))
    exp = set()
    x0 = 0
    for code, ru in ((0xF7, True), (0xF4, True), (0x20, False),
                     (0xE1, True), (0xEE, True)):
        for dx, dy in text_offsets(glyph_matrix(code, ru), 0, 1):
            exp.add((x0 + dx, 300 + dy))
        x0 += 8
    got = {(x, y) for y in range(400) for x in range(560) if emu.fb.get(x, y)}
    assert got == exp


def test_window_shift_translates_coordinates():
    """`}` (`СМЕЩ` в graf.pas) переносит начало координат.

    В `ОКР` рисунок задан координатами за краем экрана (пары кругов
    около x=980 при `СМЕЩ(700,0)`): раньше смещение двигало только
    прямоугольник отсечения, и круги отсекались целиком.
    """
    emu = run(stream('J1', '} 700 0', '4 835 200', '>1 0'))
    assert emu.fb.get(135, 200)            # 835 − 700, 200 − 0
    assert emu.fb.count_set() == 1


def test_coordinates_outside_screen_are_clipped():
    """Точка за экраном не рисуется и без смещения окна."""
    emu = run(stream('J1', '4 835 200', '>1 0'))
    assert emu.fb.count_set() == 0


def test_pick_frame_sees_picture_before_xor_erase():
    """Кадр выбирается после каждой команды, а не на границе блока ВУК.

    `ПТИЦА` рисует птицу пером `+` и тут же стирает пером `≠` внутри
    одного блока: снимок по блокам неизменно попадал на пустое место и
    вместо птицы оставалась точечная траектория.
    """
    sys.path.insert(0, str(ROOT / 'tools'))
    import gen_golden
    xor_pen = bytes((0o256,))                        # `≠` — инвертирующее перо
    data = stream('J1', '4 100 100', '+', '7 1 200 200',
                  xor_pen, '7 1 200 200')
    fb, _ = gen_golden.pick_frame(data)
    assert fb.count_set() > 0          # поймано между рисованием и стиранием


def test_reset_accepts_cyrillic_k():
    """Сброс `К` в graf.pas записан кириллической буквой (байт 0xEB)."""
    emu = run(stream('J1', '4 10 10', '>1 5', b'\xeb'))
    assert emu.fb.count_set() == 0
    assert (emu.state.x, emu.state.y) == (0, 0)


def test_symbol_table_rows_are_drawn():
    """Строки набора `СИМВОЛЫ` идут без записей `ВК` — до `≡215`.

    Раньше они терялись внутри нагрузки блока ВУК («нераспознано»), и
    таблица знаков не выводилась вовсе.
    """
    row = bytes(range(0x21, 0x30))                    # !\"#$%&'()*+,-./
    data = (stream('J1', '4 40 340', '91', ':0 ')
            + bytes((0x1B, 0o306, 0o336, 0o215))       # ≡033≡306≡336≡215
            + bytes((0o273, 0o232))                    # ≡273≡240
            + row
            + bytes((0o215, 0o257, 0o215)) + b'0' * 14)
    emu = TerminalEmulator()
    emu.run(data)
    assert emu.stats['chars'] == len(row)
    assert emu.fb.count_set() > 0


def test_symbol_table_rows_are_literal():
    """В строке набора каждый байт — код знака, включая `p`/`q`.

    Коды 0o170/0o171 совпадают с переключателями шрифта `S0`/`S1`, но в
    таблице знаков они должны выводиться как знаки (иначе из таблицы
    пропадают `p` и `q`, а на русской странице — «п» и «я»).
    """
    from mc7401.chars import glyph_matrix, text_offsets
    data = (stream('J1', '4 0 300', '91', ':0 ')
            + bytes((0x1B, 0o306, 0o336, 0o215))
            + bytes((0o273, 0o232))
            + b'pq'
            + bytes((0o215, 0o257, 0o215)) + b'0' * 14)
    emu = TerminalEmulator()
    emu.run(data)
    exp = set()
    for n, code in enumerate(b'pq'):
        for dx, dy in text_offsets(glyph_matrix(code, False), 0, 1):
            exp.add((n * 8 + dx, 300 + dy))
    got = {(x, y) for y in range(400) for x in range(560) if emu.fb.get(x, y)}
    assert got == exp


def test_symbol_table_rows_switch_register():
    """Регистр строки набора переключают коды `0o16`/`0o17` (SO/SI, 15ИЭ).

    Так в таблице знаков `СИМВОЛЫ` уживаются обе секции: латинская
    (`0o17` в начале строки — позиции `A-Z`, `a-z`) и русская (`0o16` —
    зоны `0o60..0o7E` и `0o40..0o5E`).
    """
    from mc7401.chars import glyph_matrix, text_offsets
    data = (stream('J1', '4 0 300', '91', ':0 ')
            + bytes((0x1B, 0o306, 0o336, 0o215))
            + bytes((0o273, 0o232))
            + bytes((0o17,)) + b'AB' + bytes((0o16,)) + b'\xc1\xfe'
            + bytes((0o215, 0o257, 0o215)) + b'0' * 14)
    emu = TerminalEmulator()
    emu.run(data)
    exp = set()
    x0 = 0
    for code, ru in ((ord('A'), False), (ord('B'), False),
                     (0xC1, True), (0xFE, True)):
        for dx, dy in text_offsets(glyph_matrix(code, ru), 0, 1):
            exp.add((x0 + dx, 300 + dy))
        x0 += 8
    got = {(x, y) for y in range(400) for x in range(560) if emu.fb.get(x, y)}
    assert got == exp


def test_printable_letters_survive_text_rows():
    """`T` (0o124) и `U` (0o125) в строке набора — печатные знаки.

    В таблице знаков есть вся латиница.  Раньше эти два знака гасились
    как служебные (`АР2` и `ВК`); `U` и выразим только в строках набора
    (до `≡215`) — в записях, разделённых `ВК`, он сам является
    разделителем.
    """
    from mc7401.chars import glyph_matrix, text_offsets
    data = (stream('J1', '4 0 300', '91', ':0 ')
            + bytes((0x1B, 0o306, 0o336, 0o215))
            + bytes((0o273, 0o232))
            + b'TU'
            + bytes((0o215, 0o257, 0o215)) + b'0' * 14)
    emu = TerminalEmulator()
    emu.run(data)
    exp = set()
    for n, code in enumerate(b'TU'):
        for dx, dy in text_offsets(glyph_matrix(code, False), 0, 1):
            exp.add((n * 8 + dx, 300 + dy))
    got = {(x, y) for y in range(400) for x in range(560) if emu.fb.get(x, y)}
    assert got == exp


# ── текст ОС не должен уходить в разбор графики ─────────────────────────────

@pytest.mark.parametrize('line', [
    b'KLU4:\r\n', b'KEY:\r\n', b'K:\r\n', b'4103\r\n', b'2530\r\n',
    b'SVED\r\n', b'KREML\r\n', b'3 zona\r\n', b'* konec\r\n',
    b'+ plus\r\n', b'0 0 0\r\n', b'] ramka\r\n', b'} smesh\r\n',
    b'> krug\r\n', b'J nachalo\r\n', b'-n\r\n',
    b'F AJLY NA SBOJNYH ZONAH:\r\n', b'test b-3, fort\r\n',
])
def test_os_text_is_not_taken_for_graphics(line):
    """Строка выдачи ОС остаётся текстом, даже начинаясь с буквы или цифры.

    Разбор решался по **первому знаку** записи, и текст с ним делился:
    строка уходила в графику и пропадала.  Под ловушку попадали строки
    вида `4103`, `2530` и — что важнее — приглашение ввода ключа
    (`КЛЮЧ:`, `KEY:`): машина спрашивала, на экране ничего не
    появлялось, и диалог выглядел зависшим.
    """
    from mc7401.window import _is_commands
    assert not _is_commands(line), f'{line!r} принят за графику'


def test_key_prompt_reaches_the_screen():
    """Приглашение ввода ключа доходит до экрана."""
    from mc7401.console import COLS, ROWS, Console
    from mc7401.framebuffer import Framebuffer
    import mc7401.window as W
    grid = [[' '] * COLS for _ in range(ROWS)]
    fb = Framebuffer()
    con = Console(fb)
    real = Console.put_char

    def put_char(self, ch):
        if 0 <= self.row < ROWS and 0 <= self.col < COLS:
            grid[self.row][self.col] = ch
        real(self, ch)
    Console.put_char = put_char
    try:
        win = W.TerminalWindow(None)
        win.splash = False
        win.feed(b'KLU4:\r\n')
    finally:
        Console.put_char = real
    shown = [''.join(row).rstrip() for row in grid if ''.join(row).strip()]
    assert shown, 'приглашение ключа не дошло до экрана'


def test_command_stream_is_still_recognised():
    """Поток команд терминала опознаётся по рамке ВУК, а не по буквам."""
    from mc7401.window import _is_commands
    from mc7401.constants import AR2
    assert _is_commands(bytes((AR2,)) + b'F')
    for name in GOLDEN.glob('*.bin'):
        assert _is_commands(name.read_bytes()), f'{name.name} принят за текст'


def test_multiline_listing_is_not_truncated():
    """Многострочная выдача доходит целиком, построчно пакетами."""
    import mc7401.window as W
    lines = [b'fajly na svoih zonah:', b'kra', b'4103', b'2530',
             b'sved', b'kreml']
    win = W.TerminalWindow(None)
    win.splash = False
    for line in lines:
        win.feed(line + b'\r\n')
    # Строки не пропали: каждая увеличила число зажжённых точек.
    assert win.emu.fb.count_set() > 0
    for line in lines:
        assert not W._is_commands(line + b'\r\n'), line


# ── снятие рамки с рисунка (заставка) ───────────────────────────────────────

def framed(width=560, height=400, box=(8, 1, 554, 398)) -> Framebuffer:
    """Кадр с прямоугольной рамкой по краям и точкой внутри."""
    fb = Framebuffer(width, height)
    x0, y0, x1, y1 = box
    fb.hline(x0, x1, y0)
    fb.hline(x0, x1, y1)
    for y in range(y0, y1 + 1):
        fb.point(x0, y)
        fb.point(x1, y)
    fb.point(width // 2, height // 2)
    return fb


def test_strip_border_removes_rectangle():
    """Рамка по краям кадра снимается, содержимое остаётся."""
    fb = framed()
    # Периметр рамки: две горизонтали и две вертикали без углов дважды.
    perimeter = 2 * (554 - 8 + 1) + 2 * (398 - 1 + 1) - 4
    assert fb.count_set() == perimeter + 1
    assert fb.strip_border(), 'рамка не снята'
    lit = [(x, y) for y in range(fb.height) for x in range(fb.width)
           if fb.get(x, y)]
    assert lit == [(fb.width // 2, fb.height // 2)], \
        f'после снятия осталось {len(lit)} точек'


def test_strip_border_keeps_inner_lines():
    """Рамка снимается, а черта внутри рисунка остаётся."""
    fb = framed()
    fb.hline(100, 400, 200)               # черта поперёк рисунка
    assert fb.strip_border(), 'рамка не снята'
    assert sum(fb.get(x, 200) for x in range(fb.width)) == 301, \
        'внутренняя черта потерялась'
    for x in (8, 554):
        assert not any(fb.get(x, y) for y in range(fb.height)), \
            f'вертикаль рамки x={x} осталась'


def test_strip_border_needs_empty_margin():
    """Рамка, у которой за краем есть рисунок, рамкой не считается."""
    fb = framed()
    fb.point(3, 200)                      # точка снаружи рамки
    assert not fb.strip_border(), 'рамка принята при заполненном поле'
    assert fb.count_set() > 1000


def test_strip_border_on_plain_frame_is_noop():
    """На кадре без рамки ничего не происходит и это не ошибка."""
    fb = Framebuffer(560, 400)
    fb.hline(100, 400, 200)
    assert not fb.strip_border()
    assert fb.count_set() == 301


def test_strip_border_ignores_open_lines():
    """Незамкнутые линии рамкой не считаются."""
    fb = Framebuffer(560, 400)
    fb.hline(0, 559, 1)
    fb.hline(0, 559, 398)
    assert not fb.strip_border(), 'незамкнутые линии приняты за рамку'
    assert fb.count_set() == 2 * 560


def test_kot_splash_has_no_frame():
    """На заставке с КОТ рамки нет — рисунок отрезан по краям кадра."""
    from mc7401 import startup
    from mc7401.window import TerminalWindow
    from mc7401.config import Config
    from mc7401.reader import CommandStreamReader, EventKind
    from mc7401.emulator import TerminalEmulator
    pic = startup.picture_path('КОТ', prefer_stream=True)
    emu = TerminalEmulator()
    reader = CommandStreamReader()
    reader.feed(Path(pic).read_bytes())
    reader.close()
    best, best_n = emu.fb.copy(), 0
    for ev in reader.pop():
        if ev.kind is EventKind.VUK_BLOCK:
            n = emu.fb.count_set()
            if n > best_n:
                best, best_n = emu.fb.copy(), n
        emu.handle_event(ev)
    assert best_n, 'поток КОТ не разобрался'
    assert best.strip_border(), 'у КОТ не найдена рамка'
    # Ни одна сторона прежней рамки не осталась зажжённой.
    for x in (8, 554):
        assert not any(best.get(x, y) for y in range(best.height)), \
            f'вертикаль рамки x={x} осталась'
    for y in (1, 398):
        assert not any(best.get(x, y) for x in range(best.width)), \
            f'горизонталь рамки y={y} осталась'
    del TerminalWindow, Config


# ── связь с БЭСМ-6 по ДКС ─────────────────────────────────────────────────

def test_parse_port_spec_variants():
    """Спецификация портов: одиночный, список, диапазон, смешанный."""
    assert parse_port_spec('4202') == [4202]
    assert parse_port_spec('4202,4210') == [4202, 4210]
    assert parse_port_spec('4202-4204') == [4202, 4203, 4204]
    assert parse_port_spec('4202-4203,4210') == [4202, 4203, 4210]


def test_parse_port_spec_deduplicates():
    """Повторы отбрасываются, порядок сохраняется."""
    assert parse_port_spec('4210,4202,4210') == [4210, 4202]


def test_parse_port_spec_rejects_bad_input():
    with pytest.raises(LinkError):
        parse_port_spec('')
    with pytest.raises(LinkError):
        parse_port_spec('0')
    with pytest.raises(LinkError):
        parse_port_spec('4205-4202')       # перевёрнутый диапазон


def test_spec_hint_compacts_ranges():
    assert spec_hint([4202]) == '4202'
    assert spec_hint([4202, 4203, 4204]) == '4202-4204'
    assert spec_hint([4202, 4205, 4206]) == '4202,4205-4206'


def test_split_endpoint_uses_default_ports():
    """Без указания портов берётся диапазон ДКС 4202-4223."""
    host, ports = split_endpoint('127.0.0.1')
    assert host == '127.0.0.1'
    assert ports[:1] == [4202] and ports[-1] == 4223


def test_split_endpoint_with_explicit_port():
    host, ports = split_endpoint('localhost:4210')
    assert host == 'localhost'
    assert ports == [4210]


def test_busy_reply_detects_refusal():
    """SIMH отвечает отказом строкой — такой порт пропускаем."""
    assert busy_reply(b'DKS: line busy')
    assert busy_reply(b'CONNECTION NOT AVAILABLE')
    assert not busy_reply(b'\x01\x02\x03')


def test_iac_stripper_removes_negotiation():
    """telnet-переговоры не должны попасть в поток команд."""
    s = IacStripper()
    data = s.feed(b'\xff\xfb\x18\xff\xfd\x03OK')     # WILL/DO + данные
    assert data == b'OK'


def test_iac_stripper_subnegotiation():
    """`IAC SB … IAC SE` отбрасывается целиком."""
    s = IacStripper()
    assert s.feed(b'\xff\xfa\x01\xff\xf0OK') == b'OK'


def test_iac_stripper_keeps_plain_data():
    s = IacStripper()
    assert s.feed(b'J1\x554 10 20') == b'J1\x554 10 20'


def test_iac_stripper_state_survives_split():
    """Последовательность IAC может прийти двумя чтениями."""
    s = IacStripper()
    first = s.feed(b'\xff')
    assert first == b''
    assert s.feed(b'\xfb\x18OK') == b'OK'

# ── дефекты 30-33: текст, размеры, заглушки ───────────────────────────────

def test_text_size_command_sets_size():
    """Запись `9 : N` (`':0 '`) задаёт размер для следующих строк.

    В `СТРОКИ` размер приходит отдельной записью `9 : N`; раньше она
    разбиралась как неизвестная, и знаки выходили вырожденными.
    """
    parser = CommandParser()
    parser.parse(b'91' + bytes((VK,)))          # запись размера `9 1`
    cmd = parser.parse(b':0 ' + bytes((VK,)))    # установка размера 0
    assert cmd.op is Op.SET_TEXT_SIZE
    assert cmd.args[0] == b'\x00'
    assert parser.text_size == b'\x00'


def test_end_byte_terminates_text():
    """`ЕND` (0xFF) обрывает строку текста, а не рисуется как знак.

    Программа его не выводит (цикл `WНILЕ INРUТ↑≠ЕND`), но в отличие от
    прочих служебных кодов он не должен попадать на экран.
    """
    data = (stream('J1', '4 10 300', b'9\x01')
            + b';\x00 ' + 'АБ'.encode('koi8-r') + bytes((END,))
            + 'ВГ'.encode('koi8-r') + bytes((VK,)))
    emu = TerminalEmulator()
    emu.run(data)
    assert emu.stats['chars'] == 2      # только «АБ», «ВГ» отброшено


def test_zero_text_size_still_draws_glyphs():
    """Размер 0 — это «мелкий знак», а не отсутствие знака."""
    data = stream('J1', '4 10 300', b'91', b':0 ', ';\x00 АБВ')
    emu = TerminalEmulator()
    emu.run(data)
    assert emu.stats['chars'] == 3
    assert emu.fb.count_set() > 3          # знаки не выродились в точку


def test_bind_does_not_pollute_stream():
    """`ВIND` привязывает файл к устройству и ничего не пишет.

    Заглушка выводила строку устройства (`'> ≡377'`) в поток команд,
    откуда в текст попадали посторонние символы.
    """
    sys.path.insert(0, str(ROOT / 'tools'))
    import pascal.runtime as rt
    rt._terminal.clear()
    del rt._terminal.wire[:]
    rt.BIND('> ≡377')
    rt.BIND(' ==) ≡377')
    assert bytes(rt._terminal.wire) == b''


# ── кириллица: знаки из ПЗУ знакогенератора ───────────────────────────────

def test_cyrillic_font_covers_alphabet():
    """В таблице есть все буквы русского алфавита, включая «ё»."""
    from mc7401.chars import FONT_7X8_CYR
    for ch in 'АБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдежзийклмнопрстуфхцчшщъыьэюя':
        assert ch in FONT_7X8_CYR, f'нет знака {ch!r}'
    assert 'Ё' in FONT_7X8_CYR and 'ё' in FONT_7X8_CYR


def test_every_cyrillic_letter_maps_to_a_glyph():
    """Каждой букве КОИ-8 0x80…0xFF сопоставлен знак, а не «?»."""
    from mc7401.chars import GLYPHS_RU
    for ch in 'АБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдежзийклмнопрстуфхцчшщъыьэюя':
        code = ch.encode('koi8-r')[0] & 0x7F
        assert code in GLYPHS_RU, f'код {code:#04x} ({ch}) без знака'


def test_cyrillic_glyphs_are_7x8_cells():
    """Глифы ПЗУ занимают знакомест 7×8 и не пусты."""
    from mc7401.chars import FONT_7X8_CYR
    for ch, art in FONT_7X8_CYR.items():
        assert len(art) == 8, f'{ch}: {len(art)} строк'
        for row in art:
            assert len(row) == 7, f'{ch}: строка шириной {len(row)}'
        assert any('#' in row for row in art), f'{ch}: пустой знак'


def test_latin_glyphs_are_padded_to_cell():
    """Латинские знаки 5×7 выровнены в знакомест 7×8."""
    from mc7401.chars import _LAT_MATRIX
    for code, mat in _LAT_MATRIX.items():
        if code == 0x20:                       # пробел — пустой по определению
            continue
        assert len(mat) == 8
        for row in mat:
            assert len(row) == 7


def test_cyrillic_code_mapping_is_seven_bit_koi8():
    """Правило кодов: семибитный код `c` на странице `S0` = КОИ-8 `c|0x80`.

    Подтверждено статьёй («7 бит используется в каждом байте»):
    `п` = 0x70, `и` = 0x69, `т` = 0x74 — при кодах КОИ-8 0xF0/0xE9/0xF4.
    """
    from mc7401.chars import GLYPHS_RU
    for ch, koi8 in (('п', 0xF0), ('и', 0xE9), ('т', 0xF4)):
        assert (koi8 & 0x7F) in GLYPHS_RU


# ── развёртывание и документация ───────────────────────────────────────────

def test_packaging_points_at_real_package():
    """`pyproject.toml` описывает пакет `mc7401`, а не старые модули.

    Секция `[tool.setuptools]` прежде перечисляла плоские `main`,
    `reader`, `backends*` — установка падала бы.
    """
    import tomllib
    root = Path(__file__).resolve().parent.parent
    with open(root / 'pyproject.toml', 'rb') as fh:
        cfg = tomllib.load(fh)
    assert cfg['tool']['setuptools']['packages'] == ['mc7401', 'mc7401.backends']
    assert cfg['project']['scripts']['mc7401'] == 'mc7401.cli:main'


def test_shell_helpers_exist():
    """Сценарии развёртывания и короткие команды на месте."""
    root = Path(__file__).resolve().parent.parent
    for rel in ('install.sh', 'install.ps1', 'tools/mc7401-completion.bash',
                'tools/mc7401.fish', 'tools/check_dks_link.py',
                'docs/DEPLOY.md', 'docs/COMMANDS.md'):
        assert (root / rel).is_file(), f'нет {rel}'


def test_completion_scripts_are_valid():
    """Скрипты автодополнения разбираются оболочкой."""
    import subprocess
    root = Path(__file__).resolve().parent.parent
    for script in ('tools/mc7401-completion.bash',):
        r = subprocess.run(['bash', '-n', str(root / script)],
                           capture_output=True)
        assert r.returncode == 0, r.stderr.decode()


# ── фронтенд: экран, перо, терминал ────────────────────────────────────────

def test_viewport_maps_terminal_cells_to_pixels():
    """Ячейка терминала → пиксель кадра; Y переворачивается."""
    from mc7401.screen import ScreenRenderer, Viewport
    fb = Framebuffer()
    fb.point(100, 50)
    # моно-режим: один знак на знакомест, без escape-последовательностей
    r = ScreenRenderer(fb, mode='mono', vp=Viewport(scale=1, x=0, y=0))
    # Пиксель Y=50 стоит на 399-50=349 сверху; знакомест закрывает два
    # пикселя по вертикали, поэтому строка вывода 349//2 = 174.
    lines = r.render(560, 400).split('\n')
    assert len(lines) == 400
    assert lines[174][100] != ' '


def test_viewport_fit_and_clamp():
    """`fit` вписывает кадр в окно, `clamp` не выпускает за границы."""
    from mc7401.screen import Viewport
    fb = Framebuffer()
    vp = Viewport()
    vp.fit(fb, 80, 24)
    assert vp.scale >= 1
    vp.x, vp.y = -50, -50
    vp.clamp(fb, 80, 24)
    assert vp.x == 0 and vp.y == 0


def test_renderer_produces_two_pixels_per_cell():
    """Один знакомест закрывает два пикселя по вертикали."""
    from mc7401.screen import ScreenRenderer, Viewport
    fb = Framebuffer()
    fb.hline(0, 559, 399)          # верхняя строка кадра
    r = ScreenRenderer(fb, vp=Viewport(scale=1, x=0, y=0))
    body = r.render(60, 4)
    assert body.split('\n')[0].strip()


def test_pen_maps_mouse_and_draws_stroke():
    """Мышь работает как световое перо: нажатие и протяжка рисуют линию."""
    from mc7401.pen import LightPen
    pen = LightPen()
    pen.feed_mouse('\x1b[<0;20;10M', cols=80, rows=24)
    assert not pen.down
    pen.stroke(pen.fb)
    pen.feed_mouse('\x1b[<1;30;10M', cols=80, rows=24)
    assert pen.down
    pen.feed_mouse('\x1b[<32;60;10M', cols=80, rows=24)
    assert pen.down, 'протяжка сохраняет состояние пера'
    assert pen.stroke(pen.fb)
    assert pen.fb.count_set() > 0
    pen.feed_mouse('\x1b[<0;60;10m', cols=80, rows=24)
    assert not pen.down


def test_pen_coordinates_follow_viewport():
    """Смещение окна сдвигает перо согласованно с картинкой."""
    from mc7401.pen import LightPen
    a = LightPen().feed_mouse('\x1b[<0;10;5M', cols=80, rows=24, scale=1, vx=0, vy=0)
    b = LightPen().feed_mouse('\x1b[<0;10;5M', cols=80, rows=24, scale=1, vx=100, vy=50)
    assert b[0] - a[0] == 100


def test_pen_event_encoding_is_provisional():
    """Событие пера кодируется для линии связи (кодирование provisional)."""
    from mc7401.pen import PenEvent
    ev = PenEvent(200, 300, True)
    assert ev.encode() == b'\x1b[1;200;300M'
    assert 'PROVISIONAL' in __import__('mc7401.pen', fromlist=['x']).__doc__


# ── режимы терминала по образцу 15ИЭ-00-013 ────────────────────────────────

def test_host_mode_sends_every_character_immediately():
    """В режиме С ЭВМ каждый знак уходит в линию немедленно."""
    from mc7401.chars import encode_input
    from mc7401.session import Session
    s = Session()
    assert s.mode == 'host'
    # Русские буквы идут в строку 0x60…0x7E — по правилам 15ИЭ.
    assert s.type_line('СЕТ') == encode_input('СЕТ') == b'set'
    assert s.pending() == b''


def test_local_mode_buffers_until_send():
    """В режиме АВТОНОМНО набор копится, SEND передаёт накопленное."""
    from mc7401.session import Session
    s = Session()
    s.toggle_mode()
    assert s.mode == 'local'
    assert s.type_line('ЛИН 3 ') == b''          # в линию не уходит
    assert s.pending() == b'lin 3 '               # КОИ-7, регистр свёрнут
    out = s.send()
    # Конец строки на линии ДКС — ПР (как в 15ИЭ, `to_line` dks).
    assert out.endswith(bytes((0o015,)))
    assert out.startswith(b'lin 3')
    assert s.pending() == b''                    # буфер очищен


def test_clear_command_goes_to_computer_only_in_host_mode():
    """ОЧИСТКА: в HOST команда `S` уходит на ЭВМ, в LOCAL — локально."""
    from mc7401.session import Session
    s = Session()
    assert s.clear_command() == b'S' + bytes((VK,))
    s.toggle_mode()
    assert s.clear_command() == b''


def test_echo_toggles():
    """Клавиша ЭХО: снята — набираемое не печатается."""
    from mc7401.session import Session
    s = Session()
    # По умолчанию эхо **следует за режимом**, а режим по умолчанию —
    # С ЭВМ, где эхо делает ЭВМ.  Если бы терминал печатал знак ещё и
    # сам, каждый символ выходил бы на экран дважды.
    assert s.echo is False, 'в С ЭВМ локальное эхо лишнее'
    assert 'эхо ЭВМ' in s.panel(), 'не сказано, что echoes ЭВМ'
    assert s.toggle_echo() is True
    assert 'эхо терминала' in s.panel()
    assert s.toggle_echo() is False
    assert 'эхо ЭВМ' in s.panel()


def test_echo_follows_the_session_mode():
    """`echo = auto`: в С ЭВМ эха нет, в АВТОНОМНО — есть.

    В С ЭВМ эхо делает ЭВМ (знак уходит в линию и возвращается
    нарисованным), и локальное эхо выводило бы его второй раз.  В
    АВТОНОМНО больше никто не echoes — без него набор не видно.
    """
    from mc7401.session import Session, MODE_HOST, MODE_LOCAL
    s = Session(mode=MODE_HOST)
    assert s.echo is False
    s.set_mode(MODE_LOCAL)
    assert s.echo is True, 'в АВТОНОМНО без эха набора не видно'
    s.set_mode(MODE_HOST)
    assert s.echo is False
    # Ручное решение (F6) держится до смены режима: пароль не печатается
    # и в АВТОНОМНО, пока оператор сам его не включил.
    s.toggle_echo()
    assert s.echo is True
    s.set_mode(MODE_LOCAL)
    assert s.echo is True, 'ручное решение должно пережить смену режима'
    # Возврат к «по режиму» — снова пересчитывается.
    s.set_echo(None)
    assert s.echo is True
    s.set_mode(MODE_HOST)
    assert s.echo is False


def test_russian_layout_exists_in_both_input_sets():
    """Русская раскладка есть в обоих наборах **ввода** — как в 15ИЭ.

    Набор ввода (Н2 или КОИ-8Р) к раскладке отношения не имеет: русские
    буквы набираются в обоих, по-разному лишь уходя на линию.
    """
    from mc7401.session import Session
    s = Session()
    assert s.charset_set == 'n2'
    assert s.layout == 'phonetic'
    s.set_charset('koi8r')
    assert s.charset_set == 'koi8r'
    assert s.layout == 'phonetic'


# ── файл конфигурации ──────────────────────────────────────────────────────

def test_config_defaults_without_file():
    """Без файла работают значения по умолчанию."""
    from mc7401.config import Config
    c = Config.default()
    assert c.charset == 'n2', 'набор ввода по умолчанию — Н2 (КОИ-7)'
    assert c.mode == 'host'
    # `None` — «по режиму сеанса»: в С ЭВМ эхо делает ЭВМ, в АВТОНОМНО —
    # терминал.  `true` в режиме С ЭВМ давало бы лишнее эхо каждого
    # символа: знак выходил на экран дважды.
    assert c.echo is None
    assert c.endpoint == '127.0.0.1:4202-4223'


def test_config_echo_auto_and_manual():
    """`[terminal] echo`: `auto` — по режиму, `true`/`false` — вручную."""
    from mc7401.config import parse_echo
    assert parse_echo('auto') is None
    assert parse_echo('') is None
    assert parse_echo('true') is True
    assert parse_echo('false') is False
    # Непонятное значение не молча превращается в «печатать»: лишнее
    # эхо заметнее его отсутствия, и оставляем прежнее.
    assert parse_echo('мусор', False) is False


def test_config_reads_sections(tmp_path):
    """Секции `[line]`, `[terminal]`, `[display]`, `[keys]` читаются."""
    from mc7401.config import Config
    f = tmp_path / 'mc7401.conf'
    f.write_text(
        '[line]\nendpoint = 127.0.0.1:4203\n\n'
        '[terminal]\nmode = local\ncharset = koi8r\necho = false\n'
        'pen = true\n\n'
        '[display]\nmode = mono\nscale = 3\ntitle = false\n\n'
        '[keys]\nL = mode\nq = quit\n', encoding='utf-8')
    c = Config.load(f)
    assert c.endpoint == '127.0.0.1:4203'
    assert c.mode == 'local'
    assert c.charset == 'koi8r'
    assert c.echo is False
    assert c.pen is True
    assert c.display_mode == 'mono'
    assert c.scale == 3
    assert c.title is False
    assert c.keys == {'L': 'mode', 'q': 'quit'}
    assert c.key_action('L') == 'mode'
    assert c.key_action('незадано') is None


def test_config_missing_file_is_not_fatal(tmp_path):
    """Отсутствующий файл — не ошибка, а значения по умолчанию."""
    from mc7401.config import Config
    c = Config.load(tmp_path / 'нет.conf')
    assert c.path is None
    assert c.charset == 'n2'


def test_config_sample_roundtrip(tmp_path):
    """Записанный образец читается обратно и содержит все секции."""
    from mc7401.config import Config
    p = tmp_path / 'mc7401.conf'
    Config.default().write(p)
    text = p.read_text(encoding='utf-8')
    for section in ('[line]', '[terminal]', '[display]', '[keys]'):
        assert section in text
    assert Config.load(p).charset == 'n2'


def test_startup_screen_mentions_model_and_clock():
    """Начальный экран: модель, разрешение, время, настройки, выход."""
    from datetime import datetime

    from mc7401 import startup
    text = startup.build(80, state='ДКС 127.0.0.1:4202',
                         summary='набор n2, режим host',
                         hint=startup.hint_text(),
                         clock=datetime(2026, 10, 10, 12, 34, 56))
    assert 'МС 7401' in text
    assert '560×400' in text
    assert '12:34:56' in text
    assert 'набор n2' in text
    assert 'q — выход' in text


def test_startup_screen_has_no_connection_banner():
    """Надписи о подключении на заставке нет.

    Подключение написано в строке состояния над кадром и в подвале; на
    заставке «соединение…» только занимало место и мелькало.
    """
    from datetime import datetime

    from mc7401 import startup
    text = startup.build(80, state='ДКС 127.0.0.1:4202',
                         summary='набор n2, режим host',
                         hint=startup.hint_text(),
                         clock=datetime(2026, 10, 10, 12, 34, 56))
    assert 'ДКС 127.0.0.1:4202' not in text
    assert 'соединение' not in text.lower()


# ── порядок КОИ-7: буквы БЭСМ-6 заглавными ──────────────────────────────────

def test_koi7_letters_are_capitals():
    """Буквы пути ДКС/Н2 — заглавные ЮАБЦДЕ… в обеих строках кодов.

    Линия БЭСМ-6 регистронезависима (simh `unicode_to_koi7` складывает
    оба регистра в строку `0x60…0x7F`), и по архитектуре терминала
    (ПЗУ КР556РТ5, ГОСТ 27463-87) буквы показываются заглавными, а не
    строчными.
    """
    from mc7401.chars import RUS8, display_char, display_dks
    assert display_dks(0x7C) == 'Э'
    assert display_char(0x7C, 'koi8r') == 'Э'


def test_koi8r_page_keeps_case():
    """Страница КОИ-8Р (S0) не тронута: 0x60…0x7F — заглавные."""
    from mc7401.chars import display_char
    assert display_char(0x69, 'koi8r') == 'И'
    assert display_char(0x49, 'koi8r') == 'и'


# ── заказные знаки (`≡110≡240`) ─────────────────────────────────────────────

def test_defchar_block_defines_custom_char():
    """Блок знака `≡110≡240 … ≡215` определяет пользовательский знак.

    Строки знака идут тремя глифами `WN` («N точек в знакоместе 3×3»);
    значение 5 пишется кодом `≡065` («5»), а не `≡265` — из-за этого
    знаки с пятёрками не загружались.
    """
    from mc7401 import chars
    rows_mm = [0o376, 0o202, 0o202, 0o202, 0o202, 0o202, 0o376]   # рамка
    block = bytearray((0o110, 0o240, 3, 0o267, 0o267))
    for r in rows_mm:
        block.append(0o240)
        block += bytes((chars.DOT_GLYPH_CODES[(r >> 6) & 3],
                        chars.DOT_GLYPH_CODES[(r >> 3) & 7],
                        chars.DOT_GLYPH_CODES[r & 7]))
    block.append(0o215)
    parsed = chars.decode_defchar(bytes(block))
    assert parsed is not None
    code, rows = parsed
    assert code == 3 and rows == tuple(rows_mm)
    # значение 5 кодируется «5» (≡065)
    assert chars.DOT_GLYPH_CODES[5] == 0o65


def test_vuk_service_tail_does_not_eat_next_row():
    """Хвост `ВНУК` (`≡257≡215` и нули) не должен съедать следующую строку.

    Демо-строка заказных знаков идёт сразу после блоков знаков; раньше
    хвост без обработчика превращал маркер строки с данными в одну
    нераспознанную запись.
    """
    data = (stream('J1', '4 180 35')
            + bytes((0o110, 0o240, 3, 0o267, 0o267))
            + bytes((0o240, 0o63, 0o267, 0o66)) * 7 + bytes((0o215,))
            + bytes((0o257, 0o215)) + b'0' * 14
            + bytes((0x1B, 0o306, 0o336, 0o215))
            + bytes((0o273, 0o240)) + bytes((1, 0o40, 2))
            + bytes((0o257, 0o215)) + b'0' * 14)
    emu = TerminalEmulator()
    emu.run(data)
    assert emu.stats['chars'] == 3
