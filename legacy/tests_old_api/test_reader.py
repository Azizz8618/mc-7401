"""
tests/test_reader.py — unit-тесты для модуля reader.

Проверяют:
    - базовое разбиение на команды;
    - пустые команды (двойной ВК);
    - спецкоды S0/S1/END внутри команд;
    - обрыв потока без завершающего ВК;
    - методы peek/skip/seek/reset;
    - функции split_by_font и decode_text.
"""

from __future__ import annotations

import pytest

from reader import (
    CommandStreamReader,
    VK, S0, S1, END, LF, SP,
    split_by_font,
    decode_text,
    has_special_codes,
    dump_commands,
)


# ─────────────────────────────────────────────────────────────────────────────
# Базовое чтение
# ─────────────────────────────────────────────────────────────────────────────

class TestBasicReading:

    def test_empty_stream(self):
        r = CommandStreamReader(b"")
        assert r.read_raw_command() is None
        assert r.eof

    def test_single_command(self):
        r = CommandStreamReader(b"J1" + bytes([VK]))
        assert r.read_raw_command() == b"J1"
        assert r.read_raw_command() is None

    def test_multiple_commands(self):
        data = b"J1" + bytes([VK]) + b"S" + bytes([VK]) + b"4 1 2" + bytes([VK])
        r = CommandStreamReader(data)
        assert r.read_raw_command() == b"J1"
        assert r.read_raw_command() == b"S"
        assert r.read_raw_command() == b"4 1 2"
        assert r.read_raw_command() is None

    def test_empty_command(self):
        """Двойной ВК — пустая команда."""
        data = b"J1" + bytes([VK]) + bytes([VK]) + b"S" + bytes([VK])
        r = CommandStreamReader(data)
        assert r.read_raw_command() == b"J1"
        assert r.read_raw_command() == b""
        assert r.read_raw_command() == b"S"

    def test_iteration(self):
        data = b"A" + bytes([VK]) + b"B" + bytes([VK])
        r = CommandStreamReader(data)
        assert list(r) == [b"A", b"B"]

    def test_len(self):
        data = b"A" + bytes([VK]) + b"B" + bytes([VK])
        r = CommandStreamReader(data)
        assert len(r) == 2


# ─────────────────────────────────────────────────────────────────────────────
# Спецкоды
# ─────────────────────────────────────────────────────────────────────────────

class TestSpecialCodes:

    def test_s0_within_command(self):
        data = b"9 1" + bytes([VK]) + b";0 " + bytes([S0]) + b"XY" + bytes([VK])
        r = CommandStreamReader(data)
        assert r.read_raw_command() == b"9 1"
        assert r.read_raw_command() == b";0 " + bytes([S0]) + b"XY"

    def test_end_marker(self):
        data = b";0 hello" + bytes([END]) + b"tail" + bytes([VK])
        r = CommandStreamReader(data)
        cmd = r.read_raw_command()
        assert cmd == b";0 hello" + bytes([END]) + b"tail"

    def test_vk_not_included(self):
        """ВК не должен попадать в результат."""
        r = CommandStreamReader(b"ABC" + bytes([VK]))
        assert r.read_raw_command() == b"ABC"


# ─────────────────────────────────────────────────────────────────────────────
# Обрыв потока
# ─────────────────────────────────────────────────────────────────────────────

class TestTruncation:

    def test_truncated_last_command(self):
        data = b"J1" + bytes([VK]) + b"4 100 200"  # нет ВК
        r = CommandStreamReader(data)
        assert r.read_raw_command() == b"J1"
        assert r.read_raw_command() == b"4 100 200"
        assert r.truncated is True
        assert r.read_raw_command() is None

    def test_truncated_after_empty(self):
        data = b""
        r = CommandStreamReader(data)
        assert r.read_raw_command() is None
        assert r.truncated is False


# ─────────────────────────────────────────────────────────────────────────────
# Позиционирование
# ─────────────────────────────────────────────────────────────────────────────

class TestPositioning:

    def test_position_progress(self):
        data = b"AB" + bytes([VK]) + b"CD" + bytes([VK])
        r = CommandStreamReader(data)
        assert r.position == 0
        r.read_raw_command()
        assert r.position == 3  # AB + VK
        r.read_raw_command()
        assert r.position == 6

    def test_remaining(self):
        data = b"A" + bytes([VK]) + b"BC" + bytes([VK])
        r = CommandStreamReader(data)
        assert r.remaining == len(data)
        r.read_raw_command()
        assert r.remaining == len(data) - 2

    def test_reset(self):
        data = b"A" + bytes([VK]) + b"B" + bytes([VK])
        r = CommandStreamReader(data)
        r.read_raw_command()
        r.reset()
        assert r.position == 0
        assert r.command_index == 0
        assert r.read_raw_command() == b"A"

    def test_seek(self):
        data = b"ABCD" + bytes([VK])
        r = CommandStreamReader(data)
        r.seek(2)
        assert r.read_raw_command() == b"CD"

    def test_peek_and_skip(self):
        data = b"XY" + bytes([VK])
        r = CommandStreamReader(data)
        assert r.peek_byte() == ord("X")
        assert r.peek_byte() == ord("X")
        r.skip_byte()
        assert r.peek_byte() == ord("Y")

    def test_command_index(self):
        data = b"A" + bytes([VK]) + b"B" + bytes([VK])
        r = CommandStreamReader(data)
        assert r.command_index == 0
        r.read_raw_command()
        assert r.command_index == 1
        r.read_raw_command()
        assert r.command_index == 2


# ─────────────────────────────────────────────────────────────────────────────
# split_by_font
# ─────────────────────────────────────────────────────────────────────────────

class TestSplitByFont:

    def test_pure_latin(self):
        assert split_by_font(b"abc") == [("EN", b"abc")]

    def test_switch_to_ru(self):
        raw = b"abc" + bytes([S0]) + b"xyz"
        assert split_by_font(raw) == [("EN", b"abc"), ("RU", b"xyz")]

    def test_switch_back_to_en(self):
        raw = b"a" + bytes([S0]) + b"b" + bytes([S1]) + b"c"
        assert split_by_font(raw) == [
            ("EN", b"a"),
            ("RU", b"b"),
            ("EN", b"c"),
        ]

    def test_end_marker_stops(self):
        raw = b"abc" + bytes([END]) + b"xyz"
        assert split_by_font(raw) == [("EN", b"abc")]

    def test_initial_font_ru(self):
        assert split_by_font(b"abc", initial_font="RU") == [("RU", b"abc")]

    def test_empty(self):
        assert split_by_font(b"") == []


# ─────────────────────────────────────────────────────────────────────────────
# decode_text
# ─────────────────────────────────────────────────────────────────────────────

class TestDecodeText:

    def test_ascii(self):
        assert decode_text(b"HELLO") == "HELLO"

    def test_russian_koi8(self):
        # 'а' 'б' 'в' в КОИ-8Р
        assert decode_text(bytes([S0]) + b"\xc1\xc2\xc3") == "абв"

    def test_mixed(self):
        raw = b"AB" + bytes([S0]) + b"\xc1\xc2" + bytes([S1]) + b"CD"
        assert decode_text(raw) == "ABабCD"

    def test_end_stops(self):
        raw = b"AB" + bytes([END]) + b"CD"
        assert decode_text(raw) == "AB"


# ─────────────────────────────────────────────────────────────────────────────
# has_special_codes
# ─────────────────────────────────────────────────────────────────────────────

class TestHasSpecialCodes:

    def test_plain_text(self):
        assert has_special_codes(b"HELLO") is False

    def test_with_s0(self):
        assert has_special_codes(b"AB" + bytes([S0])) is True

    def test_with_end(self):
        assert has_special_codes(b"AB" + bytes([END])) is True


# ─────────────────────────────────────────────────────────────────────────────
# dump_commands (smoke)
# ─────────────────────────────────────────────────────────────────────────────

class TestDump:

    def test_dump_smoke(self, capsys):
        data = b"J1" + bytes([VK]) + b"S" + bytes([VK])
        dump_commands(data)
        out = capsys.readouterr().out
        assert "J1" in out
        assert "S" in out

    def test_dump_limit(self, capsys):
        data = b"".join(b"A" + bytes([VK]) for _ in range(10))
        dump_commands(data, limit=3)
        out = capsys.readouterr().out
        assert "показано 3" in out