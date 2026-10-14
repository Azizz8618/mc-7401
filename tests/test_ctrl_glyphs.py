"""
test_ctrl_glyphs.py — «УПР.СИМВ»: образные знаки управляющих кодов.

Режим мигания Видеотона-340, как в 15ИЭ (`ie15emu`): нераспознанный
управляющий код показывается знаком `код | 0x40` и мигает.  Выключен по
умолчанию, и в выключенном виде код невидим и места не занимает — выдача
читается как обычно.

Эталон, ради которого всё затевалось (`ie15emu/screen.py`):

    if a & ATTR_CTRL:
        out.append(display_char(code | 0x40, "n0") if self.show_ctrl
                   else " ")

и `parser.py`: `НУС` не выводится вовсе и каретки не двигает.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mc7401 import console as con                                  # noqa: E402
from mc7401.framebuffer import Framebuffer                         # noqa: E402


def make(row: int = 20, *, show: bool = False):
    """Консоль на пустом кадре, курсор в начале строки `row`."""
    fb = Framebuffer(con.SCREEN_WIDTH, con.SCREEN_HEIGHT)
    c = con.Console(fb)
    c.col, c.row, c.show_ctrl = 0, row, show
    return c, fb


def line(c: Console, fb: Framebuffer) -> str:
    """Знаки строки `c.row`, как их видит оператор."""
    return con.read_row(fb, con.cell_y(c.row)).rstrip(' ')


def test_ctrl_code_is_invisible_by_default():
    """Выключено: код не печатается и знакоместа не занимает.

    Это не «видно пустым знакоместом», а полное отсутствие: иначе
    разбивка выдачи уехала бы везде, где в потоке есть управляющие
    коды, а их в потоке с ДКС полно.
    """
    c, fb = make()
    c.write(b'A\x01B')
    assert line(c, fb) == 'AB'
    assert c.ctrl_cells() == []


def test_ctrl_code_shows_as_code_or_0x40():
    """Включено: `0x01` → `A`, `0x1F` → `_` — знак `код | 0x40`."""
    c, fb = make(show=True)
    c.write(b'\x01\x1f')
    assert line(c, fb) == 'A_'
    assert c.ctrl_cells() == [(0, 20, 0x01), (1, 20, 0x1F)]


def test_ctrl_code_takes_a_cell_when_shown():
    """Показанный знак занимает знакоместо и сдвигает каретку.

    Иначе следующий знак затирал бы образный: показать было бы нечем.
    """
    c, fb = make(show=True)
    c.write(b'A\x01B')
    assert line(c, fb) == 'AAB'
    assert c.col == 3


def test_nul_never_shown_even_when_enabled():
    """`НУС` — пустой код: не знак терминала, каретку не двигает."""
    c, fb = make(show=True)
    c.write(b'X\x00Y')
    assert line(c, fb) == 'XY'
    assert c.ctrl_cells() == []


def test_ctrl_cells_follow_the_frame_on_scroll():
    """Образные знаки едут вместе с кадром при автоскролле.

    Мигание гасит и зажигает знаки по списку; если бы список стоял на
    месте, знаки мигали бы не там, где нарисованы.
    """
    c, fb = make(row=con.ROWS - 1, show=True)
    c.write(b'\x01')
    assert c.ctrl_cells() == [(0, con.ROWS - 1, 0x01)]
    c.write(b'\n')                       # автоскролл
    assert c.ctrl_cells() == [(0, con.ROWS - 2, 0x01)], \
        'образный знак не уехал с кадром'


def test_ctrl_cell_scrolled_off_the_top_is_dropped():
    """Знак, ушедший за верхний край кадра, из списка выпадает.

    Иначе мигание гасило бы и зажигало бы знак в ряду `-1` — за кадром,
    где уже ничего нет, но координаты считались бы от него.
    """
    c, fb = make(row=con.ROWS - 1, show=True)
    c.write(b'\x01')
    for _ in range(con.ROWS):
        c.write(b'\n')
    assert c.ctrl_cells() == [], 'знак за верхним краем остался в списке'
    assert all(row >= 0 for _, row, _ in c.ctrl_cells())


@pytest.mark.parametrize('lit', [True, False])
def test_blink_erases_and_redraws_only_the_glyph(lit):
    """Мигание: растр знака гасится и зажигается обратно."""
    c, fb = make(show=True)
    c.write(b'\x01')
    assert line(c, fb) == 'A'
    c.repaint_ctrl(lit=not lit)
    expect = '' if lit else 'A'
    assert line(c, fb) == expect
    c.repaint_ctrl(lit=lit)
    assert line(c, fb) == ('A' if lit else '')


def test_blink_leaves_neighbouring_cells_alone():
    """Мигание не выедает соседние знаки.

    Стирается растр образного знака, а не всё знакоместо: иначе поверх
    кода попала бы графика и мигание выгрызало бы куски картинки.
    """
    c, fb = make(show=True)
    c.write(b'AB\x01C')
    assert line(c, fb) == 'ABAC'
    c.repaint_ctrl(lit=False)
    assert line(c, fb) == 'AB C', 'соседние знаки должны уцелеть'


def test_clearing_forgets_the_cells():
    """Выключение режима забывает список и гасит знаки.

    Иначе выключенный режим продолжал бы перерисовывать знаки, которых
    на кадре уже нет, и список рос бы сам по себе.
    """
    c, fb = make(show=True)
    c.write(b'\x01\x02')
    assert c.ctrl_cells()
    c.repaint_ctrl(lit=False)
    c.clear_ctrl()
    assert c.ctrl_cells() == []
    assert line(c, fb) == ''


def test_mode_is_off_in_a_fresh_console():
    """По умолчанию режим выключен — как в 15ИЭ."""
    assert con.Console(Framebuffer(con.SCREEN_WIDTH,
                                   con.SCREEN_HEIGHT)).show_ctrl is False


def test_ctrl_glyph_is_the_rom_sign():
    """Образный знак рисуется тем же ПЗУ, что и всякий знак.

    `read_row` читает кадр обратно по битам растра; если бы знак уходил
    мимо `put_glyph`, его бы не было видно и в эталоне.
    """
    c, fb = make(show=True)
    c.write(b'\x05')
    assert line(c, fb) == 'E', 'образный знак не читается обратно'


# ── маршрутизация пакета ───────────────────────────────────────────────────
#
# Без этого образные знаки были бы недостижимы: пакет с управляющим кодом
# вне `ВК`/`ПС`/`ЗБ`/`ТАБ` целиком уходил в разбор графики, и текстовый
# слой их не видел.

def test_ctrl_packet_goes_to_graphics_by_default():
    """Выключен режим — пакет с управляющим кодом уходит в графику."""
    from mc7401.window import _is_commands
    assert _is_commands(b'STROKA\x01KONEC\r\n')
    assert _is_commands(b'STROKA\x00KONEC\r\n')


def test_ctrl_packet_goes_to_text_when_mode_is_on():
    """Включён режим — тот же пакет идёт на экран как текст."""
    from mc7401.window import _is_commands
    assert not _is_commands(b'STROKA\x01KONEC\r\n', show_ctrl=True)
    assert not _is_commands(b'STROKA\x00KONEC\r\n', show_ctrl=True)


def test_graphics_frames_stay_graphics_in_any_mode():
    """Графика опознаётся по рамке, и режим на неё не влияет.

    Проверено на всех эталонных потоках: рамка `АР2 'F'` есть в 19 из
    19, а значит запасной критерий «управляющий код» для графики не
    нужен и может быть отключён без риска.
    """
    import pathlib
    from mc7401.window import _is_commands
    root = pathlib.Path(__file__).resolve().parent.parent / 'golden' / 'streams'
    streams = sorted(root.glob('*.bin'))
    assert streams, 'эталонные потоки не найдены'
    for path in streams:
        data = path.read_bytes()
        assert _is_commands(data, show_ctrl=True), (
            f'{path.name}: графика ушла в текст при включённом режиме')
    assert not _is_commands(b'ABRAKADABRA\r', show_ctrl=True)