"""
console.py — текстовый слой на кадре терминала «Электроника МС 7401».

Зачем: терминал — графический, весь вывод (и графика, и текст) живёт на
одном кадре 560×400.  Текстовые данные из БЭСМ-6 (ответы ОС Диспак по
линии ДКС) рисуются **графическими символами** эталона СИМ — тем же
растровым набором знаков 7×8 (`chars.FONT_7X8_CYR`, знакогенератор
КР556РТ5), что показывает таблица `СИМВОЛЫ`.

Сетка знакомёстов складывается с экраном без остатка:

    560 / 16 = 35 колонок,   400 / 20 = 20 строк.

Знак текстового слоя вдвое крупнее растра знакогенератора: растер
7×8 точек выводится блоком 2×2 (то есть 14×16), шаг знакоместа — 16×20
(зазор 2 точки по горизонтали и 4 по вертикали, те же пропорции, что были
при растре 1:1).  Раньше знак был 1:1 — знакомест 8×10, сетка 70×40,
и на кадре ответы ОС Диспак читались слишком мелко.

Зазор между знаками обязателен: растры ПЗУ полные по ширине («М», «Б»,
«Н»), без зазора буквы слипаются.  МС 7401 рисует текст с зазором и сам —
исторический шаг строки `9` равен `GLYPH_W * size + 1` (`chars.text_advance`).

Текстовый курсор — позиция в знакомёстах: его можно поставить в любую
клетку экрана (стрелками или мышью) и набирать там текст.  Вывод строк
идёт сверху вниз, при достижении низа экрана кадр скроллится вверх на
одну строку — как на настоящем растровом терминале.

Система координат кадра — терминальная: `X` вправо, `Y` вверх, начало в
левом нижнем углу.  Строки знакомёстов отсчитываются сверху (`row = 0` —
верх экрана), чтобы текст читался как обычно.
"""

from __future__ import annotations

from typing import Optional

from . import chars
from .constants import (
    ETX,
    GLYPH_H,
    GLYPH_W,
    PEN_ERASE,
    PEN_OR,
    SCREEN_HEIGHT,
    SCREEN_WIDTH,
)
from .framebuffer import Framebuffer

#: Во сколько раз растр знака ПЗУ (7×8) увеличен на кадре.  1 — как в
#: ПЗУ: знакомест 8×10, сетка 70×40; 2 — вдвое крупнее, знакомест
#: 16×20, сетка 35×20.  Размер знака **на экране** задаётся этим и
#: `[display] scale` вместе: при `1` и масштабе окна 2 растр ПЗУ выходит
#: на экране теми же 14×16 точками, что при `2` и масштабе 1, но
#: знакомест вдвое больше.  Меняется настройкой `[terminal] glyph`:
#: `set_glyph_scale()` вызывает фронтенд при запуске.
GLYPH_SCALE = 1

#: Допустимые размеры знака текстового слоя.
GLYPH_MIN, GLYPH_MAX = 1, 4

#: Шаг знакоместа: растер знака, увеличенный в `GLYPH_SCALE` раз, плюс
#: зазор в `GLYPH_SCALE` точек по горизонтали и `2 * GLYPH_SCALE` по
#: вертикали (при растре 1:1 — 1 и 2, как и рисует текст сам терминал:
#: исторический шаг строки `9` равен `chars.text_advance` =
#: `GLYPH_W * size + 1`).  Без зазора растры слипаются: «М», «Б» и «Н»
#: в ПЗУ занимают все семь точек по ширине.
CELL_W = GLYPH_W * GLYPH_SCALE + GLYPH_SCALE        # 8
CELL_H = GLYPH_H * GLYPH_SCALE + 2 * GLYPH_SCALE    # 10

#: Размер растра знака после увеличения (то, что реально рисуется).
RASTER_W = GLYPH_W * GLYPH_SCALE
RASTER_H = GLYPH_H * GLYPH_SCALE

#: Сетка знакомёстов: 70 колонок × 40 строк — складывается с кадром
#: 560×400 без остатка.
def _break_pair(prev, cur) -> bool:
    """Продолжение ли это уже начатого перевода строки?

    `ВК ПС` и `ПС ВК` — один перевод строки в любом порядке: ДКС так
    закрывает запись каталога (проверено на живой линии, ответ на
    `КТЛ`).  Раньше поглощалась только пара `ВК ПС`, и `ПС ВК` давал
    два перевода — между строками выдачи появлялся пустой ряд.

    Удвоение того же знака (`ВК ВК`, `ПС ПС`) — это уже пустая строка,
    и поглощать его нельзя.
    """
    return prev in BREAKS and cur in BREAKS and prev != cur


COLS = SCREEN_WIDTH // CELL_W
ROWS = SCREEN_HEIGHT // CELL_H


def set_glyph_scale(scale: int) -> int:
    """Установить размер знака текстового слоя и пересчитать сетку.

    Значение приводится к 1…4.  Пересчитываются шаг знакоместа, размер
    растра и число колонок и строк кадра: от них зависят и консоль, и
    служебные строки окна (`chrome`) — они выводятся тем же знаком.
    """
    global GLYPH_SCALE, CELL_W, CELL_H, RASTER_W, RASTER_H, COLS, ROWS
    GLYPH_SCALE = max(GLYPH_MIN, min(GLYPH_MAX, int(scale)))
    # Зазор масштабируется вместе со знаком: одна точка на ряд знака по
    # горизонтали и две по вертикали — при растре 1:1 это знакомест 8×10
    # и сетка 70×40, при 2:1 — 16×20 и 35×20.
    CELL_W = GLYPH_W * GLYPH_SCALE + GLYPH_SCALE
    CELL_H = GLYPH_H * GLYPH_SCALE + 2 * GLYPH_SCALE
    RASTER_W = GLYPH_W * GLYPH_SCALE
    RASTER_H = GLYPH_H * GLYPH_SCALE
    COLS = max(1, SCREEN_WIDTH // CELL_W)
    ROWS = max(1, SCREEN_HEIGHT // CELL_H)
    return GLYPH_SCALE

#: Код `ВК` (возврат каретки) в потоке данных ЭВМ.
CR = 0x0D
#: Код `ПС` (перевод строки) в потоке данных ЭВМ.
LF = 0x0A
#: Знаки, закрывающие строку, — и как коды байтов, и как символы:
#: `write` идёт по байтам, `write_text` — по знакам.
BREAKS = (CR, LF, '\r', '\n')


def glyph_art(ch: str) -> Optional[tuple[tuple[int, ...], ...]]:
    """Матрица знака 7×8 по символу — шрифт эталона СИМ.

    Кириллица берётся из растра `FONT_7X8_CYR`, международные знаки —
    из таблицы `chars.glyph_matrix`.  Неизвестный символ даёт `None`.
    """
    art = chars.FONT_7X8_CYR.get(ch)
    if art is not None:
        return tuple(tuple(1 if c == '#' else 0 for c in row) for row in art)
    code = ord(ch)
    if 0x20 <= code < 0x80:
        return chars.glyph_matrix(code, False)
    return None


class Console:
    """Текстовый курсор и вывод строк глифами СИМ прямо на кадр."""

    def __init__(self, fb: Framebuffer) -> None:
        self.fb = fb
        #: Колонка курсора, 0…`COLS`-1.
        self.col = 0
        #: История выданных строк (`mc7401.history.History`) или `None`.
        #: Заводится фронтендом; кадр при автоскролле кладёт туда
        #: ушедшие ряды.
        self.history = None
        #: Строка курсора, 0…`ROWS`-1; строка 0 — верх экрана.
        self.row = 0
        #: Перо вывода текста (по умолчанию рисующее).
        self.pen = PEN_OR
        #: Виден `ВК` — ждём `ПС` (пара `ВК ПС` — один перевод строки).

    # ── позиционирование ─────────────────────────────────────────────────────

    def goto(self, col: int, row: int) -> None:
        """Поставить курсор в знакомёст (с ограничением по экрану)."""
        self.col = min(max(0, col), COLS - 1)
        self.row = min(max(0, row), ROWS - 1)

    def home(self) -> None:
        """Курсор в левый верхний угол."""
        self.goto(0, 0)

    def cell_rect(self, col: Optional[int] = None,
                  row: Optional[int] = None) -> tuple[int, int, int, int]:
        """Прямоугольник растра знака в знакоместе `(x0, y0, x1, y1)`.

        Знакомест — шаг `CELL_W × CELL_H` (16×20); растр знака
        `RASTER_W × RASTER_H` (14×16) лежит в его левом нижнем углу,
        справа и сверху остаётся зазор.  Координаты Y — вверх.
        """
        c = self.col if col is None else col
        r = self.row if row is None else row
        x0 = c * CELL_W
        y0 = (ROWS - 1 - r) * CELL_H
        return x0, y0, x0 + RASTER_W - 1, y0 + RASTER_H - 1

    def cursor_pixels(self) -> tuple[int, int]:
        """Пиксельные координаты левого нижнего угла знакомёста курсора."""
        x0, y0, _, _ = self.cell_rect()
        return x0, y0

    # ── вывод ───────────────────────────────────────────────────────────────

    def put_char(self, ch: str) -> None:
        """Нарисовать один знак в позиции курсора и сдвинуть курсор.

        Неизвестный символ занимает знакомёст пустым (как пробел).
        """
        mat = glyph_art(ch)
        if mat is not None:
            self._blit(mat)
        self.col += 1
        if self.col >= COLS:
            self.newline()

    def put_code(self, code: int) -> None:
        """Нарисовать знак по коду ДКС (как его показывает `display_dks`)."""
        ch = chars.display_dks(code)
        if ch:
            self.put_char(ch)

    def write(self, data: bytes) -> None:
        """Вывести данные из БЭСМ-6: `ВК`, `ПС` и ETX переводят строку.

        Печатные коды показываются знаками СИМ (`chars.display_dks`),
        прочие управляющие коды игнорируются.  Как и в `tui.py`, `ВК`
        (0x0D) тоже завершает строку — ОС Диспак завершает строки `ВК`;
        подряд идущие признаки перевода (`ВК ПС`) считаются одним.
        """
        prev = 0
        for b in data:
            if b in (CR, LF, ETX):
                # `ВК ПС` и `ПС ВК` — **один** перевод строки, в любом
                # порядке: ДКС так закрывает запись каталога (проверено
                # на живой линии, ответ на `КТЛ`).  Раньше поглощалась
                # только пара `ВК ПС`, и `ПС ВК` давал два перевода —
                # между строками выдачи появлялся пустой ряд.
                # Удвоение того же знака (`ВК ВК`, `ПС ПС`) остаётся
                # двумя переводами — это уже пустая строка.
                if _break_pair(prev, b):
                    prev = b
                    continue
                prev = b
                self.newline()
                continue
            prev = 0
            if b < 0x20:
                continue
            self.put_code(b)

    def write_text(self, text: str) -> None:
        """Вывести готовый текст (символы в форме `display_dks`)."""
        prev = ''
        for ch in text:
            if ch in '\r\n\x03':
                if _break_pair(prev, ch):
                    prev = ch
                    continue
                prev = ch
                self.newline()
                continue
            prev = ''
            if ch.isprintable():
                self.put_char(ch)

    def carriage_return(self) -> None:
        """`ВК` — в начало строки."""
        self.col = 0

    def newline(self) -> None:
        """Новая строка; внизу экрана — автоскролл кадра вверх.

        Ушедшие ряды кладутся в **историю** выдачи (`history.push_rows`),
        если она заведена: иначе верхние строки просто пропадали бы, а
        вернуться к ним было нечем.
        """
        self.col = 0
        self.row += 1
        if self.row >= ROWS:
            gone = self.fb.scroll_up(CELL_H)
            self.row = ROWS - 1
            if self.history is not None:
                self.history.push_line(gone)

    # ── стирание ────────────────────────────────────────────────────────────

    def erase_cell(self) -> None:
        """Стереть знакомёст под курсором (перо стирания)."""
        x0, y0, x1, y1 = self.cell_rect()
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                self.fb.apply(x, y, PEN_ERASE)

    def backspace(self) -> None:
        """`ВК` (Backspace): шаг влево и стирание знакомёста."""
        if self.col > 0:
            self.col -= 1
        self.erase_cell()

    # ── внутреннее ──────────────────────────────────────────────────────────

    def _blit(self, mat) -> None:
        """Нарисовать матрицу знака, увеличенную в `GLYPH_SCALE` раз.

        Строка 0 матрицы — верх знака; каждая точка растра ПЗУ
        зажигает блок `GLYPH_SCALE × GLYPH_SCALE` точек кадра.  Ряд
        рисуется отрезком (`Framebuffer.set_span`), а не по точкам.
        """
        x0, y0, _, _ = self.cell_rect()
        n = GLYPH_SCALE
        for v, row in enumerate(mat):
            for dy in range(n):
                y = y0 + (GLYPH_H - 1 - v) * n + dy
                run = -1
                for u in range(GLYPH_W + 1):
                    on = u < GLYPH_W and row[u]
                    if on and run < 0:
                        run = u
                    elif not on and run >= 0:
                        self.fb.set_span(x0 + run * n, y, x0 + u * n, self.pen)
                        run = -1


#: Знаки, которыми кадр разбирается обратно в текст.  Порядок важен:
#: у знаков-омографов («О» и «о») растр совпадает, и разбор обязан брать
#: первым начертание из этого списка.
ALPHABET = (' .,+-/:;()0123456789'
            'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
            'abcdefghijklmnopqrstuvwxyz'
            'АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ'
            'абвгдежзийклмнопрстуфхцчшщъыьэюя')

#: Таблица «растр знака → знак»; заполняется при первом разборе.
_GLYPH_KEYS: Optional[dict[bytes, str]] = None


def _glyph_table() -> dict:
    """Ключ растра знака → сам знак.

    Ключ — биты знакоместа ровно в том виде, в каком их кладёт
    `put_glyph`.  Сравнивать биты напрямую надёжнее, чем проверять
    растр по координатам: преобразования `Y` вверх и масштаб заданы
    один раз — здесь.
    """
    global _GLYPH_KEYS
    if _GLYPH_KEYS is not None:
        return _GLYPH_KEYS
    step = GLYPH_W + GLYPH_SCALE
    table: dict[bytes, str] = {}
    for ch in ALPHABET:
        fb = Framebuffer(step * GLYPH_SCALE, GLYPH_H * GLYPH_SCALE)
        put_glyph(fb, 0, 0, ch, PEN_OR)
        key = b''.join(fb.row_bytes(y) for y in range(GLYPH_H * GLYPH_SCALE))
        table.setdefault(key, ch)
    _GLYPH_KEYS = table
    return table


def read_row(fb: Framebuffer, y0: int, cols: Optional[int] = None,
             blank: str = ' ') -> str:
    """Знакоместа кадра обратно в строку знаков.

    `y0` — нижняя граница знакоместа в рядах кадра (`Y` вверх).  Знак
    ищется по таблице растров (`_glyph_table`), несовпавшее знакоместо
    даёт `blank`.  Нужен для показа истории выдачи и для проверок.
    """
    table = _glyph_table()
    step = (GLYPH_W + GLYPH_SCALE) * GLYPH_SCALE
    cols = cols or max(0, fb.width // step)
    height = GLYPH_H * GLYPH_SCALE
    out = []
    for col in range(cols):
        x0 = col * step
        key = b''.join(_row_bits(fb, y0 + dy, x0, step)
                       for dy in range(height))
        out.append(table.get(key, blank))
    return ''.join(out)


def _row_bits(fb: Framebuffer, y: int, x0: int, step: int) -> bytes:
    """`step` точек ряда, начиная с `x0`, — байтами (старший бит слева)."""
    out = bytearray((step + 7) // 8)
    for u in range(step):
        if fb.get(x0 + u, y):
            out[u >> 3] |= 1 << (7 - (u & 7))
    return bytes(out)


def put_glyph(fb: Framebuffer, x0: int, y0: int, ch: str,
              pen: str = PEN_OR) -> None:
    """Нарисовать знак `ch` левым нижним углом растра в `(x0, y0)`.

    Растр ПЗУ 7×8 выводится блоком `GLYPH_SCALE × GLYPH_SCALE` — знак на
    кадре крупнее оригинала (по умолчанию вдвое, 14×16; см.
    `[terminal] glyph`).  Общий вывод знака для всех, кто пишет на кадре:
    текстовый слой кадра и служебные строки окна (`chrome`).

    Ряд растра рисуется **отрезком** (`Framebuffer.set_span`), а не по
    точкам: на знак приходится до 224 отдельных `apply()`, и служебные
    полосы окна (почти шестьсот знаков на перерисовку) на этом уходили
    в десятки миллисекунд.
    """
    mat = glyph_art(ch)
    if mat is None:
        return
    n = GLYPH_SCALE
    for v, row in enumerate(mat):
        for dy in range(n):
            y = y0 + (GLYPH_H - 1 - v) * n + dy
            run = -1
            for u in range(GLYPH_W + 1):
                on = u < GLYPH_W and row[u]
                if on and run < 0:
                    run = u
                elif not on and run >= 0:
                    fb.set_span(x0 + run * n, y, x0 + u * n, pen)
                    run = -1



__all__ = ['Console', 'CELL_H', 'CELL_W', 'RASTER_H', 'RASTER_W',
           'GLYPH_SCALE', 'COLS', 'ROWS', 'CR', 'LF', 'glyph_art', 'put_glyph']
