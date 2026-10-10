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


class Framebuffer:
    """Одноцветный (1 бит на точку) кадр 560×400."""

    __slots__ = ("width", "height", "_bits")

    def __init__(self, width: int = SCREEN_WIDTH, height: int = SCREEN_HEIGHT):
        if width * height % 8:
            raise ValueError("размер кадра должен быть кратен 8")
        self.width = width
        self.height = height
        self._bits = bytearray(width * height // 8)

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
            self._bits[i] |= mask
        else:
            self._bits[i] &= ~mask & 0xFF

    def toggle(self, x: int, y: int) -> None:
        if not self.inside(x, y):
            return
        i = self._index(x, y)
        self._bits[i] ^= 1 << (7 - (x & 7))

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
        return sum(bin(b).count('1') for b in self._bits)

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
        return new

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Framebuffer) and self._bits == other._bits

    def __hash__(self) -> int:  # pragma: no cover
        return hash((self.width, self.height, bytes(self._bits)))

    def __repr__(self) -> str:  # pragma: no cover
        bb = self.bbox()
        return (f'Framebuffer({self.width}x{self.height}, '
                f'set={self.count_set()}, bbox={bb})')