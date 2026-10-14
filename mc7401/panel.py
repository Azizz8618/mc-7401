"""
panel.py — оформление интерфейса терминала МС 7401.

Модуль собирает три вещи, одинаковые для обоих фронтендов — консольного
(`tui.py`) и графического окна (`window.py`):

* **верхняя строка состояния** — сведения о линии над кадром;
* **подвал** — панель состояний сеанса, черта и легенда клавиш;
* **диагностические сообщения** — прокручиваемый журнал с отметкой времени.

МОДЕЛЬ ВЗЯТА ИЗ ЭТАЛОНА. Ячейка панели — кортеж сегментов `(текст, стиль)`,
стиль задаёт цвет; ячейки складываются в **ровные колонки**, чтобы разделители
« | » стояли на одних позициях во всех рядах; ряды состояний отбиваются от
легенды чертой.  Это ровно то, что делает эмулятор терминала
«Электроника-15ИЭ-00-013» (`ie15/ie15emu/screen.py`: `mk()`, `RULE`,
`STYLE_ANSI`, `set_panel()`), и повторяет его оформление:

    ВКЛ — зелёный, ВЫКЛ — красный, прочие значения — светло-голубые,
    АВТОНОМНО — красный подчёркнутый (набор идёт в буфер, а не в ЭВМ),
    F-клавиши — жёлтые, сочетания с Ctrl — маджента, правка — синяя,
    ПРОВЕРЬ ЛИНИЮ! — красный, когда баннер линии не пришёл.

Цвета отдаются отдельно от текста: `render_ansi()` собирает escape-
последовательности для консоли, `render_spans()` — текст и диапазоны для
тегов Tk.  Один и тот же набор ячеек, значит подвалы обоих фронтендов
не могут разойтись.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Sequence

from .chars import SET_LABELS
from .pen import MODE_DRAW, MODE_SELECT

#: Разделитель ячеек подвала.
SEP = ' | '
#: Номинальная ширина ячейки подвала.  Ширина, которой ряд заполняет
#: окно, считается от него планом раскладки (`Plan`) — один на весь подвал.
COLW = 17
#: Узкая колонка ниже этого не сжимается: иначе ячейка переносится
#: на следующий ряд и колонки «разъезжаются» по вертикали.
MIN_COLW = 12
#: Черта, отбивающая состояния от легенды.
RULE = '\x00rule'

#: Знак пустого значения в подвале.  Именно дефис, а не тире: в ПЗУ
#: знакогенератора СИМ (`chars.glyph_matrix`, дамп КР556РТ5) тире нет, и
#: ячейка с ним рисовалась пустой — в подвале не хватало знака там, где
#: ничего и не должно быть, и это выглядело как обрыв ячейки.  Проверка
#: «у подвала нет знаков без растра» — `tests/test_sym.py`.
EMPTY = '-'


@dataclass(frozen=True)
class Plan:
    """Раскладка подвала: сколько ячеек в ряду и какой ширины.

    Считается **один раз на подвал** и используется всеми рядами и всеми
    выводами (ANSI, текст, теги Tk).  Иначе ряды считают ширину каждый
    сам и границы колонок не совпадают: черта выходит длиннее содержимого,
    разделители « | » стоят в разных местах ряда — то самое, что здесь и
    требовалось исправить.

    Ширина ряда: `per_row * (colw + len(SEP)) - len(SEP)`.
    """

    per_row: int
    colw: int

    @property
    def width(self) -> int:
        """Полная ширина ряда подвала в знаках."""
        return self.per_row * (self.colw + len(SEP)) - len(SEP)


def plan(width: int, *, per_row: Optional[int] = None,
         colw: Optional[int] = None) -> Plan:
    """Раскладка подвала под полосу шириной `width` знаков.

    `colw`/`per_row` задают раскладку явно — так полоса служебных строк
    окна (35 знакомест СИМ) сохраняет структуру 15ИЭ, но с более узкими
    колонками; без них берутся номинальная ширина ячейки `COLW`.
    """
    if per_row:
        n = max(1, min(per_row, (width + len(SEP)) // (1 + len(SEP))))
        return Plan(n, max(1, colw or (width + len(SEP)) // n - len(SEP)))
    if colw:
        return Plan(max(1, (width + len(SEP)) // (colw + len(SEP))), colw)
    n = max(1, width // (COLW + len(SEP)))
    return Plan(n, max(MIN_COLW, (width + len(SEP)) // n - len(SEP)))

#: Стили ячеек.  `''` — обычный текст.
STYLES = (
    'on',       # включено — зелёный
    'off',      # выключено — красный
    'state',    # прочее значение состояния — светло-голубой
    'local',    # АВТОНОМНО — красный подчёркнутый
    'last',     # последнее нажатие (ПОСЛ) — белый
    'fkey',     # клавиши F2…F10 — жёлтый
    'ctrl',     # сочетания с Ctrl — маджента
    'edit',     # правка (ВК, ПРПС, ESC) — синий
    'mouse',    # мышь — голубой
    'label',    # метка состояния — приглушённая
    'diag',     # диагностика — серый
    'warn',     # предупреждение — жёлтый
    'err',      # ошибка — красный
)

#: Ячейка — кортеж сегментов `(текст, стиль)`.
Cell = tuple[tuple[str, str], ...]
#: Ряд подвала: ячейки либо `[RULE]` — черта, отбивающая состояния от легенды.
Row = list[Cell]


def mk(*segments) -> Cell:
    """Ячейка из сегментов: `mk('СЕТЬ=', ('ВКЛ', 'on'))`."""
    return tuple((s, '') if isinstance(s, str) else s for s in segments)


def cell_text(cell: Cell) -> str:
    """Ячейка сплошным текстом (проверки, дампы)."""
    return ''.join(text for text, _ in cell)


def cell_width(cell: Cell) -> int:
    return sum(len(text) for text, _ in cell)


# ── данные панели ──────────────────────────────────────────────────────────

@dataclass
class Status:
    """Состояние, из которого собираются строка состояния и подвал.

    Заполняется фронтендом перед отрисовкой; значений по умолчанию
    достаточно, чтобы панель была осмысленной и без линии (окно запущено
    с файлом потока или вовсе без источника).
    """

    #: `'host'` (С ЭВМ) или `'local'` (АВТОНОМНО).
    mode: str = 'host'
    #: Имя набора знаков: `'n2'` | `'koi8r'`.
    charset: str = 'n2'
    #: Строка состояния линии: имя источника или `адрес:порт`.
    line: str = EMPTY
    #: Тип линии из баннера: «ДКС» или «КВУ».  **Не выдумывается**: если
    #: баннер не разобран, строка состояния показывает «ПРОВЕРЬ ЛИНИЮ!»,
    #: а не «КВУ» — иначе линия ДКС выглядит как телетайп.
    link_kind: str = ''
    #: Пришёл ли баннер линии.
    banner: bool = False
    #: Номер терминала на линии (`TTY DEVICE, LINE N`).
    terminal: int = 0
    #: Номер ЭВМ (`ЭВМ-3,Т017` → 3 или `[line] machine`).
    machine: int = 0
    #: Режим линии из баннера (`RAW`, `UNICODE`, `JCUKEN`).
    wire: str = ''
    echo: bool = True
    #: Световое перо включено (клавиша F2).
    pen: bool = False
    #: Инструмент пера: `'штрих'` | `'стирание'` | `'точка'`.
    pen_tool: str = 'штрих'
    #: Режим пера: `'рисование'` | `'выделение'` (клавиша F4).
    pen_mode: str = ''
    #: Тип линии пера (0…7, как в терминале).
    pen_line_type: int = 1
    #: Толщина пера в точках.
    pen_width: int = 1
    #: Размер выделения мышью, как на 15ИЭ: `СТР 1фр 1с 12зн`.
    selection: str = ''
    #: Сколько действий можно отменить (`Ctrl-Z`, только АВТОНОМНО).
    undo: int = 0
    #: Отмена доступна — то есть терминал в режиме АВТОНОМНО.
    undo_ready: bool = False
    #: Накоплено знаков в буфере АВТОНОМНО.
    buffer: int = 0
    grid: bool = False
    scale: int = 1
    points: int = 0
    #: Последняя нажатая клавиша — «ПОСЛ:».
    last_key: str = ''
    #: Набираемая строка — «ВВОД:».
    input_line: str = ''
    #: Курсор кадра `x,y`.
    cursor: str = '0,0'
    #: Показ графики: `''` | `'протокол'` | `'псевдографика'`.
    graphics: str = ''
    #: Номер канала/порта линии (`КАНАЛ=4202`).
    channel: str = ''
    #: Положение окна в истории выдачи (`ИСТ=`): пусто — кадр живой,
    #: иначе «назад N из M» (окно листается колесом и `ЭКРАН↑`/`ЭКРАН↓`).
    history: str = ''

    @property
    def charset_label(self) -> str:
        return SET_LABELS.get(self.charset, self.charset)

    @property
    def mode_label(self) -> str:
        return 'С ЭВМ' if self.mode == 'host' else 'АВТОНОМНО'


# ── верхняя строка состояния ───────────────────────────────────────────────

def status_line(st: Status, *, now: Optional[datetime] = None,
                 full: bool = True, clock: str = '', clock_stale: bool = False) -> list[Cell]:
    """Строка состояния линии — у верхней кромки кадра, поверх него.

    Слева направо: тип линии, номер ЭВМ, номер терминала, канал, режим
    линии, дата и время.  Всё, кроме даты, берётся из баннера SIMH
    (`link.parse_greeting`); если баннер не пришёл — красное
    «ПРОВЕРЬ ЛИНИЮ!»: молчаливая работа без связи выглядит как исправный
    терминал.  Тип линии по умолчанию **не подставляется** — линия,
    объявленная баннером «Connected to DKS», это «ДКС», а не «КВУ».

    `full=False` — короткая форма для окна (`chrome`): полоса служебных
    строк всего 35 знакомест СИМ, и полная строка там занимает три ряда,
    закрывая верх кадра.  Консольному `tui.py` ширины хватает.
    """
    out: list[Cell] = []
    if st.banner and st.link_kind:
        out.append(mk((st.link_kind, 'state')))
    if st.machine:
        out.append(mk(('ЭВМ=', 'label'), (str(st.machine), 'state')))
    if st.terminal:
        out.append(mk(('ТЕРМ=', 'label'), (str(st.terminal), 'state')))
    if full:
        # Название модели в окне есть в заголовке, в полосе — лишний ряд.
        out.append(mk(('Т=МС7401', 'state')))
    if st.channel:
        out.append(mk(('КАН=', 'label'), (st.channel, 'state')))
    if st.wire:
        out.append(mk(('РЕЖ=', 'label'), (st.wire, 'state')))
    if not st.banner:
        out.append(mk(('ПРОВЕРЬ ЛИНИЮ!', 'err')))
    # Размер выделения мышью — как на 15ИЭ (`session.py`, `_update_status`):
    # видно сразу, выбрана одна строка или пол-экрана.
    if st.selection:
        out.append(mk(('ВЫД=', 'label'), (st.selection, 'state')))
    # Время берётся у **ЭВМ** (`clock`), а не у компьютера: на стенде
    # ЭВМ на директиву `ВРЕ` отвечала `ВРЕМЯ:22.04.15`, а баннер линии
    # в тот же момент несел `19:45` — сверять работу по часам терминала
    # было не с чем.  Часы компьютера подставляются только когда ЭВМ
    # молчит, и тогда помечаются меткой `КОМП`, чтобы спутать нельзя.
    if clock:
        out.append(mk((clock, 'warn' if clock_stale else 'diag')))
    else:
        stamp = (now or datetime.now()).strftime(
            '%d.%m %H:%M' if not full else '%d.%m.%Y %H:%M:%S')
        out.append(mk((f'КОМП {stamp}', 'warn')))
    return out


def _short(text: str, limit: int = 24) -> str:
    text = text or EMPTY
    return text if len(text) <= limit else text[:limit - 3] + '...'


# ── подвал ─────────────────────────────────────────────────────────────────

#: Короткие метки режима пера: колонка подвала даёт на значение четыре
#: знакоместа, а полные названия в неё не влезают.
PEN_MODES = {MODE_DRAW: 'РИС', MODE_SELECT: 'ВЫД'}
#: Короткие метки инструментов пера — по той же причине.
PEN_TOOLS = {'штрих': 'ШТР', 'точка': 'ТЧК', 'стирание': 'СТИР',
             'инверсия': 'ИНВ'}


def state_rows(st: Status, width: int = COLW) -> list[Cell]:
    """Панель состояний сеанса — первые ряды подвала.

    Подписи короткие: полоса служебных строк окна — 35 знакомест СИМ, и
    каждый ряд подвала, закрывая часть кадра, должен быть как можно
    короче.  Набор — как на 15ИЭ (режим, набор, линия, эхо, перо,
    масштаб), плюс специфичные для МС 7401 «последнее нажатие» и
    «набираемая строка» — их больше нигде не видно.
    """
    def cell(label: str, value: str, style: str = 'state',
             keep: Optional[int] = None) -> Cell:
        # `keep` — сколько знакомест значению нужно вопреки ширине
        # колонки: иначе длинное значение (`≡144 'd'`, «УПР-q») или
        # целиком уходило в многоточия либо обрезалось по чужой метке.
        limit = keep if keep is not None else width - len(label) - 1
        return mk((label, 'label'), (_short(value, limit), style))

    return [
        cell('РЖМ=', 'СЭВМ' if st.mode == 'host' else 'АВТО',
             'on' if st.mode == 'host' else 'local'),
        cell('НАБ=', 'КОИ8' if st.charset == 'koi8r' else 'Н2'),
        cell('ЛИН=', (st.link_kind or 'НЕТ') if st.banner else EMPTY,
             'state' if st.banner else 'err'),
        cell('ЭХО=', 'ВКЛ' if st.echo else 'ВЫКЛ',
             'on' if st.echo else 'off'),
        # Режим и инструмент пера — в **двух** ячейках: колонка подвала
        # узка (9 знакомест, на значение остаётся четыре), и длинное
        # «выделение» или «штрих» превращалось в одни многоточия.
        cell('ПЕРО=', PEN_MODES.get(st.pen_mode, 'РИС') if st.pen else 'ВЫК',
             'on' if st.pen else 'off'),
        cell('ИНСТ=', PEN_TOOLS.get(st.pen_tool, st.pen_tool)
             if st.pen and not st.pen_mode else EMPTY),
        cell('ТИП=', str(st.pen_line_type) if st.pen and not st.pen_mode
             else EMPTY,
             'on' if st.pen else 'off'),
        cell('ТОЛ=', str(st.pen_width) if st.pen and not st.pen_mode
             else EMPTY,
             'on' if st.pen else 'off'),
        cell('ОТМ=', str(st.undo) if st.undo else (EMPTY if st.undo_ready
                                                 else 'НЕТ'),
             'on' if st.undo else 'off'),
        # Масштаб выводится из размера окна и бывает дробным, поэтому
        # без префикса «1:» — иначе ячейка подвала обрезает значение.
        cell('МАШ=', f'{float(st.scale):.1f}'.rstrip('0').rstrip('.')),
        cell('БУФ=', str(st.buffer) if st.buffer else EMPTY),
        # Положение окна в истории выдачи.  Пока кадр живой, ячейка пустая
        # («—»): листание назад — редкое состояние, а лишняя метка в
        # подвале стоила бы ряда.  Когда окно отошло назад, видно, что
        # перед глазами не текущий кадр, иначе выдача с ЭВМ выглядит как
        # зависшая.
        cell('ИСТ=', st.history or EMPTY, 'on' if st.history else 'off'),
        # ПОСЛ — широкая ячейка: значение («≡144 'd'», «УПР-q») длиннее
        # любой колонки, и раньше метка и значение слипались в одно
        # слово — знака «=» не было, а лимит считался от ширины колонки,
        # а не от длины метки.  Свой ряд и ширину колонок не задаёт, как
        # «ПОСЛ: …» на 15ИЭ.
        wide(('ПОСЛ=', 'label'), (_short(st.last_key, 40), 'last')),
        wide(('ВВОД=', 'label'),
             (_short(st.input_line, 40),
              'local' if st.mode == 'local' else 'state')),
    ]


def legend_rows(width: int = COLW) -> list[Cell]:
    """Легенда клавиш — ряды подвала под чертой.

    Структура та же, что на 15ИЭ: группы «функциональные клавиши»,
    «мышь», «правка», отличаются только сами привязки — у МС 7401
    вместо `УПР.СИМВ`/`ВЗЯТЬ` световое перо и инструмент пера.
    Ячейки не длиннее `width`, иначе их обрезает раскладка подвала.
    """
    fkey = 'fkey'
    ctrl = 'ctrl'
    edit = 'edit'
    mouse = 'mouse'
    return [
        mk(('F2=', fkey), ('ПЕРО', '')),
        mk(('F3=', fkey), ('ИНСТР', '')),
        mk(('F5=', fkey), ('ЧИСТКА', '')),
        mk(('F6=', fkey), ('ЭХО', '')),
        mk(('F8=', fkey), ('НАБОР', '')),
        mk(('F9=', fkey), ('СЕТЬ', '')),
        mk(('F10=', fkey), ('ПЕРЕД', '')),
        mk(('F4=', fkey), ('РЕЖИМ', '')),
        mk(('F7=', fkey), ('СНЯТ', '')),
        # Правую кнопку («стирание») в подвал не вносим: она ушла бы в
        # лишний ряд, а стирание и так есть среди инструментов F3.
        mk(('ЛЕВ=', mouse), ('КУРС', '')),
        mk(('ОТМЕНА=', ctrl), ('Ctrl-z', '')),
        mk(('ВЫХОД=', fkey), ('F12', '')),
    ]


#: Стиль-маркер широкой ячейки: она всегда занимает свой ряд и **не
#: задаёт** ширину колонок.  Так в 15ИЭ устроена «ПОСЛ: …»
#: (`screen.py`, `set_panel(..., wide=[last])`): иначе длинное значение
#: сжимает всю сетку подвала, а с ней прыгает высота кадра.  Маркер —
#: обычный стиль ячейки, поэтому `cell_text` и раскладка его видят.
WIDE = 'wide'


def wide(*pairs) -> Cell:
    """Пометить ячейку как широкую: своя строка, ширину не задаёт."""
    return tuple(pairs) + (('', WIDE),)


def footer(st: Status, *, width: int, help_: bool = True,
           colw: Optional[int] = None,
           legend_colw: Optional[int] = None,
           per_row: Optional[int] = None,
           legend_per_row: Optional[int] = None) -> list[Row]:
    """Подвал целиком: состояния в ровных колонках, черта, легенда.

    Состояния и легенда раскладываются по **разным** планам: у легенды
    свои ячейки («ОТМЕНА=Ctrl-z»), и диктовать её ширину состояниям незачем
    — блоки и так разделены чертой.  Внутри каждого блока границы
    колонок совпадают в каждом ряду.

    `per_row`/`legend_per_row` фиксируют число ячеек в ряду: так можно
    расширить колонки и не пожертвовать высотой подвала.
    """
    w = colw or COLW
    state_pl = plan(width, per_row=per_row, colw=colw)
    states, wides = _split_wide(state_rows(st, w))
    rows: list[Row] = list(columns(states, width,
                                   colw=state_pl.colw,
                                   per_row=state_pl.per_row))
    rows.extend(_wide_rows(wides, width))     # широкие — свой ряд
    rows.append([RULE])
    if help_:
        lg_pl = plan(width, per_row=legend_per_row, colw=legend_colw)
        cells, lg_wide = _split_wide(legend_rows(w))
        for row in columns(cells, width, colw=lg_pl.colw,
                           per_row=lg_pl.per_row):
            rows.append(row)
        rows.extend(_wide_rows(lg_wide, width))
    return rows


def _wide_rows(wides: Sequence[Cell], width: int) -> list[Row]:
    """Разложить широкие ячейки по рядам: **соседние — в один ряд**.

    «ПОСЛ= …» и «ВВОД= …» дополняют друг друга (что нажато и что
    набрано), и в один ряд помещаются оба; разносить их значило бы
    отдать кадру лишний ряд подвала.
    """
    rows: list[Row] = []
    cur: Row = []
    used = 0
    for cell in wides:
        w = cell_width(cell)
        if cur and used + len(SEP) + w > width:
            rows.append(cur)
            cur, used = [], 0
        used += (len(SEP) if cur else 0) + w
        cur.append(cell)
    if cur:
        rows.append(cur)
    return rows


def _split_wide(cells: Sequence[Cell]) -> tuple[list[Cell], list[Cell]]:
    """Разделить ячейки на обычные и широкие (помеченные `WIDE`).

    Возвращает `(обычные, широкие)`; у широких маркер снят, чтобы
    `cell_text` и раскладка видели только текст ячейки.
    """
    plain: list[Cell] = []
    wide: list[Cell] = []
    for c in cells:
        if any(style == WIDE for _, style in c):
            wide.append(tuple(t for t in c if t[1] != WIDE))
        else:
            plain.append(c)
    return plain, wide


def columns(cells: Sequence[Cell], width: int, *, sep: str = SEP,
            colw: Optional[int] = None,
            per_row: Optional[int] = None) -> list[Row]:
    """Разложить ячейки в ряды одинаковой ширины (по плану подвала)."""
    pl = plan(width, per_row=per_row, colw=colw)
    if not cells:
        return [[]]
    return [list(cells[i:i + pl.per_row])
            for i in range(0, len(cells), pl.per_row)]


# ── вывод ──────────────────────────────────────────────────────────────────

#: Escape-последовательности стилей для консоли.
STYLE_ANSI = {
    'on': '\x1b[32m',
    'off': '\x1b[31m',
    'state': '\x1b[96m',
    'local': '\x1b[31;4m',
    'last': '\x1b[97m',
    'fkey': '\x1b[93m',
    'ctrl': '\x1b[95m',
    'edit': '\x1b[94m',
    'mouse': '\x1b[96m',
    'label': '',
    'diag': '\x1b[90m',
    'warn': '\x1b[93m',
    'err': '\x1b[31;1m',
}
RESET = '\x1b[0m'


def render_ansi(row: Row, width: int = 0) -> str:
    """Ряд подвала строкой ANSI с выравниванием по колонкам."""
    if row == [RULE]:
        return '-' * width if width else '-' * (COLW * 4 + 3 * 3)
    parts = []
    for cell in row:
        pad = max(0, COLW - cell_width(cell))
        text = cell_text(cell) + ' ' * pad
        parts.append(''.join(_ansi(text, style) for text, style in cell))
    return SEP.join(parts).rstrip()


def _ansi(text: str, style: str) -> str:
    code = STYLE_ANSI.get(style, '')
    return f'{code}{text}{RESET}' if code else text


def render_plain(row: Row, width: int = 0) -> str:
    """Ряд подвала без цветов — для проверок и снимков экрана."""
    if row == [RULE]:
        return '-' * width if width else '-' * (COLW * 4 + 3 * 3)
    return SEP.join(cell_text(c).ljust(COLW) for c in row).rstrip()


def render_spans(row: Row, width: int = 0) -> tuple[str, list[tuple[int, int, str]]]:
    """Ряд подвала как `(текст, [(начало, конец, стиль), …])` — для Tk.

    Tk не понимает escape-последовательности, поэтому цвет задаётся тегами
    на диапазонах: тот же набор ячеек, но раскраска — средствами окна.
    """
    if row == [RULE]:
        line = '-' * width if width else '-' * (COLW * 4 + 3 * 3)
        return line, []
    chunks: list[str] = []
    spans: list[tuple[int, int, str]] = []
    pos = 0
    for n, cell in enumerate(row):
        if n:
            chunks.append(SEP)
            pos += len(SEP)
        for text, style in cell:
            chunks.append(text)
            if style:
                spans.append((pos, pos + len(text), style))
            pos += len(text)
        pad = max(0, COLW - cell_width(cell))
        if pad:
            chunks.append(' ' * pad)
            pos += pad
    return ''.join(chunks).rstrip(), spans


# ── диагностика ────────────────────────────────────────────────────────────

#: Подписи важности сообщений.
KINDS = {'info': 'СВЕД', 'warn': 'ВНИМ', 'err': 'ОШИБ', 'ok': 'ГОТО'}


@dataclass
class Diagnostics:
    """Журнал диагностических сообщений.

    Хранит последние `limit` сообщений с отметкой времени; `last` —
    то, что показывается в строке состояния.  Отдельный журнал нужен
    потому, что сообщения «строка передана» или «протокол графики не
    поддержан» нельзя терять: они объясняют, почему экран выглядит так,
    а не иначе.
    """

    limit: int = 50
    entries: deque = field(default_factory=deque)
    last: str = ''
    #: Важность и отметка времени последнего сообщения — их ставит
    #: служебная полоса на кадре (`strip.DiagStrip`).
    last_kind: str = 'info'
    last_stamp: str = ''

    def add(self, text: str, kind: str = 'info', *, now=None) -> None:
        """Записать сообщение (пустое — сообщение очистки игнорируется)."""
        text = (text or '').strip()
        if not text:
            return
        stamp = (now or datetime.now()).strftime('%H:%M:%S')
        self.entries.append((stamp, kind, text))
        while len(self.entries) > self.limit:
            self.entries.popleft()
        self.last = text
        self.last_kind = kind
        self.last_stamp = stamp

    def clear(self) -> None:
        self.entries.clear()
        self.last = ''
        self.last_kind = 'info'
        self.last_stamp = ''

    def recent(self, n: int) -> list[tuple[str, str, str]]:
        """Последние `n` сообщений (старые — новые)."""
        items = list(self.entries)
        return items[-n:] if n > 0 else items

    def lines(self, n: int) -> list[Cell]:
        """Сообщения ячейками подвала: `ВАЖНО текст`.

        **Без метки времени.**  Строка журнала идёт последней строкой
        подвала, и время на ней читалось как «часы внизу» — тем более
        что это время компьютера, а не ЭВМ.  Часы в подвале уже есть
        (строка состояния), а для разбора журнал хранит отметки сам
        (`recent`, `entries`).
        """
        out: list[Cell] = []
        for stamp, kind, text in self.recent(n):
            style = 'err' if kind == 'err' else ('warn' if kind == 'warn'
                                                 else 'diag')
            out.append(mk((KINDS.get(kind, 'СВЕД') + ' ', style),
                          (_short(text, COLW * 2), style)))
        return out


__all__ = ['Cell', 'Row', 'COLW', 'SEP', 'RULE', 'Plan', 'plan',
           'Status', 'Diagnostics', 'KINDS', 'mk', 'cell_text', 'cell_width',
           'status_line', 'state_rows', 'legend_rows', 'footer', 'columns',
           'render_ansi', 'render_plain', 'render_spans', 'STYLE_ANSI']