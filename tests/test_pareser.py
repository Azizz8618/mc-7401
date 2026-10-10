"""
tests/test_parser.py — unit-тесты для модуля parser.

Проверяют:
    - разбор команд без аргументов;
    - разбор числовых команд (десятичное кодирование);
    - двоичное кодирование параметров;
    - команду 9 (текст в две строки);
    - обработку ошибок (мало параметров, обрыв).
"""

from __future__ import annotations

import pytest

from reader import VK, S0, S1, END, CommandStreamReader
from parser import Command, CommandParser, ParseError, parse_stream, parse_text_stream


# ─────────────────────────────────────────────────────────────────────────────
# Хелперы
# ─────────────────────────────────────────────────────────────────────────────

def _cmd(s: bytes) -> bytes:
    return s + bytes([VK])


def _parser(data: bytes) -> CommandParser:
    return CommandParser(CommandStreamReader(data))


def _one(data: bytes) -> Command:
    parser = _parser(data)
    c = parser.parse()
    assert c is not None
    return c


# ─────────────────────────────────────────────────────────────────────────────
# Команды без аргументов
# ─────────────────────────────────────────────────────────────────────────────

class TestNullaryCommands:

    @pytest.mark.parametrize("op", [b"2", b"+", b"S", b"K", b"F"])
    def test_nullary(self, op):
        c = _one(_cmd(op))
        assert c.op == op.decode("latin-1")
        assert c.args == []

    def test_j0(self):
        c = _one(_cmd(b"J0"))
        assert c.op == "J0"

    def test_j1(self):
        c = _one(_cmd(b"J1"))
        assert c.op == "J1"


# ─────────────────────────────────────────────────────────────────────────────
# Числовые команды (десятичное кодирование)
# ─────────────────────────────────────────────────────────────────────────────

class TestNumericCommands:

    def test_move_abs(self):
        c = _one(_cmd(b"4 100 200"))
        assert c.op == "4"
        assert c.args == [100, 200]

    def test_move_rel(self):
        c = _one(_cmd(b"5 -10 20"))
        assert c.op == "5"
        assert c.args == [-10, 20]

    def test_line_abs(self):
        c = _one(_cmd(b"7 2 400 300"))
        assert c.op == "7"
        assert c.args == [2, 400, 300]

    def test_line_rel(self):
        c = _one(_cmd(b"8 1 5 -5"))
        assert c.op == "8"
        assert c.args == [1, 5, -5]

    def test_rect(self):
        c = _one(_cmd(b"] 8 1 552 398"))
        assert c.op == "]"
        assert c.args == [8, 1, 552, 398]

    def test_circle_with_type(self):
        c = _one(_cmd(b"> 1 150"))
        assert c.op == ">"
        assert c.args == [1, 150]

    def test_circle_without_type(self):
        c = _one(_cmd(b"> 150"))
        assert c.op == ">"
        assert c.args == [150]

    def test_shift(self):
        c = _one(_cmd(b"} 700 0"))
        assert c.op == "}"
        assert c.args == [700, 0]

    def test_extra_spaces(self):
        c = _one(_cmd(b"4   100    200"))
        assert c.args == [100, 200]

    def test_too_few_args_raises(self):
        with pytest.raises(ParseError):
            _one(_cmd(b"4 100"))

    def test_zero_args_where_required(self):
        with pytest.raises(ParseError):
            _one(_cmd(b"7"))


# ─────────────────────────────────────────────────────────────────────────────
# Двоичное кодирование
# ─────────────────────────────────────────────────────────────────────────────

class TestBinaryCommands:

    def test_line_binary(self):
        # b"7" + T=1 + X=100 + Y=200
        data = _cmd(bytes([ord("7"), 1, 100, 200]))
        c = _one(data)
        assert c.op == "7"
        assert c.args == [1, 100, 200]

    def test_move_binary(self):
        data = _cmd(bytes([ord("4"), 10, 20]))
        c = _one(data)
        assert c.op == "4"
        assert c.args == [10, 20]

    def test_binary_truncated_to_7bit(self):
        # Байт 0x85 должен обрезаться до 0x05.
        data = _cmd(bytes([ord("4"), 0x85, 0x85]))
        c = _one(data)
        assert c.args == [0x05, 0x05]


# ─────────────────────────────────────────────────────────────────────────────
# Команда 9 — текст
# ─────────────────────────────────────────────────────────────────────────────

class TestTextCommand:

    def test_simple_text(self):
        data = _cmd(b"9 1") + _cmd(b";0 HELLO")
        c = _one(data)
        assert c.op == "9"
        assert c.args == [1, 0]
        assert c.text == "HELLO"
        assert c.font == "EN"

    def test_text_with_cyrillic(self):
        data = (
            _cmd(b"9 1")
            + _cmd(b";0 " + bytes([S0]) + b"\xc1\xc2\xc3")
        )
        c = _one(data)
        assert c.text == "абв"
        assert c.font == "RU"

    def test_text_size_2(self):
        data = _cmd(b"9 2") + _cmd(b";1 X")
        c = _one(data)
        assert c.args == [2, 1]
        assert c.text == "X"

    def test_text_truncated_raises(self):
        data = _cmd(b"9 1")  # нет второй строки
        with pytest.raises(ParseError):
            _one(data)

    def test_text_missing_semicolon_raises(self):
        data = _cmd(b"9 1") + _cmd(b"0 HELLO")  # нет ';'
        with pytest.raises(ParseError):
            _one(data)


# ─────────────────────────────────────────────────────────────────────────────
# Поток команд
# ─────────────────────────────────────────────────────────────────────────────

class TestStreamParsing:

    def test_parse_stream(self):
        data = (
            _cmd(b"J1")
            + _cmd(b"S")
            + _cmd(b"4 10 20")
            + _cmd(b"2")
        )
        cmds = parse_stream(data)
        assert len(cmds) == 4
        assert [c.op for c in cmds] == ["J1", "S", "4", "2"]

    def test_parse_stream_limit(self):
        data = b"".join(_cmd(b"S") for _ in range(5))
        cmds = parse_stream(data, limit=2)
        assert len(cmds) == 2

    def test_iteration(self):
        data = _cmd(b"J1") + _cmd(b"S")
        parser = _parser(data)
        ops = [c.op for c in parser]
        assert ops == ["J1", "S"]

    def test_command_index_increases(self):
        data = _cmd(b"S") + _cmd(b"S") + _cmd(b"S")
        parser = _parser(data)
        c1 = parser.parse()
        c2 = parser.parse()
        assert c1.index == 1
        assert c2.index == 2

    def test_repr(self):
        c = _one(_cmd(b"4 10 20"))
        s = repr(c)
        assert "Command" in s
        assert "'4'" in s


# ─────────────────────────────────────────────────────────────────────────────
# Текстовая форма
# ─────────────────────────────────────────────────────────────────────────────

class TestTextStream:

    def test_parse_text_stream(self):
        text = "J1\nS\n4 100 200\n2\n"
        cmds = parse_text_stream(text)
        assert [c.op for c in cmds] == ["J1", "S", "4", "2"]

    def test_parse_text_with_windows_eol(self):
        text = "J1\r\nS\r\n"
        cmds = parse_text_stream(text)
        assert [c.op for c in cmds] == ["J1", "S"]