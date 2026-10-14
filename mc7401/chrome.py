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

from . import console
from .console import put_glyph
from .constants import PEN_OR, SCREEN_WIDTH
from .framebuffer import Framebuffer
from .panel import (Cell, Diagnostics, Row, RULE, SEP, Status, cell_text,
                    cell_width, columns, footer as panel_footer, mk, plan,
                    status_line as panel_status_line)

#: Высота строки служебных строк — та же, что у текстового слоя кадра.
#:
#: Берётся из `console` при каждом обращении, а не один раз при импорте:
#: `set_glyph_scale` переприсваивает `console.CELL_H` уже **после** загрузки
#: этого модуля — окно вызывает его в `__init__` по `[terminal] glyph`.
#: Константа, вычисленная при импорте, осталась бы от прежнего масштаба
#: знака, и служебные полосы рисовались бы вдвое ниже текста кадра и
#: налезали бы на него.  Поэтому `ROW_H` — вычисляемое поле модуля
#: (см. `__getattr__` в конце файла), а внутри модуля — через `row_h()`.
def row_h() -> int:
    """Высота ряда служебных строк в точках — текущий размер знака."""
    return console.CELL_H
#: Ширина ячейки подвала в знакоместах СИМ.  Полоса — 70 знакомест, и
#: колонки должны быть широкими, чтобы значение влезало рядом с
#: меткой: на девяти знакоместах «ПЕРО=ВЫД» или «ОТМЕНА=Ctrl-z»
#: превращались в многоточия.  Число ячеек в ряду задано отдельно
#: (`CELL_PER_ROW`): иначе расширение колонки отбирало бы у кадра ещё
#: один ряд подвала.
CELL_COLS = 11
#: Сколько ячеек состояний и легенды влезает в ряд.  Фиксировано, чтобы
#: высота подвала (а с ней и высота кадра) не зависела от ширины колонки
#: и не скакала от нажатия.
CELL_PER_ROW = 5
#: Легенда — свои ячейки («ОТМЕНА=Ctrl-z»), диктовать ей ширину
#: состояний незачем: блоки разделены чертой.
LEGEND_PER_ROW = 6

#: Ряд-черта: рисуется заливкой, а не знаком — в знакогенераторе нет
#: знака горизонтальной линии во всю ширину знакоместа.  Маркер тот же,
#: что и в `panel.RULE`, поэтому подвал собирается один раз.


def cols() -> int:
    """Сколько знакомест в строке служебных строк.

    Считается на лету: размер знака задаётся настройкой `[terminal] glyph`
    (`console.set_glyph_scale`), и полоса обязана с ним совпадать.
    """
    return max(1, SCREEN_WIDTH // console.STEP_W)


# ── содержимое ─────────────────────────────────────────────────────────────

#: Ряды между строкой состояния и кадром: черта и пустой ряд — отбивка,
#: как на 15ИЭ.  Отступ нужен, чтобы верхний ряд рабочего поля не лип к
#: черте: на настоящем терминале кадр отделён от служебных строк.
STATUS_GAP: list[Row] = [[RULE], []]


def _wrap_cells(cells: list[Cell], width: int) -> list[Row]:
    """Разложить ячейки по рядам полосы — жадно, по ширине."""
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


#: Заполнитель зарезервированного ряда.
#:
#: Ряд резерва должен быть **непустым**: пустые ряды над чертой читались
#: бы дырами в полосе, да и требование «строка состояния, затем черта»
#: проверяется как «все ряды над чертой непусты».  Рисовать в нём
#: нечего — знакоместо пробела.
_RESERVE_CELL: Cell = mk((' ', 'label'))
#: Резерв рядов строки состояния по ширине полосы: считается один раз.
_RESERVED: dict[int, int] = {}


def reserved_status_rows(width: int) -> int:
    """Сколько рядов занимает строка состояния **в худшем случае**.

    Набор её ячеек условен: `ЭВМ=`, `ТЕРМ=`, `КАН=`, `РЕЖ=`, `ВЫД=` и
    время появляются и исчезают по ходу работы.  Раньше высота считалась
    по факту, и каждая появившаяся ячейка могла добавить ряд: кадр от
    того съезжал вниз и сжимался (`fy` и `scale` пересчитывались), а
    выделение мышью начиналось **именно** в тот момент — картинка
    уезжала из-под курсора прямо в начале протяжки, и выделение
    выпадало на соседний ряд.  Подвал тоже скакал, когда в журнал
    писалась строка с отметкой времени.

    Поэтому высота резервируется по худшему случаю — все ячейки на
    месте, значения предельной длины, — и строка состояния занимает
    одно и то же число рядов всегда.  Пустые ряды дорисовываются и
    ничего не занимают на экране.
    """
    if width not in _RESERVED:
        clock = '99.99 99:99:99'
        worst = Status(link_kind='ДКС', banner=True, machine=99,
                       terminal=999, channel='999', wire='ЖОПЕСЪ',
                       selection='КОЛ 9фр 39с 999зн')
        lost = Status(link_kind='', banner=False, machine=0, terminal=0,
                      channel='', wire='', selection=worst.selection)
        n = 0
        for st in (worst, lost):
            cells = list(panel_status_line(st, full=False, clock=clock))
            n = max(n, len(_wrap_cells(cells, width)))
        _RESERVED[width] = max(1, n)
    return _RESERVED[width]


def status_rows(st: Status, *, clock: str = '',
                clock_stale: bool = False) -> list[Row]:
    """Всё, что стоит **над** кадром: строка состояния, черта и отступ.

    Порядок и содержание — как в эталоне 15ИЭ: тип линии, ЭВМ, терминал,
    канал, режим, время.  Без баннера — «ПРОВЕРЬ ЛИНИЮ!», а не выдуманный
    «КВУ» (тип линии разбирает `link.parse_greeting`).

    Форма короткая (`full=False`): полоса — 35 знакомест, полная строка
    с названием модели и полными датой-временем занимала бы три ряда и
    закрывала верх кадра.

    Под строкой состояния идут черта и пустой ряд (`STATUS_GAP`) — по
    требованию «после строки состояния черта, затем отступ в одну строку
    перед рабочей областью».  Ряды возвращаются готовыми, поэтому
    `top_height` и `render_status` ничего не знают про отступ.
    """
    cells: list[Cell] = list(panel_status_line(st, full=False,
                                              clock=clock,
                                              clock_stale=clock_stale))
    width = cols()
    rows = _wrap_cells(cells, width)
    # Добиваем до резерва: кадр обязан стоять на месте при любом наборе
    # ячеек — см. `reserved_status_rows`.
    while len(rows) < reserved_status_rows(width):
        rows.append([_RESERVE_CELL])
    return rows + [list(r) for r in STATUS_GAP]


def status_text_rows(st: Status, *, clock: str = '',
                    clock_stale: bool = False) -> list[Row]:
    """Только строка состояния — без черты и отступа.

    Нужна там, где верхняя полоса нужна для проверки содержимого, а не
    для рисования: `len` такой полосы иначе сбился бы на два ряда
    отбивки.
    """
    full = status_rows(st, clock=clock, clock_stale=clock_stale)
    return full[:-len(STATUS_GAP)] if STATUS_GAP else full


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
    rows = panel_footer(st, width=width, help_=help_, colw=CELL_COLS,
                        per_row=CELL_PER_ROW, legend_per_row=LEGEND_PER_ROW)
    if diag is not None and diag.last:
        rows.extend(columns(diag.lines(1), width, colw=CELL_COLS,
                            per_row=CELL_PER_ROW))
    return rows


def chrome_height(top: list[Row], bottom: list[Row]) -> int:
    """Высота полосы служебных строк в точках — оба конца вместе."""
    return (len(top) + len(bottom)) * row_h()


def top_height(top: list[Row]) -> int:
    """Высота строки состояния над кадром в точках."""
    return len(top) * row_h()


# ── отрисовка ──────────────────────────────────────────────────────────────

def render_status(fb: Framebuffer, top: list[Row]) -> None:
    """Всё, что стоит **над** кадром: строка состояния, черта и отступ.

    В полосе `fb` кадр занимает нижние `SCREEN_HEIGHT` рядов, а верхняя
    полоса — `len(top) * ROW_H`.  Ряды нумеруются `Y` вверх, поэтому
    первый ряд строки состояния — самый верхний, а последний (`[]` —
    отступ) — вплотную к кадру.

    Пустой ряд ничего не рисует, а полоса стирается целиком, поэтому
    отступ остаётся чистым: верхний ряд рабочего поля не липнет к черте.
    """
    h = len(top) * row_h()
    fb.erase_rows(fb.height - h, fb.height)
    for n, row in enumerate(top):
        _row(fb, row, fb.height - (n + 1) * row_h(), pad=False)


def render_footer(fb: Framebuffer, bottom: list[Row]) -> None:
    """Подвал — под кадром, привязан к нижней границе полосы.

    Первый ряд подвала — верхний из отведённых ему: нарисованный
    снизу, он читался бы перевёрнутым (сначала легенда, потом
    состояния).
    """
    h = len(bottom) * row_h()
    fb.erase_rows(0, h)
    for n, row in enumerate(bottom):
        _row(fb, row, h - (n + 1) * row_h(), pad=True)


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
            fb.apply(x, y0 + row_h() // 2, PEN_OR)
        return
    pl = plan(fb.width // console.STEP_W, colw=CELL_COLS)
    # Ширину ячейки обрезает только подвал: у колонок она общая, а в
    # строке состояния ячейки идут впритык и «13.10 07:52» не должно
    # превращаться в «13.10 07:».
    limit = pl.colw if pad else fb.width // console.STEP_W
    x = 0
    for n, cell in enumerate(row):
        if n:
            _sep(fb, x, y0)
            x += len(SEP) * console.STEP_W
        text = cell_text(cell)[:limit]
        for ch in text:
            if x >= fb.width:
                break
            put_glyph(fb, x, y0, ch, PEN_OR)
            x += console.STEP_W
        if pad:
            x += max(0, pl.colw - len(text)) * console.STEP_W


def _sep(fb: Framebuffer, x: int, y0: int) -> None:
    """Разделитель « | » ячейкой подвала."""
    mid = row_h() // 2
    for dx, dy in ((0, mid - 3), (0, mid + 3), (0, mid),
                   (console.CELL_W - 1, mid - 3),
                   (console.CELL_W - 1, mid + 3),
                   (console.CELL_W - 1, mid)):
        fb.apply(x + dx, y0 + dy, PEN_OR)
    for k in range(1, 3):
        fb.apply(x + k, y0 + mid, PEN_OR)


def __getattr__(name: str):
    """`ROW_H` следует за текущим размером знака.

    Имя остаётся в `__all__` и читается снаружи — `window.footer_px` и
    проверки обращаются к `chrome.ROW_H`, — но вычисляется при каждом
    обращении, а не схватывается один раз при импорте.  `ROW_H` —
    синоним `console.CELL_H`, и после `set_glyph_scale` он меняется.
    """
    if name == 'ROW_H':
        return row_h()
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


__all__ = ['cols', 'row_h', 'ROW_H', 'CELL_COLS', 'STATUS_GAP', 'status_rows',
           'reserved_status_rows',
           'status_text_rows', 'footer_rows', 'chrome_height', 'top_height',
           'render', 'render_status', 'render_footer']