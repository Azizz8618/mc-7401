"""
tests/test_commands.py — интеграционные тесты на эталонных потоках.

Прогоняют команды через все три backend'а и проверяют, что:
    - SVG содержит ожидаемые примитивы;
    - NullBackend получает корректное число вызовов;
    - статистика эмулятора согласуется с ожиданиями;
    - потоки не вызывают исключений.
"""

from __future__ import annotations

import pytest

from reader import VK, CommandStreamReader
from parser import CommandParser, parse_stream
from state import TerminalState
from emulator import TerminalEmulator, NullBackend, run_stream


def _cmd(s: bytes) -> bytes:
    return s + bytes([VK])


# ─────────────────────────────────────────────────────────────────────────────
# Sun
# ─────────────────────────────────────────────────────────────────────────────

class TestSunStream:

    def test_parses(self, sun_stream):
        cmds = parse_stream(sun_stream)
        ops = [c.op for c in cmds]
        assert "J1" in ops
        assert "S" in ops
        assert "]" in ops
        assert ops.count(">") == 20
        assert ops[-1] == "2"

    def test_emulator_null(self, sun_stream):
        st = TerminalState()
        backend = NullBackend(st)
        steps, errors = run_stream(sun_stream, backend, strict=True)
        assert steps > 0
        assert errors == 0
        # 20 окружностей + рамка + 2 (J1, S) + 4 + 2
        circles = [c for c in backend.calls if c[0] == "circle"]
        assert len(circles) == 20

    def test_svg_backend(self, sun_stream, tmp_svg):
        from backends import SVGBackend
        st = TerminalState()
        backend = SVGBackend(st)
        run_stream(sun_stream, backend, strict=True)
        backend.save(tmp_svg)

        svg = tmp_svg.read_text(encoding="utf-8")
        assert "<svg" in svg
        assert svg.count("<circle") == 20
        assert "<rect" in svg


# ─────────────────────────────────────────────────────────────────────────────
# Grid
# ─────────────────────────────────────────────────────────────────────────────

class TestGridStream:

    def test_parses(self, grid_stream):
        cmds = parse_stream(grid_stream)
        assert cmds[0].op == "J1"
        assert cmds[-1].op == "2"

    def test_line_count(self, grid_stream):
        st = TerminalState()
        backend = NullBackend(st)
        run_stream(grid_stream, backend, strict=True)
        lines = [c for c in backend.calls if c[0] == "line"]
        # 9 вертикальных + 7 горизонтальных = 16
        assert len(lines) == 16

    def test_svg_backend(self, grid_stream, tmp_svg):
        from backends import SVGBackend
        st = TerminalState()
        backend = SVGBackend(st)
        run_stream(grid_stream, backend, strict=True)
        backend.save(tmp_svg)

        svg = tmp_svg.read_text(encoding="utf-8")
        assert svg.count("<line") == 16


# ─────────────────────────────────────────────────────────────────────────────
# Mixed (текст + XOR + сдвиг)
# ─────────────────────────────────────────────────────────────────────────────

class TestMixedStream:

    def test_parses(self, mixed_stream):
        cmds = parse_stream(mixed_stream)
        ops = [c.op for c in cmds]
        assert "9" in ops
        assert "}" in ops
        assert ">" in ops

    def test_no_errors(self, mixed_stream):
        st = TerminalState()
        backend = NullBackend(st)
        steps, errors = run_stream(mixed_stream, backend, strict=True)
        assert errors == 0

    def test_text_present(self, mixed_stream):
        st = TerminalState()
        backend = NullBackend(st)
        run_stream(mixed_stream, backend)
        texts = [c for c in backend.calls if c[0] == "text"]
        assert any("HELLO" in t[3] for t in texts)

    def test_svg_text(self, mixed_stream, tmp_svg):
        from backends import SVGBackend
        st = TerminalState()
        backend = SVGBackend(st)
        run_stream(mixed_stream, backend, strict=True)
        backend.save(tmp_svg)

        svg = tmp_svg.read_text(encoding="utf-8")
        assert "<text" in svg


# ─────────────────────────────────────────────────────────────────────────────
# Двоичный поток
# ─────────────────────────────────────────────────────────────────────────────

class TestBinaryStream:

    def test_binary_parses(self, binary_stream):
        cmds = parse_stream(binary_stream)
        # J1, 4 0 0, 7 <binary>, 2
        assert len(cmds) == 4
        line_cmd = cmds[2]
        assert line_cmd.op == "7"
        assert line_cmd.args == [1, 100, 100]


# ─────────────────────────────────────────────────────────────────────────────
# Обрыв потока
# ─────────────────────────────────────────────────────────────────────────────

class TestTruncatedStream:

    def test_truncated_no_errors(self, truncated_stream):
        st = TerminalState()
        backend = NullBackend(st)
        steps, errors = run_stream(truncated_stream, backend)
        # последняя команда без ВК всё же разбирается
        assert steps == 2
        assert errors == 0

    def test_truncated_flag(self, truncated_stream):
        reader = CommandStreamReader(truncated_stream)
        parser = CommandParser(reader)
        parser.parse()
        parser.parse()
        assert reader.truncated is True


# ─────────────────────────────────────────────────────────────────────────────
# Backend'ы: SVG через фабрику
# ─────────────────────────────────────────────────────────────────────────────

class TestBackendFactory:

    def test_create_svg(self, sun_stream, tmp_svg):
        from backends import create_backend
        st = TerminalState()
        backend = create_backend("svg", st)
        run_stream(sun_stream, backend, strict=True)
        backend.save(tmp_svg)
        assert tmp_svg.exists()

    def test_unknown_backend(self):
        from backends import create_backend
        with pytest.raises(ValueError):
            create_backend("unknown", TerminalState())

    def test_available_backends_includes_svg(self):
        from backends import available_backends
        assert "svg" in available_backends()


# ─────────────────────────────────────────────────────────────────────────────
# Цепочка: parser + emulator + SVG, побайтово
# ─────────────────────────────────────────────────────────────────────────────

class TestEndToEnd:

    def test_full_pipeline(self, sun_stream, tmp_svg):
        reader = CommandStreamReader(sun_stream)
        parser = CommandParser(reader)

        from backends import SVGBackend
        st = TerminalState()
        backend = SVGBackend(st, border=True, metadata=True)
        emu = TerminalEmulator(st, backend)

        for cmd in parser:
            emu.execute(cmd)

        backend.save(tmp_svg)
        svg = tmp_svg.read_text(encoding="utf-8")

        assert "<?xml" in svg
        assert "<svg" in svg
        assert "</svg>" in svg
        assert "metadata" in svg
        assert svg.count("<circle") == 20