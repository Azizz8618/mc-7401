"""
pen.py — световое перо и его эмуляция мышью.

Световое перо — указатель, которым оператор работает прямо по экрану
graphic-дисплея: нажатие даёт событие в координатах кадра. На реальном
МС7401 такого устройства нет: статья «Системы команд растрового
графического дисплея „Электроника МС7401“» (Лацис, Штаркман; МПСС,
1989, № 5, стр. 39) говорит, что внешними устройствами встроенной ЭВМ
являются «клавиатура и линия связи с основной ЭВМ».

Поэтому перо реализовано как возможность фронтенда: мышь терминала
хоста работает как световое перо, а координаты передаются по линии
связи. Кодирование на линии задаётся здесь и помечено как
`PROVISIONAL` — в имеющихся источниках его нет.

    >>> pen = LightPen()
    >>> pen.move_cell(40, 12, cols=80, rows=24)
    (200, 200)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .framebuffer import Framebuffer

#: Последовательность SGR-мыши: `\x1b[<b;x;ym` / `\x1b[<b;x;yM`.
#: Нижняя «m» — отпускание, верхняя «M» — нажатие или движение.
_MOUSE_RE = re.compile(r'^\x1b\[<(\d+);(\d+);(\d+)([Mm])$')

#: Кодирование события пера на линии связи. ПРОВИЗОРИАЛЬНОЕ: в
#: источниках не найдено; заменяется, как только появится описание.
PEN_PREFIX = '\x1b['


@dataclass
class PenEvent:
    """Событие светового пера в координатах кадра."""

    x: int                 # 0..559, X вправо
    y: int                 # 0..399, Y вверх
    down: bool             # перо прижато
    button: int = 0

    def encode(self) -> bytes:
        """Представить событие для отправки на ЭВМ (PROVISIONAL)."""
        return (f'{PEN_PREFIX}{int(self.down)};{self.x};{self.y}M'
                ).encode('ascii')


@dataclass
class LightPen:
    """Мышь терминала хоста как световое перо.

    Переводит ячейку терминала в пиксель кадра с учётом текущего
    окна просмотра и масштаба, поэтому перо «попадает» туда же, куда
    указал пользователь, при любом масштабе и смещении.
    """

    def __init__(self, fb: Optional[Framebuffer] = None) -> None:
        self.fb = fb or Framebuffer()
        self.x = 0
        self.y = 0
        self.down = False
        self.button = 0
        #: Предыдущая позиция при протяжке — от неё тянется штрих.
        self._last: Optional[tuple[int, int]] = None
        #: События, накопленные с последнего чтения.  Поле объявлено в
        #: классе как `field(...)`, но `__init__` здесь свой, поэтому
        #: заполняется вручную.
        self.events: list[PenEvent] = []
        self.enabled = False

    # ── включение / выключение ──────────────────────────────────────────────

    @staticmethod
    def enable_sequence() -> str:
        """Включить отчётность мыши в терминале (SGR, режим 1002)."""
        return '\x1b[?1000h\x1b[?1002h\x1b[?1006h'

    @staticmethod
    def disable_sequence() -> str:
        return '\x1b[?1006l\x1b[?1002l\x1b[?1000l'

    # ── ввод ────────────────────────────────────────────────────────────────

    @staticmethod
    def parse_mouse(data: str) -> Optional[tuple[int, int, int, bool]]:
        """Разобрать `\x1b[<b;x;ym` → (кнопка, столбец, строка, отпускание).

        `b` кодирует состояние: младший бит — левая кнопка, бит 2 (4) —
        отпускание, бит 32 (32) — перемещение с зажатой кнопкой.
        """
        m = _MOUSE_RE.match(data)
        if not m:
            return None
        b, x, y, tail = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
        return b, x, y, (tail == 'm' or bool(b & 4))

    def move_cell(self, col: int, row: int, *, cols: int, rows: int,
                  scale: int = 1, vx: int = 0, vy: int = 0,
                  button: int = 0, pressed: bool = False) -> tuple[int, int]:
        """Перевести ячейку терминала в пиксель кадра.

        Строки идут сверху вниз, кадр использует `Y` вверх.
        """
        px = vx + max(0, col - 1) * scale
        py = vy + max(0, row - 1) * scale * 2
        self.x = max(0, min(self.fb.width - 1, px))
        self.y = max(0, min(self.fb.height - 1, self.fb.height - 1 - py))
        self.button = button
        self.down = pressed
        self.events.append(PenEvent(self.x, self.y, pressed, button))
        return self.x, self.y

    def feed_mouse(self, data: str, **kw) -> Optional[tuple[int, int]]:
        """Обработать escape-последовательность мыши."""
        parsed = self.parse_mouse(data)
        if parsed is None:
            return None
        b, col, row, released = parsed
        btn = b & 3
        # В режиме SGR при протяжке биты кнопки равны нулю (движение —
        # бит 32), поэтому «прижато» липкое: нажатие включает, отпускание
        # выключает, а движение состояние сохраняет. Так же ведёт себя
        # настоящее световое перо.
        if released:
            pressed = False
        elif btn:
            pressed = True
        else:
            pressed = self.down
        cols, rows = kw.get('cols', 80), kw.get('rows', 24)
        return self.move_cell(col, row, cols=cols, rows=rows,
                              scale=kw.get('scale', 1),
                              vx=kw.get('vx', 0), vy=kw.get('vy', 0),
                              button=btn, pressed=pressed)

    def take_events(self) -> list[PenEvent]:
        evs, self.events = self.events, []
        return evs

    # ── рисование ───────────────────────────────────────────────────────────

    def stroke(self, fb: Framebuffer) -> bool:
        """Продлить штрих от предыдущей позиции к текущей.

        Так работает перо на векторном экране: при протяжке с нажатой
        кнопкой рисуется непрерывная линия.
        """
        if not self.down or self._last is None:
            self._last = (self.x, self.y)
            return False
        x0, y0 = self._last
        dx, dy = self.x - x0, self.y - y0
        steps = max(abs(dx), abs(dy))
        if steps == 0:
            return False
        for i in range(1, steps + 1):
            fb.point(x0 + round(dx * i / steps), y0 + round(dy * i / steps))
        self._last = (self.x, self.y)
        return True

    def crosshair(self, fb: Framebuffer, size: int = 6) -> None:
        """Нарисовать перекрестье в текущей точке (обзор поверх картинки)."""
        for d in range(-size, size + 1):
            fb.point(self.x + d, self.y)
            fb.point(self.x, self.y + d)
        if self.down:
            fb.point(self.x + size + 1, self.y)
            fb.point(self.x, self.y + size + 1)


__all__ = ['PenEvent', 'LightPen', 'PEN_PREFIX']