"""
tests/test_emulator.py — unit-тесты для модуля emulator.

Используют NullBackend для проверки вызовов backend'а без
реальных зависимостей. Тестируют:
    - диспетчеризацию команд;
    - преобразование координат со смещением окна;
    - отсечение;
    - режимы OR/XOR;
    - статистику по командам;
    - run_stream.
"""

from __future__ import annotations

import pytest

from reader import VK, CommandStreamReader
from parser import CommandParser, parse_stream
from state import TerminalState, MODE_OR, MODE_XOR
from emulator import TerminalEmulator, NullBackend, run_stream


# ─────────────────────────────────────────────────────────────────────────────
# Хелперы
# ─────────────────────────────────────────────────────────────────────────────

def _cmd(s: bytes) -> bytes:
    return s + bytes([VK])


def _emu():
    st = TerminalState()
    backend = NullBackend(st)
    return TerminalEmulator(st, backend), st, backend


def _run(data: bytes):
    emu, st, backend = _emu()
    for cmd in parse_stream(data):
        emu.execute(cmd)
    return emu, st, backend


# ─────────────────────────────────────────────────────────────────────────────
# Диспетчеризация команд
# ─────────────────────────────────────────────────────────────────────────────

class TestDispatch:

    def test_unknown_command_ignored(self):
        emu, st, backend = _emu()
        # пустая команда
        from parser import Command
        emu.execute(Command(op="", args=[], raw=b"", index=1))
        # неизвестная
        emu.execute(Command(op="Z", args=[], raw=b"Z", index=2))
        assert backend.calls == []

    def test_j1_j0(self):
        emu, st, backend = _emu()
        emu.execute(Command(op="J1", args=[], raw=b"", index=1))
        assert st.graphics is True
        emu.execute(Command(op="J0", args=[], raw=b"", index=2))
        assert st.graphics is False


# ─────────────────────────────────────────────────────────────────────────────
# Перемещение
# ─────────────────────────────────────────────────────────────────────────────

class TestMove:

    def test_move_abs(self):
        emu, st, backend = _emu()
        emu.execute(Command(op="4", args=[100, 200], raw=b"", index=1))
        assert (st.x, st.y) == (100, 200)
        assert backend.calls == []

    def test_move_rel(self):
        emu, st, backend = _emu()
        emu.execute(Command(op="4", args=[10, 10], raw=b"", index=1))
        emu.execute(Command(op="5", args=[5, 5], raw=b"", index=2))
        assert (st.x, st.y) == (15, 15)


# ─────────────────────────────────────────────────────────────────────────────
# Линии
# ─────────────────────────────────────────────────────────────────────────────

class TestLines:

    def test_line_abs(self):
        emu, st, backend = _emu()
        emu.execute(Command(op="4", args=[0, 0], raw=b"", index=1))
        emu.execute(Command(op="7", args=[1, 100, 100], raw=b"", index=2))
        assert len(backend.calls) == 1
        kind, x1, y1, x2, y2, lt = backend.calls[0]
        assert kind == "line"
        assert (x1, y1, x2, y2) == (0, 0, 100, 100)
        assert lt == 1
        assert (st.x, st.y) == (100, 100)

    def test_line_rel(self):
        emu, st, backend = _emu()
        emu.execute(Command(op="4", args=[50, 50], raw=b"", index=1))
        emu.execute(Command(op="8", args=[2, 10, 20], raw=b"", index=2))
        kind, x1, y1, x2, y2, lt = backend.calls[0]
        assert (x1, y1, x2, y2) == (50, 50, 60, 70)
        assert (st.x, st.y) == (60, 70)
        assert lt == 2

    def test_line_clipped(self):
        emu, st, backend = _emu()
        emu.execute(Command(op="4", args=[100, 100], raw=b"", index=1))
        # линия, выходящая за экран
        emu.execute(Command(op="7", args=[1, 5000, 100], raw=b"", index=2))
        kind, x1, y1, x2, y2, lt = backend.calls[0]
        assert x2 == st.width

    def test_line_fully_outside(self):
        emu, st, backend = _emu()
        emu.execute(Command(op="4", args=[-100, -100], raw=b"", index=1))
        emu.execute(Command(op="7", args=[1, -50, -50], raw=b"", index=2))
        assert backend.calls == []


# ─────────────────────────────────────────────────────────────────────────────
# Окружности
# ─────────────────────────────────────────────────────────────────────────────

class TestCircles:

    def test_circle_with_type(self):
        emu, st, backend = _emu()
        emu.execute(Command(op="4", args=[100, 100], raw=b"", index=1))
        emu.execute(Command(op=">", args=[2, 50], raw=b"", index=2))
        kind, cx, cy, r, lt = backend.calls[0]
        assert kind == "circle"
        assert (cx, cy, r, lt) == (100, 100, 50, 2)

    def test_circle_without_type(self):
        emu, st, backend = _emu()
        st.line_type = 3
        emu.execute(Command(op="4", args=[100, 100], raw=b"", index=1))
        emu.execute(Command(op=">", args=[50], raw=b"", index=2))
        kind, cx, cy, r, lt = backend.calls[0]
        assert lt == 3


# ─────────────────────────────────────────────────────────────────────────────
# Прямоугольники
# ─────────────────────────────────────────────────────────────────────────────

class TestRects:

    def test_rect(self):
        emu, st, backend = _emu()
        emu.execute(Command(op="]", args=[10, 20, 100, 200], raw=b"", index=1))
        kind, x1, y1, x2, y2, lt = backend.calls[0]
        assert kind == "rect"
        assert (x1, y1, x2, y2) == (10, 20, 100, 200)


# ─────────────────────────────────────────────────────────────────────────────
# Смещение окна
# ─────────────────────────────────────────────────────────────────────────────

class TestShift:

    def test_shift_affects_abs_pos(self):
        emu, st, backend = _emu()
        emu.execute(Command(op="}", args=[100, 50], raw=b"", index=1))
        emu.execute(Command(op="4", args=[0, 0], raw=b"", index=2))
        emu.execute(Command(op=">", args=[1, 10], raw=b"", index=3))
        kind, cx, cy, r, lt = backend.calls[0]
        assert (cx, cy) == (100, 50)

    def test_shift_accumulates(self):
        emu, st, backend = _emu()
        emu.execute(Command(op="}", args=[100, 0], raw=b"", index=1))
        emu.execute(Command(op="}", args=[100, 50], raw=b"", index=2))
        assert (st.shift_x, st.shift_y) == (200, 50)


# ─────────────────────────────────────────────────────────────────────────────
# Режимы
# ─────────────────────────────────────────────────────────────────────────────

class TestModes:

    def test_xor_mode(self):
        emu, st, backend = _emu()
        emu.execute(Command(op="≠", args=[], raw=b"", index=1))
        assert st.mode == MODE_XOR
        assert ("mode", MODE_XOR) in backend.calls

    def test_or_mode(self):
        emu, st, backend = _emu()
        emu.execute(Command(op="≠", args=[], raw=b"", index=1))
        emu.execute(Command(op="+", args=[], raw=b"", index=2))
        assert st.mode == MODE_OR


# ─────────────────────────────────────────────────────────────────────────────
# Очистка и сброс
# ─────────────────────────────────────────────────────────────────────────────

class TestClearReset:

    def test_clear(self):
        emu, st, backend = _emu()
        emu.execute(Command(op="S", args=[], raw=b"", index=1))
        assert ("clear",) in backend.calls

    def test_reset(self):
        emu, st, backend = _emu()
        emu.execute(Command(op="4", args=[100, 200], raw=b"", index=1))
        emu.execute(Command(op="K", args=[], raw=b"", index=2))
        assert (st.x, st.y) == (0, 0)
        assert ("clear",) in backend.calls


# ─────────────────────────────────────────────────────────────────────────────
# Текст
# ─────────────────────────────────────────────────────────────────────────────

class TestText:

    def test_text(self):
        emu, st, backend = _emu()
        from parser import Command
        emu.execute(Command(op="9", args=[2, 0], text="HELLO",
                            font="EN", raw=b"", index=1))
        kind, x, y, text, size, orient, font = backend.calls[0]
        assert kind == "text"
        assert text == "HELLO"
        assert size == 2
        assert orient == 0
        assert font == "EN"

    def test_text_with_shift(self):
        emu, st, backend = _emu()
        emu.execute(Command(op="}", args=[100, 50], raw=b"", index=1))
        from parser import Command
        emu.execute(Command(op="9", args=[1, 0], text="A",
                            font="EN", raw=b"", index=2))
        kind, x, y, *rest = backend.calls[0]
        assert (x, y) == (100, 50)


# ─────────────────────────────────────────────────────────────────────────────
# Статистика
# ─────────────────────────────────────────────────────────────────────────────

class TestStats:

    def test_stats_counted(self):
        emu, st, backend = _emu()
        for op in ["J1", "S", "4", "4", "7", "2"]:
            args = {
                "4": [0, 0],
                "7": [1, 0, 0],
            }.get(op, [])
            from parser import Command
            emu.execute(Command(op=op, args=args, raw=b"", index=1))

        stats = emu.stats
        assert stats["J1"] == 1
        assert stats["S"] == 1
        assert stats["4"] == 2
        assert stats["7"] == 1
        assert stats["2"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# run_stream
# ─────────────────────────────────────────────────────────────────────────────

class TestRunStream:

    def test_simple(self):
        data = _cmd(b"J1") + _cmd(b"4 10 20") + _cmd(b"2")
        st = TerminalState()
        backend = NullBackend(st)
        steps, errors = run_stream(data, backend)
        assert steps == 3
        assert errors == 0

    def test_max_steps(self):
        data = _cmd(b"S") * 10
        st = TerminalState()
        backend = NullBackend(st)
        steps, _ = run_stream(data, backend, max_steps=3)
        assert steps == 3

    def test_no_data(self):
        st = TerminalState()
        backend = NullBackend(st)
        steps, errors = run_stream(b"", backend)
        assert steps == 0
        assert errors == 0