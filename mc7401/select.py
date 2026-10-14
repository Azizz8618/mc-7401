"""
select.py — выделение текста мышью, как в терминале 15ИЭ-00-013.

Световое перо работает в двух режимах: рисует по кадру или **выделяет**
текст.  Режим выделения повторяет поведение мыши 15ИЭ
(`ie15/ie15emu/screen.py`: `sel_begin`, `sel_drag`, `sel_end`,
`sel_stats`):

* нажатие задаёт начало выделения, протяжка тянет конец, отпускание
  завершает — получившийся кусок текста становится отдельным
  фрагментом;
* выделение **строчное**: от знака нажатия до конца строки в первой
  строке и от начала строки до знака отпускания в последней, а
  промежуточные строки берутся целиком.  Поэтому протяжка справа
  налево выделяет ровно то же, что и слева направо;
* протяжка с `Ctrl` — **колоночная**: берётся прямоугольник знаков,
  одинаковый для всех рядов;
* фрагментов может быть сколько угодно, пока оператор не снимет их
  клавишей `F7`; размер показан в строке состояния — «ВЫД=СТР 1фр 1с
  12зн», как на 15ИЭ.

Протяжка держится в координатах **кадра**, а не окна: во время
протягивания ЭВМ успевает выдать строки, и привязка к ряду окна уводила
начало на другую строку — выделение разрасталось до половины экрана.

    >>> sel = Selection()
    >>> _ = sel.begin(3, 4)
    >>> _ = sel.drag(3, 11)
    >>> _ = sel.end()
    >>> sel.stats()
    (1, 1, 8)
    >>> sel.label()
    'СТР 1фр 1с 8зн'
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

#: Ширина сетки знакомест берётся из `console` при каждом обращении:
#: `set_glyph_scale` переприсваивает `console.COLS` уже **после** загрузки
#: этого модуля (окно вызывает его в `__init__` по `[terminal] glyph`).
#: Значение по умолчанию в сигнатуре вычислялось бы при определении
#: функции — то есть осталось бы от прежнего масштаба знака, и размер
#: выделения в строке состояния считался бы не по той сетке.
from . import console

#: Вид выделения, как в строке состояния 15ИЭ.
KIND_LINE = 'СТР'          # строчное
KIND_BLOCK = 'КОЛ'         # колоночное

#: Максимум фрагментов: глубже смотреть некуда, а память терминала
#: ограничена.  При переполнении снимается самый старый фрагмент.
MAX_FRAGS = 32


@dataclass
class Fragment:
    """Один завершённый кусок выделения: диапазон знакомест по рядам."""

    row_from: int
    row_to: int
    #: Куда ведёт выделение в первой строке и откуда заканчивает в
    #: последней.  Для колоночного выделения границы одинаковы.
    x_from: int
    x_to: int
    #: Колоночное выделение (протяжка с `Ctrl`): диапазон знаков один
    #: на все ряды, а у строчного промежуточные строки берутся целиком.
    block: bool = False

    def span(self, row: int, cols: int) -> tuple[int, int] | None:
        """Диапазон знакомест в строке `row`: `(x0, x1)`, `x1` не входит.

        Как `_span` на 15ИЭ: строчное выделение берёт у первой строки от
        знака нажатия до конца, у последней — от начала до знака
        отпускания, промежуточные — целиком.  Поэтому протяжка справа
        налево выделяет ровно то же, что и слева направо.  На одной
        строке начало и конец — один диапазон, порядок не важен.
        """
        if not self.row_from <= row <= self.row_to:
            return None
        if self.block or self.row_from == self.row_to:
            return min(self.x_from, self.x_to), max(self.x_from, self.x_to) + 1
        x0 = self.x_from if row == self.row_from else 0
        x1 = (self.x_to + 1) if row == self.row_to else cols
        return x0, x1

    def cells(self, cols: int) -> list[tuple[int, int]]:
        """Знакоместа выделения как `(col, row)`, по рядам сверху вниз."""
        out: list[tuple[int, int]] = []
        for row in range(self.row_from, self.row_to + 1):
            span = self.span(row, cols)
            if span is None:
                continue
            x0, x1 = span
            out.extend((col, row) for col in range(max(0, x0), min(cols, x1)))
        return out


@dataclass
class Selection:
    """Выделения текста мышью: фрагменты и идущая протяжка."""

    #: Завершённые фрагменты, каждый — результат одной протяжки.
    frags: list[Fragment] = field(default_factory=list)
    #: Вид последнего выделения — в строке состояния.
    kind: str = KIND_LINE
    #: Идущая протяжка: `(строка, знак)` начала и конца, колоночная ли.
    _drag: tuple | None = None

    # ── протяжка ────────────────────────────────────────────────────────────

    def begin(self, row: int, col: int, block: bool = False) -> None:
        """Начало протяжки в знакоместе `(col, row)`.

        `block=True` — колоночное выделение (протяжка с `Ctrl`), как на
        15ИЭ; иначе строчное.
        """
        self._drag = (row, col, row, col, block)

    def drag(self, row: int, col: int) -> None:
        """Текущая точка протяжки — конец тянется за указателем."""
        if self._drag is None:
            return
        r0, c0, _, _, block = self._drag
        self._drag = (r0, c0, row, col, block)

    def end(self) -> Fragment | None:
        """Отпускание: протяжка становится фрагментом.

        Возвращает новый фрагмент или `None`, если выделять нечего:
        клик без протяжки даёт один знак, и он тоже берётся — так же
        на 15ИЭ, где быстрый протяг выделяет один знак.
        """
        if self._drag is None:
            return None
        r0, c0, r1, c1, block = self._drag
        self._drag = None
        # Ряд всегда сверху вниз; на одной строке порядок знаков не
        # важен — так же в `_span` на 15ИЭ.
        top, bottom = min(r0, r1), max(r0, r1)
        xa, xb = (c0, c1)
        if r1 < r0:
            xa, xb = c1, c0
        frag = Fragment(top, bottom, xa, xb, block)
        self.kind = KIND_BLOCK if block else KIND_LINE
        self.frags.append(frag)
        while len(self.frags) > MAX_FRAGS:
            self.frags.pop(0)
        return frag

    def cancel(self) -> None:
        """Отменить идущую протяжку: уже выбранные фрагменты остаются.

        Клик мимо текста на 15ИЭ отменяет протяжку, а не выделение.
        """
        self._drag = None

    # ── состояние ───────────────────────────────────────────────────────────

    @property
    def dragging(self) -> bool:
        """Идёт ли протяжка."""
        return self._drag is not None

    def clear(self) -> bool:
        """Снять всё выделение. Возвращает True, если что-то было снято."""
        had = bool(self.frags) or self._drag is not None
        self.frags.clear()
        self._drag = None
        return had

    def empty(self) -> bool:
        """Нет ли выделения (ни завершённого, ни идущего)."""
        return not self.frags and self._drag is None

    def stats(self, cols: Optional[int] = None) -> tuple[int, int, int]:
        """Размер выделения: `(фрагментов, строк, знаков)`.

        Идущая протяжка считается наравне с завершённой: пока оператор
        тянет мышь, он видит, сколько выделяет.  `cols` — ширина сетки
        знакомест: у строчного выделения промежуточные строки берутся
        целиком, и без ширины их длина не считается.
        """
        if cols is None:
            cols = console.COLS
        frags = list(self.frags)
        if self._drag is not None:
            frags.append(self._drag_fragment())
        rows: set[int] = set()
        chars = 0
        for frag in frags:
            for row in range(frag.row_from, frag.row_to + 1):
                span = frag.span(row, cols)
                if span:
                    rows.add(row)
                    chars += max(0, min(cols, span[1]) - max(0, span[0]))
        return len(frags), len(rows), chars

    def cells(self, cols: int) -> set[tuple[int, int]]:
        """Знакоместа выделения как множество `(col, row)`.

        Идущая протяжка входит — иначе выделение было бы видно только
        после отпускания кнопки.
        """
        out: set[tuple[int, int]] = set()
        for frag in self.frags:
            out.update(frag.cells(cols))
        if self._drag is not None:
            out.update(self._drag_fragment().cells(cols))
        return out

    def _drag_fragment(self) -> 'Fragment':
        """Идущая протяжка как фрагмент — для подсветки и размера."""
        r0, c0, r1, c1, block = self._drag
        top, bottom = min(r0, r1), max(r0, r1)
        xa, xb = (c0, c1)
        if r1 < r0:
            xa, xb = c1, c0
        return Fragment(top, bottom, xa, xb, block)

    def label(self) -> str:
        """Подпись для строки состояния: ``СТР 1фр 1с 12зн``."""
        frags, rows_n, chars = self.stats()
        return f'{self.kind} {frags}фр {rows_n}с {chars}зн'


__all__ = ['Selection', 'Fragment', 'KIND_LINE', 'KIND_BLOCK', 'MAX_FRAGS']
