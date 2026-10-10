"""
emulator.py — выполнение команд терминала «Электроника МС 7401».

`TerminalEmulator` держит состояние терминала (текущая позиция, перо,
тип линии, шрифт, режим, смещение окна) и рисует в `Framebuffer`.

Из статьи МПСС 5/1989 (с. 41–43):

* примитивы — отрезок, дуга, окружность, прямоугольник, строка текста;
* координаты задаются **в точках растра**;
* отсечение по краю экрана или по произвольному прямоугольному окну
  со сторонами, параллельными экрану, выполняется **всегда**;
* тип линии задаётся 8-разрядным кодом-шаблоном, который повторяется
  вдоль рисунка отрезка; для штриховой линии обязательна отрисовка
  яркими точками (штрих) и стирание — тусклыми;
* три пера: рисующее, стирающее и инвертирующее.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import chars
#: Набор русских знаков МС 7401 (из двух: Н2/КОИ-7 и КОИ-8Р).
RUSSIAN_SET = 'koi8r'

from .constants import (
    END,
    LINE_PATTERNS,
    LINE_TYPE_DISABLED,
    PEN_BY_CODE,
    PEN_ERASE,
    PEN_OR,
    PEN_XOR,
    S0,
    S1,
    SCREEN_HEIGHT,
    SCREEN_WIDTH,
)
from .framebuffer import Framebuffer
from .parser import Command, CommandParser, Op
from .reader import EventKind

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Состояние терминала
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Window:
    """Прямоугольное окно отсечения в координатах экрана."""

    x0: int = 0
    y0: int = 0
    x1: int = SCREEN_WIDTH - 1
    y1: int = SCREEN_HEIGHT - 1

    def contains(self, x: int, y: int) -> bool:
        return self.x0 <= x <= self.x1 and self.y0 <= y <= self.y1

    def is_empty(self) -> bool:
        return self.x1 < self.x0 or self.y1 < self.y0


def _code_int(value) -> int:
    """Код символа как число.

    Параметры команды `9` приходят из потока как коды символов
    (`СНР(МСШ)`, `СНР(ОРН)`), то есть это байты, а не десятичные числа.
    """
    if isinstance(value, (bytes, bytearray)):
        return value[0] if value else 0
    return int(value)


@dataclass
class TerminalState:
    """Изменяемое состояние терминала."""

    x: int = 0
    y: int = 0
    pen: str = PEN_OR
    line_type: int = 1
    #: Набор знаков: 'n0' | 'n1' | 'n2'; по умолчанию Н2 (VT-52),
    #: как в терминале 15ИЭ.  `S0` включает Н1, `S1` — Н2.
    font: str = chars.DEFAULT_SET
    text_size: int = 0
    text_direction: int = 0
    mode: str = 'TEXT'                # 'TEXT' | 'GRAPHICS'
    window_shift: tuple[int, int] = (0, 0)
    window: Window = field(default_factory=Window)
    #: Счётчик выполненных команд (для отчётов и тестов).
    commands: int = 0

    def clone(self) -> 'TerminalState':
        return TerminalState(
            x=self.x, y=self.y, pen=self.pen, line_type=self.line_type,
            font=self.font, text_size=self.text_size,
            text_direction=self.text_direction, mode=self.mode,
            window_shift=self.window_shift,
            window=Window(self.window.x0, self.window.y0,
                          self.window.x1, self.window.y1),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Эмулятор
# ─────────────────────────────────────────────────────────────────────────────

class TerminalEmulator:
    """Исполнитель команд над растровым кадром."""

    def __init__(self,
                 fb: Optional[Framebuffer] = None,
                 on_frame: Optional[Callable[[Framebuffer], None]] = None,
                 xor_code: Optional[int] = None):
        self.fb = fb if fb is not None else Framebuffer()
        self.state = TerminalState()
        self.parser = (CommandParser() if xor_code is None
                       else CommandParser(xor_code))
        self.on_frame = on_frame
        self.frames = 0
        #: Число блоков ВУК в потоке (границы выдачи).
        self.blocks = 0
        #: Статистика.
        self.stats = {'commands': 0, 'unknown': 0, 'chars': 0}

    # ── основной вход ────────────────────────────────────────────────────────

    def run(self, stream: bytes, *, collect_frames: bool = False):
        """Выполнить поток байтов и вернуть кадр.

        При `collect_frames` возвращается список кадров на конец каждого
        кадра (`АР2 'F‾' ВК`) — так восстанавливается то, что видел
        оператор: программа очищает экран в начале каждой картины.
        """
        from .reader import CommandStreamReader
        frames: list[Framebuffer] = []
        reader = CommandStreamReader()
        reader.feed(stream)
        reader.close()
        for ev in reader.pop():
            if collect_frames and ev.kind is EventKind.FRAME_END:
                frames.append(self.fb.copy())
            self.handle_event(ev)
        if collect_frames:
            return frames, self.fb
        return self.fb

    def feed_events(self, events) -> None:
        for ev in events:
            self.handle_event(ev)

    def handle_event(self, ev) -> None:
        """Обработать событие потока."""
        kind = ev.kind

        if ev.meta.get('defchar'):
            self._define_char(ev.data)
            return
        if kind is EventKind.FRAME_END:
            self.frames += 1
            if self.on_frame:
                self.on_frame(self.fb)
            return
        if kind is EventKind.RU_FONT:
            self.state.font = RUSSIAN_SET
            return
        if kind is EventKind.VUK_BLOCK:
            self.blocks += 1          # блок ВУК — граница выдачи
            return
        if kind is EventKind.PAD:
            return
        if kind is EventKind.COMMAND:
            self.execute(self.parser.parse(ev.data))
            return
        # FRAME_BEGIN, TEXT_LINE, DEFCHAR, DEFROW, DEFDONE, END_OF_STREAM
        # действий не требуют: они уже учтены разбором записи.
        return

    # ── выполнение команды ───────────────────────────────────────────────────

    def execute(self, cmd: Command) -> None:
        self.state.commands += 1
        self.stats['commands'] += 1
        op = cmd.op
        a = cmd.args
        st = self.state

        if op is Op.TEXT_MODE:
            st.mode = 'TEXT'
            return
        if op is Op.GRAPHICS_MODE:
            st.mode = 'GRAPHICS'
            return
        if op is Op.RESET:
            self.reset()
            return
        if op is Op.CLEAR:
            self.fb.clear()
            return
        if op is Op.SET_PEN:
            code = (a[0] or b'0').decode('ascii', 'replace')[:1] or '0'
            st.pen = PEN_BY_CODE.get(code, PEN_OR)
            return
        if op is Op.PEN_OR:
            st.pen = PEN_OR
            return
        if op is Op.PEN_XOR:
            st.pen = PEN_XOR
            return
        if op is Op.PEN_ERASE:
            st.pen = PEN_ERASE
            return
        if op is Op.SET_LINE_TYPE:
            try:
                st.line_type = int((a[0] or b'0').decode().strip() or 0)
            except ValueError:
                st.line_type = 1
            return
        if op is Op.FRAME_END:
            self.frames += 1
            if self.on_frame:
                self.on_frame(self.fb)
            return
        if op is Op.UPDATE_END:
            return

        if op is Op.MOVE_ABS:
            if a[0] is None or a[1] is None:
                return
            self.move_to(int(a[0]), int(a[1]), absolute=True)
            return
        if op is Op.MOVE_REL:
            if a[0] is None or a[1] is None:
                return
            self.move_to(int(a[0]), int(a[1]), absolute=False)
            return
        if op is Op.LINE_ABS:
            if a[1] is None or a[2] is None:
                return
            self.line_to(int(a[1]), int(a[2]),
                         ltype=a[0] if a[0] is not None else st.line_type)
            return
        if op is Op.LINE_REL:
            if a[1] is None or a[2] is None:
                return
            self.line_to(int(a[1]), int(a[2]), absolute=False,
                         ltype=a[0] if a[0] is not None else st.line_type)
            return
        if op is Op.RECT:
            if any(v is None for v in a):
                return
            self.rect(int(a[0]), int(a[1]), int(a[2]), int(a[3]))
            return
        if op is Op.CIRCLE:
            if a[1] is None:
                return
            self.circle(int(a[1]), ltype=a[0] if a[0] is not None else st.line_type)
            return
        if op is Op.TEXT:
            self.draw_text(cmd)
            return
        if op is Op.SET_TEXT_SIZE:
            # Запись `9 : N` задаёт размер для всех следующих строк.
            st.text_size = _code_int(a[0]) if a else 0
            return

        if op is Op.SHIFT_WINDOW:
            self.shift_window(int(a[0] or 0), int(a[1] or 0))
            return

        if op is Op.UNKNOWN:
            self.stats['unknown'] += 1
            log.debug('неизвестная запись: %r', cmd.raw)

    # ── примитивы ────────────────────────────────────────────────────────────

    def reset(self) -> None:
        """Полный сброс терминала (команда `К`)."""
        self.fb.clear()
        st = self.state
        st.x = st.y = 0
        st.pen = PEN_OR
        st.line_type = 1
        st.font = chars.DEFAULT_SET
        st.text_size = 0
        st.text_direction = 0
        st.mode = 'GRAPHICS'
        st.window_shift = (0, 0)
        st.window = Window()

    def move_to(self, x: int, y: int, absolute: bool = True) -> None:
        """Перемещение пера (без рисования)."""
        if absolute:
            self.state.x, self.state.y = x, y
        else:
            self.state.x += x
            self.state.y += y

    def line_to(self, x: int, y: int, absolute: bool = True,
                ltype: Optional[int] = None) -> None:
        """Провести отрезок из текущей точки в (x, y)."""
        st = self.state
        x0, y0 = (st.x, st.y)
        x1, y1 = (x, y) if absolute else (st.x + x, st.y + y)
        self.draw_line(x0, y0, x1, y1, ltype)

    def draw_line(self, x0: int, y0: int, x1: int, y1: int,
                  ltype: Optional[int] = None) -> None:
        """Нарисовать отрезок с учётом шаблона типа линии."""
        st = self.state
        t = st.line_type if ltype is None else int(ltype)
        pattern = LINE_PATTERNS.get(t)
        if pattern is None or t == LINE_TYPE_DISABLED:
            st.x, st.y = x1, y1
            return
        n = max(abs(x1 - x0), abs(y1 - y0))
        self._line_segments(x0, y0, x1, y1, pattern, n)
        st.x, st.y = x1, y1

    def _line_segments(self, x0: int, y0: int, x1: int, y1: int,
                       pattern, n: int) -> None:
        """Разбить отрезок на «включённые» участки по 8-разрядному шаблону."""
        st = self.state
        if n == 0:
            self.plot(x0, y0)
            return
        if all(pattern):
            self._bresenham(x0, y0, x1, y1)
            return
        pen_saved = st.pen
        i = 0
        total = n + 1
        while i < total:
            if pattern[i & 7]:
                # Найти конец непрерывного участка «включённых» битов.
                j = i
                while j + 1 < total and pattern[(j + 1) & 7]:
                    j += 1
                ax = x0 + round((x1 - x0) * i / n)
                ay = y0 + round((y1 - y0) * i / n)
                bx = x0 + round((x1 - x0) * j / n)
                by = y0 + round((y1 - y0) * j / n)
                self._bresenham(ax, ay, bx, by)
                i = j + 1
            else:
                i += 1
        st.pen = pen_saved

    def _bresenham(self, x0: int, y0: int, x1: int, y1: int) -> None:
        """Цифровая прямая Брезенхэма с отсечением по окну."""
        dx = abs(x1 - x0)
        dy = -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        win = self.state.window
        guard = 0
        limit = 4 * (SCREEN_WIDTH + SCREEN_HEIGHT) + 8
        while True:
            if win.contains(x0, y0):
                self.plot(x0, y0)
            if x0 == x1 and y0 == y1:
                break
            guard += 1
            if guard > limit:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def plot(self, x: int, y: int, pen: Optional[str] = None) -> None:
        """Поставить точку пером терминала (с отсечением по окну)."""
        st = self.state
        if not st.window.contains(x, y):
            return
        self.fb.apply(x, y, pen or st.pen)

    def rect(self, x0: int, y0: int, x1: int, y1: int) -> None:
        """Прямоугольник (команда `]`) — рисуется контурами текущим пером."""
        if x0 > x1:
            x0, x1 = x1, x0
        if y0 > y1:
            y0, y1 = y1, y0
        t = self.state.line_type
        self.draw_line(x0, y0, x1, y0, t)
        self.draw_line(x1, y0, x1, y1, t)
        self.draw_line(x1, y1, x0, y1, t)
        self.draw_line(x0, y1, x0, y0, t)
        self.state.x, self.state.y = x0, y0

    def circle(self, r: int, ltype: Optional[int] = None) -> None:
        """Окружность радиуса `r` с центром в текущей точке (команда `>`)."""
        if r <= 0:
            self.plot(self.state.x, self.state.y)
            return
        st = self.state
        t = st.line_type if ltype is None else int(ltype)
        pattern = LINE_PATTERNS.get(t)
        pen_saved = st.pen
        steps = max(16, int(r * 8))
        prev = None
        for i in range(steps + 1):
            ang = 2.0 * 3.141592653589793 * i / steps
            px = round(st.x + r * _cos(ang))
            py = round(st.y + r * _sin(ang))
            if pattern is not None and not pattern[i & 7]:
                prev = None
                continue
            if prev is None:
                self.plot(px, py)
            else:
                self._bresenham(prev[0], prev[1], px, py)
            prev = (px, py)
        st.pen = pen_saved

    # ── текст ────────────────────────────────────────────────────────────────

    def draw_text(self, cmd: Command) -> None:
        """Вывести строку текста (команда `9`)."""
        st = self.state
        size = st.text_size
        direction = st.text_direction
        if len(cmd.args) >= 1 and cmd.args[0]:
            try:
                size = _code_int(cmd.args[0])
            except (TypeError, ValueError):
                pass
        if len(cmd.args) >= 2 and cmd.args[1]:
            try:
                direction = _code_int(cmd.args[1])
            except (TypeError, ValueError):
                direction = 0
        russian = st.font == RUSSIAN_SET
        # Нулевой размер в потоке означает «мелкий знак», а не отсутствие
        # знака: без этого глифы вырождаются в одну точку.
        size = max(1, size)
        payload = self._prepare_payload(cmd.payload, russian)
        if not payload:
            return
        self.stats['chars'] += len(payload)
        ux, uy = chars.text_advance(direction, size)
        pen_saved = st.pen
        for n, code in enumerate(payload):
            mat = chars.glyph_matrix(code, russian)
            for dx, dy in chars.text_offsets(mat, direction, size):
                self.plot(round(st.x + ux * n + dx), round(st.y + uy * n + dy))
        # После строки текста терминал перемещает маркер по её концу.
        st.x = round(st.x + ux * len(payload))
        st.y = round(st.y + uy * len(payload))
        st.pen = pen_saved

    def _prepare_payload(self, payload: bytes, russian: bool) -> list[int]:
        """Разобрать байты строки: S0/S1 переключают набор знаков, 0o240 — пропуск."""
        out: list[int] = []
        for b in payload:
            if b == END:                        # признак конца текста
                break
            if b in (S0, S1):                   # S0 — набор Н1, S1 — Н2
                name = chars.set_for_code(b)
                if name is not None:
                    self.state.font = name
                    russian = (name == 'n1')
                continue
            if b in (0o124, 0o125, 0o33):       # служебные
                continue
            out.append(b)
        return out

    # ── окно ─────────────────────────────────────────────────────────────────

    def shift_window(self, dx: int, dy: int) -> None:
        """Сместить окно отсечения (команда `LF`)."""
        st = self.state
        st.window_shift = (st.window_shift[0] + dx, st.window_shift[1] + dy)
        ox, oy = st.window_shift
        st.window = Window(ox, oy, ox + SCREEN_WIDTH - 1, oy + SCREEN_HEIGHT - 1)

    # ── пользовательские знаки ───────────────────────────────────────────────

    def _define_char(self, data: bytes) -> None:
        """Загрузить пользовательский знак (команда `≡110`)."""
        if len(data) < 9:
            return
        code = data[1] & 0o77
        rows = list(data[2:9])
        try:
            chars.USER_TABLE.define(code, rows)
        except (IndexError, ValueError) as exc:      # pragma: no cover
            log.debug('знак не загружен: %s', exc)

    # ── снимок состояния ─────────────────────────────────────────────────────

    def save_state(self) -> bytes:
        """Сохранить состояние терминала (аппаратная возможность МС-7401).

        Команды «запомнить состояние» / «восстановить состояние»
        (статья МПСС 5/1989, рис. 2) хранят текущую позицию, тип линии
        и выбранное перо в стеке терминала.
        """
        st = self.state
        return bytes((st.x & 0xFF, (st.x >> 8) & 0x0F,
                      st.y & 0xFF, (st.y >> 8) & 0x0F,
                      st.line_type & 0x0F,
                      {PEN_OR: 0, PEN_XOR: 1, PEN_ERASE: 2}[st.pen] & 0x0F,
                      1 if st.font == RUSSIAN_SET else 0))

    def restore_state(self, blob: bytes) -> None:
        st = self.state
        st.x = blob[0] | (blob[1] & 0x0F) << 8
        st.y = blob[2] | (blob[3] & 0x0F) << 8
        st.line_type = blob[4] & 0x0F
        st.pen = (PEN_OR, PEN_XOR, PEN_ERASE)[blob[5] & 0x0F]
        st.font = RUSSIAN_SET if blob[6] else chars.DEFAULT_SET


# ─────────────────────────────────────────────────────────────────────────────
# Мелкие помощники
# ─────────────────────────────────────────────────────────────────────────────

def _cos(a: float) -> float:
    import math
    return math.cos(a)


def _sin(a: float) -> float:
    import math
    return math.sin(a)