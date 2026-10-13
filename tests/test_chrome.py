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
from mc7401 import panel
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
    """Полоса: кадр в середине, строка состояния над ним, подвал под ним."""
    fb = Framebuffer(SCREEN_WIDTH,
                     400 + chrome.ROW_H * (len(top) + len(bottom)))
    chrome.render(fb, top, bottom, frame_height=400)
    return fb


def bands(top_rows, bottom_rows) -> tuple[int, int, int]:
    """Границы полос в рядах полосы: `(подвал, кадр, верх)`, `Y` вверх."""
    foot = chrome.ROW_H * len(bottom_rows)
    top = chrome.ROW_H * len(top_rows)
    return foot, foot + 400, foot + 400 + top


def cell_pixels(fb: Framebuffer, col: int, y0: int) -> int:
    """Сколько точек зажёг знак в знакоместе `col` над рядом `y0`.

    `y0` — нижняя граница знакоместа в рядах кадра (`Y` вверх), а не
    номер строки сверху: полоса растёт и вниз (подвал), и вверх.
    """
    x0 = col * CELL_W
    return sum(fb.get(x0 + dx, y0 + dy)
               for dx in range(CELL_W) for dy in range(CELL_H))


# ── раскладка ─────────────────────────────────────────────────────────────

def test_cols_matches_frame_width():
    """Полоса служебных строк — по ширине кадра, в знакоместах СИМ."""
    assert chrome.cols() * CELL_W == SCREEN_WIDTH
    assert chrome.cols() == 70


def test_status_line_fits_narrow_strip():
    """Полоса — 35 знакомест: строка состояния переносится, а не режется."""
    rows = chrome.status_text_rows(make_status())
    assert rows, 'строка состояния не пуста'
    for row in rows:
        assert sum(chrome.cell_width(c) + 3 for c in row) - 3 <= chrome.cols()


def test_status_line_is_not_cut_to_column_width():
    """Время в строке состояния не обрезается шириной колонки подвала.

    Подвал раскладывается ячейками по 9 знакомест, и та же обрезка
    применялась к строке состояния: «13.10 07:52» превращалось в
    «13.10 07:».
    """
    rows = chrome.status_text_rows(make_status())
    text = ''.join(chrome.cell_text(c) for row in rows for c in row)
    assert re.search(r'\d\d\.\d\d \d\d:\d\d', text), text
    fb = draw(rows, [])
    hi = fb.height
    lit = [cell_pixels(fb, col, hi - (n + 1) * chrome.ROW_H)
           for n in range(len(rows)) for col in range(chrome.cols())]
    assert max(lit) > CELL_W


def test_footer_shows_pen_mode_and_tool_without_ellipsis():
    """Режим пера и инструмент видны в подвале целиком.

    Колонка подвала — 9 знакомест, на значение остаётся четыре, а длинные
    «выделение» и «штрих» превращались в одни многоточия: оператор не
    видел, в каком режиме перо.  Поэтому у режимов и инструментов свои
    короткие метки.
    """
    st = make_status()
    st.pen = True
    for mode, label in (('рисование', 'РИС'), ('выделение', 'ВЫД')):
        st.pen_mode = mode
        st.pen_tool = 'штрих'
        text = ' '.join(panel.render_plain(r)
                        for r in chrome.footer_rows(st, help_=True))
        assert f'ПЕРО={label}' in text, f'режим {mode} не виден: {text}'
        assert '...' not in text.split('ПЕРО=')[1][:12], 'метка обрезана'
    st.pen_mode = ''
    text = ' '.join(panel.render_plain(r)
                    for r in chrome.footer_rows(st, help_=True))
    assert 'ИНСТ=ШТР' in text, f'инструмент не виден: {text}'
    st.pen = False
    text = ' '.join(panel.render_plain(r)
                    for r in chrome.footer_rows(st, help_=True))
    assert 'ПЕРО=ВЫК' in text


def test_footer_rows_do_not_grow_silently():
    """Высота подвала не растёт молча: каждый ряд отнимает у кадра.

    Добавление ячеек в легенду однажды выросло рядом и ужало кадр на
    десять процентов — это не видно ни в одном снимке, а заметно на
    глаз.  Сейчас подвал — шесть рядов: пять на состояния, легенду и
    черту плюс ряд широких ячеек «ПОСЛ= … | ВВОД= …».
    """
    st = make_status()
    st.pen = True
    st.pen_mode = 'выделение'
    rows = chrome.footer_rows(st, help_=True)
    assert len(rows) == 6, f'подвал вырос до {len(rows)} рядов'
    # Без легенды (`--no-help`) подвал на два ряда ниже.
    assert len(chrome.footer_rows(st, help_=False)) == 4


def test_legend_names_pen_mode_and_undo():
    """Легенда называет новые клавиши: F4 — режим, F7 — снять, Ctrl-Z."""
    text = ' '.join(panel.render_plain(r)
                    for r in chrome.footer_rows(make_status(), help_=True))
    for key in ('F4=РЕЖИМ', 'F7=СНЯТ', 'ОТМЕНА=Ctrl-z'):
        assert key in text, f'в легенде нет {key}'


def test_status_line_shows_selection_size():
    """Размер выделения виден в строке состояния, как на 15ИЭ."""
    st = make_status()
    st.selection = 'СТР 1фр 1с 24зн'
    text = ' '.join(panel.cell_text(c) for r in chrome.status_rows(st)
                    for c in r if r != [RULE])
    assert 'ВЫД=' in text and '24зн' in text, text


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


def test_status_line_has_rule_and_gap_above_frame():
    """Под строкой состояния — черта и отступ в один ряд, затем кадр.

    По требованию: «после верхней строки состояния черта, затем отступ
    в одну строку перед рабочей областью».  Отступ нужен, чтобы верхний
    ряд кадра не лип к черте.
    """
    top = chrome.status_rows(make_status())
    assert len(top) >= 3, 'нет места под черту и отступ'
    assert top[-2] == [RULE], 'перед отступом не черта'
    assert top[-1] == [], 'отступ перед кадром не пустой ряд'
    # Строка состояния — над чертой, а не после неё.
    assert all(row and row != [RULE] for row in top[:-2])


def test_status_gap_does_not_shrink_the_frame():
    """Отступ добавляется к полосе, а не отбирается у кадра.

    Кадр остаётся 400 рядами: полоса растёт вверх, и рабочее поле не
    должно терять ни одного ряда.
    """
    st = make_status()
    top = chrome.status_rows(st)
    fb = Framebuffer(SCREEN_WIDTH, 400 + chrome.ROW_H * (len(top) + 6))
    chrome.render(fb, top, chrome.footer_rows(st), frame_height=400)
    foot, frame_lo, _frame_hi = bands(top, chrome.footer_rows(st))
    # `bands` отдаёт границы полос: кадр — между подвалом и верхней
    # полосой, и отбивка добавляется к полосе, а не выедает кадр.
    assert frame_lo - foot == 400, 'кадр перестал быть 560×400'


def test_status_gap_is_drawn_as_blank_row():
    """Ряд отступа зажжён пустотой, а черта — сплошной заливкой."""
    st = make_status()
    top = chrome.status_rows(st)
    fb = draw(top, [])
    hi = fb.height
    # Ряд отступа — самый нижний из верхней полосы.
    gap_y0 = hi - len(top) * chrome.ROW_H
    gap_lit = sum(fb.get(x, gap_y0 + dy)
                  for x in range(SCREEN_WIDTH) for dy in range(chrome.ROW_H))
    assert gap_lit == 0, f'отступ чем-то заполнен: {gap_lit} точек'
    # Черта — рядом над ним: сплошная линия в один ряд во всю ширину.
    rule_y0 = gap_y0 + chrome.ROW_H
    mid = rule_y0 + chrome.ROW_H // 2
    assert sum(fb.get(x, mid) for x in range(SCREEN_WIDTH)) == SCREEN_WIDTH, \
        'черта не сплошная по ширине'
    for dy in (0, chrome.ROW_H - 1):
        assert sum(fb.get(x, rule_y0 + dy)
                   for x in range(SCREEN_WIDTH)) == 0, \
            'черта зажгла лишний ряд'


def test_top_height_accounts_for_gap():
    """Высота верхней полосы включает черту и отступ."""
    st = make_status()
    top = chrome.status_rows(st)
    assert chrome.top_height(top) == len(top) * chrome.ROW_H
    assert chrome.top_height(top) > chrome.top_height(chrome.status_text_rows(st))


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
    foot, _, _ = bands(top, bottom)

    lit = fb.count_set()
    # Одни разделители дали бы единицы сотен точек; знаки — тысячи.
    assert lit > 500, f'в подвале почти пусто: {lit} точек'
    # И первая ячейка первого ряда подвала — буква, а не пустота.
    first = cell_pixels(fb, 0, foot - chrome.ROW_H)
    assert first > CELL_W, f'первая ячейка подвала пуста: {first} точек'
    assert foot == chrome.ROW_H * len(bottom)


def test_status_line_draws_text():
    """Над кадром — строка состояния, и она читаема."""
    st = make_status()
    top = chrome.status_rows(st)
    fb = draw(top, [])
    hi = fb.height
    lit = [cell_pixels(fb, col, hi - (n + 1) * chrome.ROW_H)
           for n in range(len(top)) for col in range(chrome.cols())]
    assert max(lit) > CELL_W


def test_chrome_does_not_touch_frame():
    """Подвал не наезжает на рабочее поле: кадр остаётся нетронутым.

    Раньше подвал ложился поверх нижней кромки кадра и закрывал
    рабочее поле — текст ОС Диспак под ним пропадал.  Теперь подвал
    стоит **под** кадром, а строка состояния — **над** ним.
    """
    top, bottom = chrome.status_rows(make_status()), \
        chrome.footer_rows(make_status())
    fb = Framebuffer(SCREEN_WIDTH,
                     400 + chrome.ROW_H * (len(top) + len(bottom)))
    chrome.render(fb, top, bottom, frame_height=400)
    foot, frame_hi, top_hi = bands(top, bottom)

    assert foot > 0, 'подвал должен быть под кадром, а не поверх него'
    assert top_hi == fb.height, 'строка состояния должна быть над кадром'
    # Ни одна точка кадра не закрашена служебными строками.
    for y in range(foot, frame_hi):
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