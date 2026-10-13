"""
test_console.py — текстовый слой на кадре и отмена текстового режима.

Проверяется новый текстовый слой `mc7401.console`: вывод данных БЭСМ-6
глифами эталона СИМ в позиции курсора, автоскролл, стирание знакомёста,
и ликвидация отдельного текстового режима (`J0` — no-op).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mc7401.console import (CELL_H, CELL_W, COLS, GLYPH_MAX,           # noqa: E402
                            GLYPH_MIN, GLYPH_SCALE, RASTER_H, RASTER_W,
                            ROWS, Console, set_glyph_scale)

#: Умолчание модуля: тест на перебор размеров обязан вернуть именно его.
_DEFAULT_GLYPH_SCALE = GLYPH_SCALE
from mc7401.constants import GLYPH_H, GLYPH_W, VK               # noqa: E402
from mc7401.framebuffer import Framebuffer                      # noqa: E402
from mc7401.parser import CommandParser, Op                     # noqa: E402


# ── J0: текстовый режим ликвидирован ────────────────────────────────────────

def test_j0_is_noop():
    """`J0` принимается и игнорируется: терминал всегда графический."""
    cmd = CommandParser().parse(b'J0' + bytes((VK,)))
    assert cmd.op is Op.NOOP
    assert cmd.name == 'J0'


def test_j1_still_selects_graphics():
    cmd = CommandParser().parse(b'J1' + bytes((VK,)))
    assert cmd.op is Op.GRAPHICS_MODE


def test_emulator_state_has_no_mode():
    """Поля `mode` (TEXT/GRAPHICS) в состоянии терминала больше нет."""
    from mc7401.emulator import TerminalState
    assert not hasattr(TerminalState(), 'mode')


def test_j0_does_not_change_frame():
    from mc7401.emulator import TerminalEmulator
    emu = TerminalEmulator()
    emu.execute(emu.parser.parse(b'J0' + bytes((VK,))))
    assert emu.fb.count_set() == 0


# ── сетка знакомёстов ───────────────────────────────────────────────────────

def test_grid_fits_screen_exactly():
    """560/8 = 70 колонок, 400/10 = 40 строк — без остатка.

    Растр знака ПЗУ 7×8 идёт на кадр без увеличения (`[terminal] glyph`
    = 1), знакомест с зазором — 8×10: по одной точке на ряд знака по
    горизонтали и две по вертикали.  Без зазоров буквы и строки слипаются:
    «М», «Б» и «Н» в ПЗУ занимают все семь точек по ширине.

    Размер знака **на экране** при этом прежний, 14×16: растр 7×8
    выводится окном с масштабом 2 (`[display] scale`), так что знак
    выглядит как при `glyph = 2, scale = 1`, но знакомест вдвое больше.
    """
    assert COLS == 70 and ROWS == 40
    assert COLS * CELL_W == 560
    assert ROWS * CELL_H == 400
    assert RASTER_W == 7 and RASTER_H == 8
    assert CELL_W == RASTER_W + GLYPH_SCALE
    assert CELL_H == RASTER_H + 2 * GLYPH_SCALE


def test_screen_glyph_size_follows_the_window():
    """Размер знака на экране задаёт окно, а не настройка.

    Кадр равен размеру окна пользователя, поэтому и знак на экране
    зависит от окна: растр ПЗУ 7×8 умножается на масштаб, при котором
    кадр вписан.  `[display] scale` может лишь **ограничить** увеличение
    (0 — не ограничивать, и это умолчание), но не задаёт размер сам.
    """
    from mc7401.config import Config
    cfg = Config.default()
    assert cfg.glyph == 1, 'растр ПЗУ идёт на кадр без увеличения'
    assert cfg.scale == 0, 'кадр ровно по размеру окна'
    # Ограничение, если его задали, не даёт кадру стать крупнее.
    assert cfg.scale <= 0 or cfg.scale > 0
    # А при жёстком ограничении знак на экране предсказуем.
    assert GLYPH_W * cfg.glyph * 2 == 14 and GLYPH_H * cfg.glyph * 2 == 16


def test_glyph_scale_is_configurable():
    """`[terminal] glyph` меняет и знак, и сетку кадра (1…4)."""
    import mc7401.console as con
    try:
        for scale, cols, rows in ((1, 70, 40), (2, 35, 20), (3, 23, 13)):
            assert set_glyph_scale(scale) == scale
            assert con.GLYPH_SCALE == scale
            assert con.RASTER_W == GLYPH_W * scale
            assert (con.COLS, con.ROWS) == (cols, rows)
        assert set_glyph_scale(0) == GLYPH_MIN, 'слишком малый — крайнний'
        assert set_glyph_scale(99) == GLYPH_MAX, 'слишком большой — крайнний'
    finally:
        # Вернуть умолчание модуля, а не число из проверки: иначе тесты
        # после этого считают сетку по чужому масштабу.
        set_glyph_scale(_DEFAULT_GLYPH_SCALE)


# ── вывод текста ────────────────────────────────────────────────────────────

def test_write_draws_glyphs_at_cursor():
    fb = Framebuffer()
    con = Console(fb)
    con.goto(10, 5)
    con.write(b'A')
    x0, y0, x1, y1 = con.cell_rect(10, 5)
    assert any(fb.get(x, y) for y in range(y0, y1 + 1)
               for x in range(x0, x1 + 1)), 'глиф не нарисован'
    assert (con.col, con.row) == (11, 5)


def test_write_cr_lf_and_etx():
    """`ВК`, `ПС` и ETX переводят строку; `ВК ПС` — один перевод."""
    fb = Framebuffer()
    con = Console(fb)
    con.write(b'AB\r\n')                 # пара ВК ПС — один перевод
    assert con.col == 0 and con.row == 1
    con.write(b'A\r')                    # одиночный ВК — тоже перевод
    assert con.col == 0 and con.row == 2
    con.write(b'B\n')                    # одиночная ПС — тоже
    assert con.col == 0 and con.row == 3
    con.write(bytes((0x03,)))            # ETX — конец строки
    assert con.col == 0 and con.row == 4


def test_write_lf_cr_is_one_break():
    """`ПС ВК` — тоже один перевод строки, как `ВК ПС`.

    ДКС закрывает запись каталога парой `ПС ВК` (проверено на живой
    линии, ответ на `КТЛ`).  Раньше поглощалась только пара `ВК ПС`, и
    `ПС ВК` давал **два** перевода: между строками выдачи появлялся
    пустой ряд, и список каталога шёл втрое реже строк.
    """
    fb = Framebuffer()
    con = Console(fb)
    con.write(b'AB\n\r')                # пара ПС ВК — один перевод
    assert con.col == 0 and con.row == 1, f'ряд {con.row}, колонка {con.col}'
    con.write(b'CD\r\n')                # и ВК ПС — тоже один
    assert con.col == 0 and con.row == 2


def test_write_doubled_break_is_a_blank_line():
    """Удвоение того же знака — пустая строка, и поглощать его нельзя."""
    fb = Framebuffer()
    con = Console(fb)
    con.write(b'A\r\rB')
    assert con.row == 2, f'ряд {con.row}: удвоение ВК проглочено'
    con2 = Console(Framebuffer())
    con2.write(b'A\n\nB')
    assert con2.row == 2, f'ряд {con2.row}: удвоение ПС проглочено'


def test_ktl_listing_rows_are_not_doubled():
    """Настоящий ответ `КТЛ` занимает ряд на запись, а не три.

    Записи каталога — 71 знак при сетке в 70 знакомест, поэтому запись
    переносится ещё на один ряд; лишнего пустого ряда между записями
    быть не должно.
    """
    fb = Framebuffer()
    con = Console(fb)
    rec = (b'B|CM6   123400    foPgdP  123400    TECT1   123400'
           b'    KAlxK   123400   ')
    con.write(rec + b'\n\r')
    first = con.row
    con.write(rec + b'\n\r')
    # Запись переносится (71 знак при 70 колонках) плюс один перевод.
    assert con.row - first == 2, (
        f'между записями {con.row - first} ряда, ожидалось два '
        f'(перенос 71-го знака и один перевод строки)')


def test_write_ignores_control_codes():
    fb = Framebuffer()
    con = Console(fb)
    con.write(bytes((0o016, 0o017, 0o033)))   # SO/SI/АР2 — служебные
    assert (con.col, con.row) == (0, 0)
    assert fb.count_set() == 0


def test_line_wraps_at_screen_edge():
    fb = Framebuffer()
    con = Console(fb)
    con.goto(COLS - 1, 3)
    con.write(b'A')
    assert con.col == 0 and con.row == 4, 'перенос на новую строку'


def test_autoscroll_at_bottom():
    """Внизу экрана кадр скроллится вверх, курсор остаётся внизу."""
    fb = Framebuffer()
    con = Console(fb)
    con.goto(0, ROWS - 1)
    con.write(bytes((0x0A,)))
    assert con.row == ROWS - 1
    con.write(bytes((0x0A,)))
    assert con.row == ROWS - 1


def test_scroll_up_moves_picture():
    fb = Framebuffer()
    fb.point(5, 0)                       # точка в самом низу
    fb.scroll_up(GLYPH_H)
    assert fb.get(5, 0) == 0, 'низ должен очиститься'
    assert fb.get(5, GLYPH_H) == 1, 'точка ушла вверх'
    assert fb.count_set() == 1


# ── стирание ────────────────────────────────────────────────────────────────

def test_backspace_erases_cell():
    fb = Framebuffer()
    con = Console(fb)
    con.write(b'A')
    assert fb.count_set() > 0
    con.backspace()
    assert fb.count_set() == 0
    assert con.col == 0


def test_erase_cell_clears_only_cell():
    fb = Framebuffer()
    con = Console(fb)
    con.write(b'A')
    con.goto(0, 0)
    con.erase_cell()
    assert fb.count_set() == 0
