"""
framebuffer.py — растровый кадр терминала «Электроника МС 7401».

Экран 560×400 точек, одна точка — один бит (как в оригинале: 7 точек
на байт видеопамяти, ≈32 Кбайт). Система координат терминала:

* начало — левый нижний угол,
* `X` растёт вправо, `Y` — вверх.

Хранение — построчно: `bytearray` длиной 560*400/8 = 28000 байт,
совпадающий с порядком «слева направо, снизу вверх» (как в видеопамяти
оригинального дисплея). Для удобства рисования индекс точки вычисляется
через `_index()`.
"""

from __future__ import annotations

import logging
from typing import Iterable, Iterator

from .constants import PEN_ERASE, PEN_OR, PEN_XOR, SCREEN_HEIGHT, SCREEN_WIDTH

log = logging.getLogger(__name__)



def _byte_runs() -> tuple[tuple[tuple[int, int], ...], ...]:
    """Для каждого значения байта — серии зажжённых битов внутри него.

    Байт хранит 8 точек, старший бит — левая, поэтому бит 7 отдаёт
    отрезок `(0, 1)`.  Таблица строится один раз: `Framebuffer.runs()`
    идёт по ней, не трогая биты по одному.
    """
    table = []
    for v in range(256):
        segs = []
        start = None
        for k in range(8):
            if v & (1 << (7 - k)):
                if start is None:
                    start = k
            elif start is not None:
                segs.append((start, k))
                start = None
        if start is not None:
            segs.append((start, 8))
        table.append(tuple(segs))
    return tuple(table)


_BYTE_RUNS = _byte_runs()

#: Сколько единичных битов в каждом значении байта.  Через `translate`
#: считается за один проход на C — `bin(b).count('1')` в цикле обходился
#: в сотни раз дороже, а подсчёт зажжённых точек нужен при каждой правке
#: кадра и при сборке окна истории.
_POPCOUNT = bytes(i.bit_count() for i in range(256))


def popcount(data) -> int:
    """Число единичных битов в последовательности байтов."""
    return sum(bytes(data).translate(_POPCOUNT))


class Framebuffer:
    """Одноцветный (1 бит на точку) кадр 560×400."""

    __slots__ = ("width", "height", "_bits", "_count")

    def __init__(self, width: int = SCREEN_WIDTH, height: int = SCREEN_HEIGHT):
        if width * height % 8:
            raise ValueError("размер кадра должен быть кратен 8")
        self.width = width
        self.height = height
        self._bits = bytearray(width * height // 8)
        #: Число зажжённых точек (считается инкрементально).
        self._count = 0

    # ── адресация ────────────────────────────────────────────────────────────

    def _index(self, x: int, y: int) -> int:
        """Индекс бита в кадре. `y` отсчитывается снизу."""
        return ((self.height - 1 - y) * self.width + x) >> 3

    # ── элементарный доступ ──────────────────────────────────────────────────

    def inside(self, x: int, y: int) -> bool:
        """Находится ли точка в пределах экрана.

        Отсечение выполняется терминалом всегда и не отключается
        (статья МПСС 5/1989, с. 42).
        """
        return 0 <= x < self.width and 0 <= y < self.height

    def get(self, x: int, y: int) -> int:
        if not self.inside(x, y):
            return 0
        i = self._index(x, y)
        return (self._bits[i] >> (7 - (x & 7))) & 1

    def set(self, x: int, y: int, value: int) -> None:
        if not self.inside(x, y):
            return
        i = self._index(x, y)
        mask = 1 << (7 - (x & 7))
        if value:
            if not self._bits[i] & mask:
                self._bits[i] |= mask
                self._count += 1
        else:
            if self._bits[i] & mask:
                self._bits[i] &= ~mask & 0xFF
                self._count -= 1

    def toggle(self, x: int, y: int) -> None:
        if not self.inside(x, y):
            return
        i = self._index(x, y)
        mask = 1 << (7 - (x & 7))
        self._bits[i] ^= mask
        if self._bits[i] & mask:
            self._count += 1
        else:
            self._count -= 1

    def apply(self, x: int, y: int, pen: str) -> None:
        """Применить перо к точке.

        pen: ``OR``   — установить точку,
             ``XOR``  — инвертировать,
             ``ERASE`` — снять точку.
        """
        if pen == PEN_OR:
            self.set(x, y, 1)
        elif pen == PEN_XOR:
            self.toggle(x, y)
        else:
            self.set(x, y, 0)

    # ── массовые операции ────────────────────────────────────────────────────

    def clear(self) -> None:
        self._bits = bytearray(len(self._bits))
        self._count = 0

    def row_bytes(self, y: int) -> bytes:
        """Ряд кадра битами, слева направо (`stride` байт).

        Ряд хранится побайтно, а не по точкам: историю выдачи держат
        именно такие ряды, и вернуть их на экран — положить байты
        обратно (`put_row_bytes`), без разбора по знакам.
        """
        if not 0 <= y < self.height:
            return bytes(self.width // 8)
        stride = self.width // 8
        base = ((self.height - 1 - y) * self.width) >> 3
        return bytes(self._bits[base:base + stride])

    def put_row_bytes(self, y: int, data: bytes) -> None:
        """Положить ряд битами, как его вернул `row_bytes`."""
        if not 0 <= y < self.height:
            return
        stride = self.width // 8
        data = bytes(data)[:stride]
        if not data:
            return
        base = ((self.height - 1 - y) * self.width) >> 3
        old = self._bits[base:base + stride]
        self._bits[base:base + stride] = data
        self._count += popcount(data) - popcount(old)

    def top_bits(self, rows: int) -> bytes:
        """Верхние `rows` рядов кадра битами, **сверху вниз**.

        Память кадра и так уложена сверху вниз (`_index`), так что верх
        кадра — это её начало: его можно взять одним срезом, без сборки
        ряд за рядом.  Нужен для показа окна истории, где к строкам
        истории снизу приклеивается верх кадра.
        """
        stride = self.width // 8
        rows = max(0, min(rows, self.height))
        return bytes(self._bits[:rows * stride])

    @classmethod
    def from_bits(cls, width: int, height: int, bits) -> 'Framebuffer':
        """Кадр из готовой памяти: `bits` — ряды сверху вниз.

        Обратная сторона `top_bits`: окно истории собирается одним буфером
        (строки истории, к которым снизу приклеен верх кадра), а не
        построчной укладкой — так окно собирается за доли миллисекунды
        вместо десятков.
        """
        fb = cls(width, height)
        data = bytes(bits)[:len(fb._bits)]
        fb._bits[:len(data)] = data
        fb._count = popcount(data)
        return fb

    def scroll_up(self, rows: int) -> list[bytes]:
        """Сдвинуть кадр вверх, снизу — пустая полоса; вернуть ушедшие ряды.

        Так работает автоскролл текста: содержимое уходит к верху экрана,
        внизу освобождается строка под новый текст.  Возвращённые ряды
        (сверху вниз) уходят в **историю** выдачи — иначе верхние строки
        просто пропадали бы.  Строки хранятся сверху вниз (`_index`),
        поэтому сдвиг идёт в сторону начала массива.
        """
        if rows <= 0:
            return []
        stride = self.width // 8
        rows = min(rows, self.height)
        shift = rows * stride
        dropped = [bytes(self._bits[i * stride:(i + 1) * stride])
                   for i in range(rows)]
        self._bits[0:len(self._bits) - shift] = self._bits[shift:]
        self._bits[len(self._bits) - shift:] = bytes(shift)
        self._count -= popcount(b''.join(dropped))
        if self._count < 0:                      # pragma: no cover
            self._count = 0
        return dropped

    def point(self, x: int, y: int, pen: str = PEN_OR) -> None:
        self.apply(x, y, pen)

    def hline(self, x0: int, x1: int, y: int, pen: str = PEN_OR) -> None:
        if x0 > x1:
            x0, x1 = x1, x0
        if not self.inside(x0, y) and not self.inside(x1, y):
            return
        for x in range(max(x0, 0), min(x1, self.width - 1) + 1):
            self.apply(x, y, pen)

    def vline(self, y0: int, y1: int, x: int, pen: str = PEN_OR) -> None:
        if y0 > y1:
            y0, y1 = y1, y0
        for y in range(max(y0, 0), min(y1, self.height - 1) + 1):
            self.apply(x, y, pen)

    def blit(self, x: int, y: int, rows: Iterable[Iterable[int]], pen: str = PEN_OR) -> None:
        """Наложить битовую матрицу (строки от верхней к нижней, MSB=left)."""
        for dy, row in enumerate(rows):
            yy = y + dy
            for dx, bit in enumerate(row):
                if bit:
                    self.apply(x + dx, yy, pen)

    def row_runs(self, y: int) -> list[tuple[int, int]]:
        """Серии зажжённых точек в одном ряду: `(x0, x1)`, `x1` не входит.

        Рендер окна просматривает кадр по ячейкам знакоместа; обход по
        точкам давал 20 мс на кадр, а по сериям — единицы: на ячейку
        приходится сравнить пару отрезков вместо `scale²` вызовов `get()`.
        """
        if not 0 <= y < self.height:
            return []
        base = ((self.height - 1 - y) * self.width) >> 3
        bits = self._bits
        stride = self.width // 8
        segs: list[tuple[int, int]] = []
        for i in range(stride):
            v = bits[base + i]
            if v:
                b = i << 3
                segs.extend((b + s, b + e) for s, e in _BYTE_RUNS[v])
        if not segs:
            return []
        out: list[tuple[int, int]] = []
        start, end = segs[0]
        for s, e in segs[1:]:
            if s <= end:
                if e > end:
                    end = e
            else:
                out.append((start, end))
                start, end = s, e
        out.append((start, end))
        return out

    def set_span(self, x0: int, y: int, x1: int, pen: str = PEN_OR) -> None:
        """Перо на горизонтальном отрезке `x0…x1` (правый край не входит).

        Знак ПЗУ выводится блоками по `GLYPH_SCALE` точек, и на знак
        приходится сотня отдельных `set()`.  Отрезок же берётся целиком,
        по одному байту на восемь точек: служебные полосы окна рисуются
        знаками и без этого съедали всю перерисовку.
        """
        if not 0 <= y < self.height:
            return
        x0 = max(0, x0)
        x1 = min(self.width, x1)
        if x0 >= x1:
            return
        base = ((self.height - 1 - y) * self.width) >> 3
        first = x0 >> 3
        last = (x1 - 1) >> 3
        for i in range(first, last + 1):
            lo = (x0 & 7) if i == first else 0
            tail = x1 & 7
            hi = tail if (i == last and tail) else 8
            if hi <= lo:
                continue
            # В байте бит 7 — самая левая точка, поэтому отрезку
            # [lo, hi) отвечают биты 8-hi … 7-lo.
            mask = ((1 << (hi - lo)) - 1) << (8 - hi)
            cur = self._bits[base + i]
            if pen == PEN_OR:
                added = mask & ~cur & 0xFF
                if added:
                    self._bits[base + i] = cur | mask
                    self._count += bin(added).count('1')
            elif pen == PEN_XOR:
                flipped = (cur & mask) ^ mask
                self._bits[base + i] = cur ^ mask
                self._count += bin(flipped).count('1') \
                    - bin(cur & mask).count('1')
            else:
                dropped = cur & mask
                if dropped:
                    self._bits[base + i] = cur & ~mask & 0xFF
                    self._count -= bin(dropped).count('1')

    def strip_border(self, cover: float = 0.9) -> bool:
        """Снять прямоугольную рамку, очерченную почти по краям кадра.

        Некоторые эталонные рисунки обведены рамкой (`graf.pas`, КОТ:
        `ХLN` по трём сторонам квадрата).  На заставке рамка не нужна —
        она отрезает рисунок от краёв кадра и читается как граница окна.
        Сам эталон не трогаем: рамка снимается только на копии, из
        которой строится заставка.

        Рамкой считается **замкнутый** прямоугольник, у которого стороны
        зажжены не меньше чем на `cover` длины, а вокруг него поля
        пусты.  Последнее условие — главное: оно отличает рамку от любой
        горизонтальной черты внутри рисунка, поэтому содержимое кадра
        снять нельзя.  Возвращает `True`, если рамка была и снята.
        """
        w, h = self.width, self.height
        need_x = int(w * cover)
        need_y = int(h * cover)
        rows = [y for y in range(h)
                if sum(self.get(x, y) for x in range(w)) >= need_x]
        cols = [x for x in range(w)
                if sum(self.get(x, y) for y in range(h)) >= need_y]
        if len(rows) < 2 or len(cols) < 2:
            return False
        top, bottom = rows[0], rows[-1]
        left, right = cols[0], cols[-1]
        if right - left < 2 or bottom - top < 2:
            return False
        # Стороны замкнуты: каждый ряд между углами зажжён по краям.
        for y in range(top, bottom + 1):
            if not self.get(left, y) or not self.get(right, y):
                return False
        for x in range(left, right + 1):
            if not self.get(x, top) or not self.get(x, bottom):
                return False
        # Вокруг рамки пусто — иначе это часть рисунка, а не рамка.
        for y in list(range(top)) + list(range(bottom + 1, h)):
            if any(self.get(x, y) for x in range(w)):
                return False
        for x in list(range(left)) + list(range(right + 1, w)):
            if any(self.get(x, y) for y in range(h)):
                return False
        for y in (top, bottom):
            self.erase_rows(y, y + 1)
        for x in (left, right):
            for y in range(top, bottom + 1):
                self.apply(x, y, PEN_ERASE)
        return True

    def erase_rows(self, y_from: int, y_to: int) -> None:
        """Стереть точки в рядах `y_from…y_to` (координаты Y вверх).

        Служебные полосы окна (строка состояния, подвал) ложатся поверх
        кадра и каждый раз стирают своё место.  Обход по точкам на полосе
        560×340 — это 190 000 вызовов `apply()`, и на нём уходила вся
        перерисовка; здесь гасится нужными байтами сразу.
        """
        y_from = max(0, y_from)
        y_to = min(self.height, y_to)
        if y_from >= y_to:
            return
        stride = self.width // 8
        top = (self.height - y_to) * stride      # Y вверх → строки снизу
        bottom = (self.height - y_from) * stride
        dropped = self._bits[top:bottom]
        self._bits[top:bottom] = bytes(len(dropped))
        self._count -= sum(bin(b).count('1') for b in dropped)
        if self._count < 0:                      # pragma: no cover
            self._count = 0

    # ── серии зажжённых точек ──────────────────────────────────────────────

    def runs(self) -> Iterator[tuple[int, int, int]]:
        """Горизонтальные серии зажжённых точек: `(x0, y, x1)`, `x1` не входит.

        Это то, что окно отдаёт в `PhotoImage.put`, поэтому считать его
        нужно на каждой перерисовке и **часто**.  Обход по точкам здесь
        давал 120 мс на кадр 560×400 (224 000 вызовов `get()`), и ввод
        с клавиатуры из-за этого заметно отставал.  Обход по байтам —
        в 70 раз меньше операций (по одному на байт строки) и те же
        серии: 1.6 мс.
        """
        stride = self.width // 8
        bits = self._bits
        for y in range(self.height):
            base = ((self.height - 1 - y) * self.width) >> 3
            segs: list[tuple[int, int]] = []
            for i in range(stride):
                v = bits[base + i]
                if v:
                    b = i << 3
                    segs.extend((b + s, b + e) for s, e in _BYTE_RUNS[v])
            if not segs:
                continue
            start, end = segs[0]
            for s, e in segs[1:]:
                if s <= end:                    # серии слиплись — склеить
                    if e > end:
                        end = e
                else:
                    yield start, y, end
                    start, end = s, e
            yield start, y, end

    # ── выгрузка ─────────────────────────────────────────────────────────────

    def to_rows_bottom_up(self) -> Iterator[bytearray]:
        """Побайтовые строки кадра снизу вверх (7 бит на байт, как в ВП)."""
        stride = self.width // 8
        for row in range(self.height - 1, -1, -1):
            base = row * stride
            yield self._bits[base:base + stride]

    def to_array_bottom_up(self) -> bytearray:
        """Кадр как массив байтов (560*400/8 = 28000 байт), снизу вверх."""
        stride = self.width // 8
        out = bytearray(stride * self.height)
        for row in range(self.height):
            dst = (self.height - 1 - row) * stride
            src = row * stride
            out[dst:dst + stride] = self._bits[src:src + stride]
        return out

    def count_set(self) -> int:
        return self._count

    def bbox(self) -> tuple[int, int, int, int] | None:
        """Границы ненулевых точек: (xmin, ymin, xmax, ymax) или None."""
        xmin, ymin = self.width, self.height
        xmax = ymax = -1
        stride = self.width // 8
        for row in range(self.height):
            base = row * stride
            chunk = self._bits[base:base + stride]
            if not any(chunk):
                continue
            for byte in range(stride):
                b = chunk[byte]
                if not b:
                    continue
                for bit in range(8):
                    if b & (1 << (7 - bit)):
                        x = byte * 8 + bit
                        if x < xmin:
                            xmin = x
                        if x > xmax:
                            xmax = x
                        if row < ymin:
                            ymin = row
                        if row > ymax:
                            ymax = row
        if xmax < 0:
            return None
        return xmin, ymin, xmax, ymax

    def copy(self) -> 'Framebuffer':
        new = Framebuffer.__new__(Framebuffer)
        new.width = self.width
        new.height = self.height
        new._bits = bytearray(self._bits)
        new._count = self._count
        return new

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Framebuffer) and self._bits == other._bits

    def __hash__(self) -> int:  # pragma: no cover
        return hash((self.width, self.height, bytes(self._bits)))

    def __repr__(self) -> str:  # pragma: no cover
        bb = self.bbox()
        return (f'Framebuffer({self.width}x{self.height}, '
                f'set={self.count_set()}, bbox={bb})')