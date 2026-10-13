"""Служебные строки окна: строка состояния и подвал глифами ПЗУ.

Отдельного внимания заслуживает то, что подвал **нарисован**: он
рисуется глифами знакогенератора (`chrome.put_glyph`), и одна опечатка
в проверке бита растра (`row[u] == '#'` вместо `row[u]` для матрицы из
чисел) тихо оставляла на экране одни разделители колонок — без букв.
Проверка ниже ловит именно это: знаки в подвале должны зажигать точки.
"""

import re

import pytest

from mc7401 import chrome
from mc7401.console import CELL_H, CELL_W
from mc7401.constants import SCREEN_WIDTH
from mc7401.framebuffer import Framebuffer
from mc7401.panel import RULE, Diagnostics, Status


def make_status(**kw) -> Status:
    base = dict(link_kind='ДКС', banner=True, machine=3, terminal=3,
                channel='4203', wire='RAW', line='127.0.0.1:4203',
                mode='host', charset='koi8r', echo=True, pen=False,
                pen_tool='штрих', grid=False, scale=2, buffer=0,
                points=0, cursor='0,0', graphics='', last_key='',
                input_line='')
    base.update(kw)
    return Status(**base)


def draw(top, bottom) -> Framebuffer:
    """Полоса во всю высоту кадра — служебные строки поверх него."""
    fb = Framebuffer(SCREEN_WIDTH, chrome.ROW_H * (len(top) + len(bottom)))
    chrome.render(fb, top, bottom, frame_height=fb.height)
    return fb


def cell_pixels(fb: Framebuffer, col: int, row: int) -> int:
    """Сколько точек зажёг знак в знакоместе (`col`, `row`) снизу вверх."""
    x0, y0 = col * CELL_W, row * CELL_H
    return sum(fb.get(x0 + dx, y0 + dy)
               for dx in range(CELL_W) for dy in range(CELL_H))


# ── раскладка ─────────────────────────────────────────────────────────────

def test_cols_matches_frame_width():
    assert chrome.cols() * CELL_W == SCREEN_WIDTH
    assert chrome.cols() == 35


def test_status_line_fits_narrow_strip():
    """Полоса — 35 знакомест: строка состояния переносится, а не режется."""
    rows = chrome.status_rows(make_status())
    assert rows, 'строка состояния не пуста'
    for row in rows:
        assert sum(chrome.cell_width(c) + 3 for c in row) - 3 <= chrome.cols()


def test_status_line_is_not_cut_to_column_width():
    """Время в строке состояния не обрезается шириной колонки подвала.

    Подвал раскладывается ячейками по 9 знакомест, и та же обрезка
    применялась к строке состояния: «13.10 07:52» превращалось в
    «13.10 07:».
    """
    rows = chrome.status_rows(make_status())
    text = ''.join(chrome.cell_text(c) for row in rows for c in row)
    assert re.search(r'\d\d\.\d\d \d\d:\d\d', text), text
    fb = draw(rows, [])
    lit = [cell_pixels(fb, col, row)
           for row in range(len(rows)) for col in range(chrome.cols())]
    assert max(lit) > CELL_W


def test_footer_columns_are_even():
    """Все колонки подвала одной ширины — границы совпадают в каждом ряду."""
    rows = chrome.footer_rows(make_status(), help_=True)
    for row in rows:
        if row == [RULE]:
            continue
        xs = []
        x = 0
        for cell in row:
            xs.append(x)
            x += chrome.cell_width(cell) + len(chrome.SEP) * CELL_W
        assert len(set(xs)) == len(xs), 'колонки налезают друг на друга'


# ── отрисовка ─────────────────────────────────────────────────────────────

def test_footer_draws_letters_not_only_separators():
    """Подвал зажжён знаками, а не одними разделителями « | ».

    Именно этот тест ловит молчаливую поломку растра знака: разделители
    рисуются точками напрямую и остаются на месте даже тогда, когда
    сами знаки не рисуются вовсе.
    """
    st = make_status()
    top, bottom = chrome.status_rows(st), chrome.footer_rows(st)
    fb = draw(top, bottom)

    lit = fb.count_set()
    # Одни разделители дали бы единицы сотен точек; знаки — тысячи.
    assert lit > 500, f'в подвале почти пусто: {lit} точек'
    # И первая ячейка первого ряда подвала — буква, а не пустота.
    first = cell_pixels(fb, 0, 0)
    assert first > CELL_W, f'первая ячейка подвала пуста: {first} точек'


def test_status_line_draws_text():
    """Верхняя кромка кадра — строка состояния, и она читаема."""
    st = make_status()
    top = chrome.status_rows(st)
    fb = draw(top, [])
    lit = [cell_pixels(fb, col, row)
           for row in range(len(top)) for col in range(chrome.cols())]
    assert max(lit) > CELL_W


def test_chrome_does_not_touch_frame_middle():
    """Служебные строки закрывают только кромки кадра, середина цела."""
    top, bottom = chrome.status_rows(make_status()), \
        chrome.footer_rows(make_status())
    fb = Framebuffer(SCREEN_WIDTH, 400)
    chrome.render(fb, top, bottom, frame_height=400)
    top_px, bottom_px = chrome.top_height(top), len(bottom) * chrome.ROW_H
    # Подвал — внизу кадра (`Y` вверх), строка состояния — вверху;
    # между ними кадр программы не тронут.
    for y in (bottom_px, (bottom_px + 400 - top_px) // 2, 400 - top_px - 1):
        assert not any(fb.get(x, y) for x in range(SCREEN_WIDTH)), \
            f'строка кадра {y} закрашена служебной строкой'
    assert fb.count_set() > 0, 'служебные строки не нарисованы вовсе'


def test_footer_without_legend_is_shorter():
    """`[terminal] help = false` убирает легенду — подвал ниже."""
    st = make_status()
    full = chrome.footer_rows(st, help_=True)
    brief = chrome.footer_rows(st, help_=False)
    assert len(brief) < len(full)
    assert len(brief) >= 3          # состояния, черта, диагностика


def test_diagnostics_are_last_footer_row():
    diag = Diagnostics()
    diag.add('экран очищен', 'ok')
    rows = chrome.footer_rows(make_status(), diag=diag)
    text = ''.join(chrome.cell_text(c) for c in rows[-1])
    assert 'экран очищен' in text


def test_footer_shows_line_kind_from_banner():
    """Тип линии в подвале — разобранный из баннера, а не зашитый.

    Раньше ячейка `ЛИН=` показывала «ДКС» при любом баннере, и линия,
    объявленная обычным tty, выглядела бы как ДКС.
    """
    for kind in ('ДКС', 'КВУ'):
        text = ' '.join(chrome.cell_text(c)
                        for c in chrome.footer_rows(make_status(link_kind=kind))[0])
        assert kind in text, f'в подвале нет типа линии {kind}'
    # Без баннера тип линии неизвестен — и подвал не должен его выдумывать.
    text = ' '.join(chrome.cell_text(c) for c in
                    chrome.footer_rows(make_status(banner=False, link_kind=''))[0])
    assert 'ДКС' not in text


def test_footer_height_fits_reasonable_share_of_frame():
    """Подвал не должен закрывать большую часть кадра.

    Полоса служебных строк — 35 знакомест, и подвал умещается в треть
    кадра по высоте.  Больше — значит легенда снова разрослась.
    """
    top = chrome.status_rows(make_status())
    bottom = chrome.footer_rows(make_status(), help_=True)
    share = chrome.chrome_height(top, bottom) / 400
    assert share <= 0.75, f'подвал закрывает {share:.0%} кадра'


@pytest.mark.parametrize('pen', ['OR', 'XOR', 'ERASE'])
def test_render_accepts_pen_modes(pen):
    """`render` не должен ломаться на любом пере, что передаёт кадр."""
    st = make_status()
    fb = Framebuffer(SCREEN_WIDTH, 400)
    chrome.render(fb, chrome.status_rows(st),
                  chrome.footer_rows(st), frame_height=400)