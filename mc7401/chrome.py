"""
chrome.py — строка состояния и подвал окна глифами СИМ.

На МС 7401 нет ни одного знака, нарисованного не его собственным
знакогенератором: графика программы, текст ОС Диспак, рамка курсора и —
следовательно — служебные строки самого эмулятора.  Раньше строка
состояния и подвал выводились моноширинным шрифтом окна, и у экрана
получалось две типографики: знаки ПЗУ и чужой системный шрифт.  Теперь
и они рисуются растрами знакогенератора КР556РТ5 тем же знаком, что и
текстовый слой кадра (по умолчанию 14×16 точек).

СТРУКТУРА — КАК В 15ИЭ. Ячейки те же самые, что и в консольном `tui.py`
и в эталоне 15ИЭ-00-013 (`ie15/ie15emu/screen.py`): «метка=значение» в
**ровных колонках** с разделителями « | », ряды состояний отбиты от
легенды чертой, легенда идёт группами.  Рендеринг меняется один —
цвет ANSI на монохромный растр, потому что кадр у МС 7401 одноцветный.
Изменять структуру подвала ради «красивого» вида здесь нельзя: она
воспроизводит то, что видит оператор настоящего терминала.

РАСКЛАДКА. Окно по умолчанию развёрнуто на весь экран, а кадр вписан в
него вместе со служебными строками: строка состояния — **над** кадром
(как на 15ИЭ), подвал — **под** ним и привязан к нижней границе экрана.
Подвал не наезжает на кадр: рабочее поле видно целиком, текст ОС не
пропадает под служебными рядами.

Знак служебных строк — тот же, что и в кадре, и масштабируется вместе
с ним, поэтому строки читаются так же, как ответы ОС Диспак.  Сократить
подвал можно настройкой `[terminal] help = false` — легенда клавиш
исчезает, состояния остаются.
"""

from __future__ import annotations

from typing import Optional

from .console import CELL_H, CELL_W, put_glyph
from .constants import PEN_OR, SCREEN_WIDTH
from .framebuffer import Framebuffer
from .panel import (Cell, Diagnostics, Row, RULE, SEP, Status, cell_text,
                    cell_width, columns, footer as panel_footer, plan,
                    status_line as panel_status_line)

#: Высота строки служебных строк — та же, что у текстового слоя кадра.
ROW_H = CELL_H
#: Ширина ячейки подвала в знакоместах СИМ.  Полоса — 35 знакомест, и
#: при ширине 9 в ряд помещаются три: подвал получается втрое короче по
#: высоте, а каждый его ряд закрывает меньше кадра.  Все колонки при
#: этом ровные — раскладку считает `panel.plan`.
CELL_COLS = 9

#: Ряд-черта: рисуется заливкой, а не знаком — в знакогенераторе нет
#: знака горизонтальной линии во всю ширину знакоместа.  Маркер тот же,
#: что и в `panel.RULE`, поэтому подвал собирается один раз.


def cols() -> int:
    """Сколько знакомест в строке служебных строк.

    Считается на лету: размер знака задаётся настройкой `[terminal] glyph`
    (`console.set_glyph_scale`), и полоса обязана с ним совпадать.
    """
    return max(1, SCREEN_WIDTH // CELL_W)


# ── содержимое ─────────────────────────────────────────────────────────────

def status_rows(st: Status) -> list[Row]:
    """Строка состояния линии — ряд ячеек, перенесённый по ширине полосы.

    Порядок и содержание — как в эталоне 15ИЭ: тип линии, ЭВМ, терминал,
    канал, режим, время.  Без баннера — «ПРОВЕРЬ ЛИНИЮ!», а не выдуманный
    «КВУ» (тип линии разбирает `link.parse_greeting`).

    Форма короткая (`full=False`): полоса — 35 знакомест, полная строка
    с названием модели и полными датой-временем занимала бы три ряда и
    закрывала верх кадра.
    """
    cells: list[Cell] = list(panel_status_line(st, full=False))
    width = cols()
    rows: list[Row] = []
    cur: Row = []
    used = 0
    for cell in cells:
        w = cell_width(cell)
        if cur and used + len(SEP) + w > width:
            rows.append(cur)
            cur, used = [], 0
        used += (len(SEP) if cur else 0) + w
        cur.append(cell)
    if cur:
        rows.append(cur)
    return rows


def footer_rows(st: Status, *, help_: bool = True,
                diag: Optional[Diagnostics] = None) -> list[Row]:
    """Подвал: состояния, черта и легенда — структура 15ИЭ.

    Ячейки и их раскладка — те же, что у консольного `tui.py`, только
    колонки уже: полоса служебных строк — 35 знакомест СИМ, а не окно
    терминала.  Диагностика идёт последним рядом **без** второй черты:
    каждый ряд подвала закрывает часть кадра, и лишняя черта стоила бы
    двадцати точек экрана.
    """
    width = cols()
    rows = panel_footer(st, width=width, help_=help_, colw=CELL_COLS)
    if diag is not None and diag.last:
        rows.extend(columns(diag.lines(1), width, colw=CELL_COLS))
    return rows


def chrome_height(top: list[Row], bottom: list[Row]) -> int:
    """Высота полосы служебных строк в точках — оба конца вместе."""
    return (len(top) + len(bottom)) * ROW_H


def top_height(top: list[Row]) -> int:
    """Высота строки состояния над кадром в точках."""
    return len(top) * ROW_H


# ── отрисовка ──────────────────────────────────────────────────────────────

def render_status(fb: Framebuffer, top: list[Row]) -> None:
    """Строка состояния — **над** кадром, как на 15ИЭ.

    В полосе `fb` кадр занимает нижние `SCREEN_HEIGHT` рядов, а строка
    состояния — верхние `len(top) * ROW_H`.  Ряды нумеруются `Y` вверх,
    поэтому первый ряд строки состояния — самый верхний.
    """
    h = len(top) * ROW_H
    fb.erase_rows(fb.height - h, fb.height)
    for n, row in enumerate(top):
        _row(fb, row, fb.height - (n + 1) * ROW_H, pad=False)


def render_footer(fb: Framebuffer, bottom: list[Row]) -> None:
    """Подвал — под кадром, привязан к нижней границе полосы.

    Первый ряд подвала — верхний из отведённых ему: нарисованный
    снизу, он читался бы перевёрнутым (сначала легенда, потом
    состояния).
    """
    h = len(bottom) * ROW_H
    fb.erase_rows(0, h)
    for n, row in enumerate(bottom):
        _row(fb, row, h - (n + 1) * ROW_H, pad=True)


def render(fb: Framebuffer, top: list[Row], bottom: list[Row],
           *, frame_height: int) -> None:
    """Нарисовать обе служебные строки вокруг кадра.

    `fb` — полоса шириной кадра: подвал занимает в ней ряды
    `0 … footer_px`, кадр — `footer_px … footer_px + frame_height`,
    строка состояния — над кадром.  Кадр не закрашивается ничем.
    """
    render_footer(fb, bottom)
    render_status(fb, top)


def _row(fb: Framebuffer, row: Row, y0: int, *, pad: bool) -> None:
    """Одна строка: ячейки через разделитель « | »; `RULE` — черта.

    `pad=True` — каждая ячейка дополняется до `CELL_COLS`, и границы
    колонок совпадают в каждом ряду подвала.  `pad=False` — ячейки идут
    впритык, своей ширины: так раскладывается строка состояния, и при
    дополнении до ширины колонки она уезжала бы за 35 знакомест полосы.
    """
    if row == [RULE]:
        for x in range(fb.width):
            fb.apply(x, y0 + ROW_H // 2, PEN_OR)
        return
    pl = plan(fb.width // CELL_W, colw=CELL_COLS)
    # Ширину ячейки обрезает только подвал: у колонок она общая, а в
    # строке состояния ячейки идут впритык и «13.10 07:52» не должно
    # превращаться в «13.10 07:».
    limit = pl.colw if pad else fb.width // CELL_W
    x = 0
    for n, cell in enumerate(row):
        if n:
            _sep(fb, x, y0)
            x += len(SEP) * CELL_W
        text = cell_text(cell)[:limit]
        for ch in text:
            if x >= fb.width:
                break
            put_glyph(fb, x, y0, ch, PEN_OR)
            x += CELL_W
        if pad:
            x += max(0, pl.colw - len(text)) * CELL_W


def _sep(fb: Framebuffer, x: int, y0: int) -> None:
    """Разделитель « | » ячейкой подвала."""
    mid = ROW_H // 2
    for dx, dy in ((0, mid - 3), (0, mid + 3), (0, mid),
                   (CELL_W - 1, mid - 3), (CELL_W - 1, mid + 3),
                   (CELL_W - 1, mid)):
        fb.apply(x + dx, y0 + dy, PEN_OR)
    for k in range(1, 3):
        fb.apply(x + k, y0 + mid, PEN_OR)


__all__ = ['cols', 'ROW_H', 'CELL_COLS', 'status_rows', 'footer_rows',
           'chrome_height', 'render', 'render_status', 'render_footer']