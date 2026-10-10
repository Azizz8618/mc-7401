"""
emulator.py — эмулятор терминала «Электроника МС 7401».

Модуль связывает разобранные команды (Command) с состоянием
(TerminalState) и backend'ом отрисовки (SVGBackend / TkBackend /
PygameBackend).

Основной класс:
    TerminalEmulator — исполняет команды одну за другой, обновляет
    состояние и вызывает методы backend'а.

Backend'ы должны реализовать следующий интерфейс:

    draw_line(x1, y1, x2, y2, line_type)
    draw_circle(cx, cy, r, line_type)
    draw_rect(x1, y1, x2, y2, line_type)
    draw_text(x, y, text, size, orient, font)
    clear()
    set_mode(mode)          # опционально: OR/XOR/ERASE
    present()               # опционально: отобразить кадр

Координаты передаются в «экранной» системе (Y вверх, левый нижний
угол — начало). Backend сам решает, как их преобразовывать в свою
систему (SVG инвертирует Y, Pygame — нет).

Автор: <ваше имя>
Лицензия: MIT
"""

from __future__ import annotations

import logging
import math
from typing import Optional, Protocol, runtime_checkable

from parser import Command
from state import (
    TerminalState,
    MODE_OR, MODE_XOR, MODE_ERASE,
    FONT_EN, FONT_RU,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Протокол backend'а
# ─────────────────────────────────────────────────────────────────────────────

@runtime_checkable
class Backend(Protocol):
    """Минимальный интерфейс backend'а отрисовки."""

    state: TerminalState

    def draw_line(self, x1: int, y1: int, x2: int, y2: int, line_type: int) -> None: ...
    def draw_circle(self, cx: int, cy: int, r: int, line_type: int) -> None: ...
    def draw_rect(self, x1: int, y1: int, x2: int, y2: int, line_type: int) -> None: ...
    def draw_text(self, x: int, y: int, text: str, size: int, orient: int, font: str) -> None: ...
    def clear(self) -> None: ...


# ─────────────────────────────────────────────────────────────────────────────
# Эмулятор
# ─────────────────────────────────────────────────────────────────────────────

class TerminalEmulator:
    """
    Исполняет команды, обновляя состояние и вызывая backend.

    Пример:
        state = TerminalState()
        backend = SVGBackend(state)
        emu = TerminalEmulator(state, backend)
        for cmd in parser:
            emu.execute(cmd)
    """

    __slots__ = ("state", "backend", "_stats")

    def __init__(self, state: TerminalState, backend: Backend):
        self.state = state
        self.backend = backend
        # счётчики для отчёта
        self._stats: dict[str, int] = {}

    # ─── Публичный API ───────────────────────────────────────────────────────

    def execute(self, cmd: Command) -> None:
        """Исполнить одну команду."""
        op = cmd.op
        self._stats[op] = self._stats.get(op, 0) + 1

        # Пустая команда (двойной ВК) — просто игнорируем.
        if not op:
            return

        handler = self._HANDLERS.get(op)
        if handler is None:
            log.debug("Неизвестная команда %r (args=%r)", op, cmd.args)
            return

        try:
            handler(self, cmd)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "Ошибка выполнения команды #%d %r: %s",
                cmd.index, cmd, exc,
            )
            raise

    @property
    def stats(self) -> dict[str, int]:
        """Статистика по типам команд (для отчёта)."""
        return dict(self._stats)

    # ─── Обработчики команд ──────────────────────────────────────────────────

    # J0 / J1 — переключение режима терминала.
    def _op_J0(self, cmd: Command) -> None:
        self.state.set_graphics(False)

    def _op_J1(self, cmd: Command) -> None:
        self.state.set_graphics(True)

    # 0 — перо/цвет.
    def _op_0(self, cmd: Command) -> None:
        if cmd.args:
            self.state.color = cmd.args[0]

    # 1 — тип линии.
    def _op_1(self, cmd: Command) -> None:
        if cmd.args:
            self.state.set_line_type(cmd.args[0])

    # 2 — конец кадра. Ничего не делает, кроме, возможно, present().
    def _op_2(self, cmd: Command) -> None:
        present = getattr(self.backend, "present", None)
        if callable(present):
            present()

    # 4 — абсолютное перемещение.
    def _op_4(self, cmd: Command) -> None:
        if len(cmd.args) < 2:
            log.warning("Команда 4 без координат")
            return
        self.state.x, self.state.y = cmd.args[0], cmd.args[1]

    # 5 — относительное перемещение.
    def _op_5(self, cmd: Command) -> None:
        if len(cmd.args) < 2:
            log.warning("Команда 5 без смещений")
            return
        self.state.x += cmd.args[0]
        self.state.y += cmd.args[1]

    # 7 — линия к абсолютной точке.
    def _op_7(self, cmd: Command) -> None:
        if len(cmd.args) < 3:
            log.warning("Команда 7 без координат")
            return
        line_type, x2, y2 = cmd.args[0], cmd.args[1], cmd.args[2]

        x1, y1 = self.state.abs_pos()
        # координаты в командах — без смещения окна
        ax2, ay2 = x2 + self.state.shift_x, y2 + self.state.shift_y

        self._draw_line(x1, y1, ax2, ay2, line_type)

        # обновляем логическую позицию
        self.state.x, self.state.y = x2, y2
        self.state.set_line_type(line_type)

    # 8 — линия относительно точки.
    def _op_8(self, cmd: Command) -> None:
        if len(cmd.args) < 3:
            log.warning("Команда 8 без параметров")
            return
        line_type, dx, dy = cmd.args[0], cmd.args[1], cmd.args[2]

        x1, y1 = self.state.abs_pos()
        x2 = x1 + dx
        y2 = y1 + dy

        self._draw_line(x1, y1, x2, y2, line_type)

        self.state.x += dx
        self.state.y += dy
        self.state.set_line_type(line_type)

    # 9 — текст.
    def _op_9(self, cmd: Command) -> None:
        if cmd.text is None:
            log.warning("Команда 9 без текста")
            return

        size = cmd.args[0] if cmd.args else 1
        orient = cmd.args[1] if len(cmd.args) > 1 else 0

        x, y = self.state.abs_pos()
        self.backend.draw_text(x, y, cmd.text, size, orient, cmd.font)

    # ] — прямоугольник/рамка.
    def _op_bracket(self, cmd: Command) -> None:
        if len(cmd.args) < 4:
            log.warning("Команда ] без четырёх координат")
            return
        x1, y1, x2, y2 = cmd.args[:4]

        # координаты со смещением окна
        ax1, ay1 = x1 + self.state.shift_x, y1 + self.state.shift_y
        ax2, ay2 = x2 + self.state.shift_x, y2 + self.state.shift_y

        # отсечение и вызов backend
        clipped = self.state.clip_line(ax1, ay1, ax2, ay2)
        if clipped is None:
            return

        self.backend.draw_rect(*clipped, self.state.line_type)

    # > — окружность.
    def _op_gt(self, cmd: Command) -> None:
        if not cmd.args:
            log.warning("Команда > без параметров")
            return

        # формат: > T R    или    > R
        if len(cmd.args) == 1:
            line_type = self.state.line_type
            radius = cmd.args[0]
        else:
            line_type = cmd.args[0]
            radius = cmd.args[1]

        cx, cy = self.state.abs_pos()
        self.backend.draw_circle(cx, cy, radius, line_type)
        self.state.set_line_type(line_type)

    # } — смещение окна.
    def _op_shift(self, cmd: Command) -> None:
        if len(cmd.args) < 2:
            log.warning("Команда } без смещений")
            return
        self.state.shift_x += cmd.args[0]
        self.state.shift_y += cmd.args[1]

    # + — режим OR.
    def _op_plus(self, cmd: Command) -> None:
        self.state.set_mode(MODE_OR)
        self._set_backend_mode(MODE_OR)

    # ≠ — режим XOR.
    def _op_neq(self, cmd: Command) -> None:
        self.state.set_mode(MODE_XOR)
        self._set_backend_mode(MODE_XOR)

    # S — очистка экрана.
    def _op_S(self, cmd: Command) -> None:
        self.backend.clear()

    # K — сброс (очистка + состояние).
    def _op_K(self, cmd: Command) -> None:
        self.backend.clear()
        self.state.reset()

    # F‾ — конец / заполнение. Пока ничего не делает.
    def _op_F(self, cmd: Command) -> None:
        pass

    # ─── Вспомогательные ─────────────────────────────────────────────────────

    def _draw_line(
        self,
        x1: int, y1: int,
        x2: int, y2: int,
        line_type: int,
    ) -> None:
        """Отсечь линию и передать backend'у."""
        clipped = self.state.clip_line(x1, y1, x2, y2)
        if clipped is None:
            return
        self.backend.draw_line(*clipped, line_type)

    def _set_backend_mode(self, mode: str) -> None:
        """Уведомить backend о смене режима (если он это поддерживает)."""
        setter = getattr(self.backend, "set_mode", None)
        if callable(setter):
            setter(mode)

    # ─── Представление ───────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return f"TerminalEmulator(state={self.state!r}, backend={type(self.backend).__name__})"

    # ─── Таблица обработчиков ────────────────────────────────────────────────

    _HANDLERS = {
        "J0": _op_J0,
        "J1": _op_J1,
        "0":  _op_0,
        "1":  _op_1,
        "2":  _op_2,
        "4":  _op_4,
        "5":  _op_5,
        "7":  _op_7,
        "8":  _op_8,
        "9":  _op_9,
        "]":  _op_bracket,
        ">":  _op_gt,
        "}":  _op_shift,
        "+":  _op_plus,
        "≠":  _op_neq,
        "S":  _op_S,
        "K":  _op_K,
        "F":  _op_F,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Высокоуровневые утилиты
# ─────────────────────────────────────────────────────────────────────────────

def run_stream(
    data: bytes,
    backend: Backend,
    *,
    strict: bool = False,
    max_steps: int = 0,
) -> tuple[int, int]:
    """
    Прогнать бинарный поток команд через эмулятор.

    Возвращает (число успешно обработанных команд, число ошибок).
    """
    from reader import CommandStreamReader
    from parser import CommandParser

    reader = CommandStreamReader(data)
    parser = CommandParser(reader)
    emu = TerminalEmulator(backend.state, backend)

    steps = 0
    errors = 0

    while True:
        if max_steps and steps >= max_steps:
            break

        try:
            cmd = parser.parse()
        except Exception as exc:  # noqa: BLE001
            errors += 1
            log.error("Ошибка разбора команды #%d: %s", steps, exc)
            if strict:
                raise
            continue

        if cmd is None:
            break

        try:
            emu.execute(cmd)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            log.error("Ошибка выполнения #%d %r: %s", steps, cmd, exc)
            if strict:
                raise

        steps += 1

    return steps, errors


# ─────────────────────────────────────────────────────────────────────────────
# Заглушка backend'а (для отладки без зависимостей)
# ─────────────────────────────────────────────────────────────────────────────

class NullBackend:
    """
    Backend, который ничего не рисует, а только логирует вызовы.
    Полезен для проверки парсера и эмулятора без зависимостей.
    """

    def __init__(self, state: TerminalState):
        self.state = state
        self.calls: list[tuple] = []

    def draw_line(self, x1, y1, x2, y2, line_type):
        self.calls.append(("line", x1, y1, x2, y2, line_type))

    def draw_circle(self, cx, cy, r, line_type):
        self.calls.append(("circle", cx, cy, r, line_type))

    def draw_rect(self, x1, y1, x2, y2, line_type):
        self.calls.append(("rect", x1, y1, x2, y2, line_type))

    def draw_text(self, x, y, text, size, orient, font):
        self.calls.append(("text", x, y, text, size, orient, font))

    def clear(self):
        self.calls.append(("clear",))

    def set_mode(self, mode):
        self.calls.append(("mode", mode))

    def present(self):
        self.calls.append(("present",))


# ─────────────────────────────────────────────────────────────────────────────
# Самотест
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")

    from reader import VK
    from parser import CommandParser
    from reader import CommandStreamReader

    # Собираем поток: рамка + линии + окружность + текст + XOR + сброс.
    def cmd(s: bytes) -> bytes:
        return s + bytes([VK])

    stream = (
        cmd(b"J1") +
        cmd(b"S") +
        cmd(b"] 8 1 552 398") +
        cmd(b"4 280 200") +
        cmd(b"> 1 100") +
        cmd(b"7 2 50 50") +
        cmd(b"7 2 500 350") +
        cmd(b"≠") +
        cmd(b"7 2 500 50") +
        cmd(b"+") +
        cmd(b"9 1") +
        cmd(b";0 HELLO") +
        cmd(b"2")
    )

    state = TerminalState()
    backend = NullBackend(state)
    emu = TerminalEmulator(state, backend)

    parser = CommandParser(CommandStreamReader(stream))
    for c in parser:
        emu.execute(c)

    print(f"\nСостояние после прогона:\n{state.describe()}\n")
    print("Вызовы backend'а:")
    for call in backend.calls:
        print(f"  {call}")
    print("\nСтатистика по командам:")
    for op, n in sorted(emu.stats.items()):
        print(f"  {op!r}: {n}")