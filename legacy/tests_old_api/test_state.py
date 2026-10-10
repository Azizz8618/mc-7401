"""
tests/test_state.py — unit-тесты для модуля state.

Проверяют:
    - координаты и смещение окна;
    - отсечение отрезков (Коэн — Сазерленд);
    - тип линии и dash-массивы;
    - сохранение и восстановление состояния;
    - сброс;
    - применение команд через apply_command.
"""

from __future__ import annotations

import pytest

from state import (
    TerminalState,
    SCREEN_WIDTH,
    SCREEN_HEIGHT,
    MODE_OR, MODE_XOR, MODE_ERASE,
    FONT_EN, FONT_RU,
    LINE_DASHES,
)


# ─────────────────────────────────────────────────────────────────────────────
# Инициализация
# ─────────────────────────────────────────────────────────────────────────────

class TestInit:

    def test_defaults(self):
        st = TerminalState()
        assert st.width == SCREEN_WIDTH
        assert st.height == SCREEN_HEIGHT
        assert st.x == 0
        assert st.y == 0
        assert st.shift_x == 0
        assert st.shift_y == 0
        assert st.line_type == 0
        assert st.mode == MODE_OR
        assert st.font == FONT_EN
        assert st.graphics is False

    def test_clone(self):
        st = TerminalState()
        st.x = 100
        st.shift_x = 50
        st.line_type = 3
        c = st.clone()
        assert c.x == 100
        assert c.shift_x == 50
        assert c.line_type == 3
        # изменение клона не влияет на оригинал
        c.x = 999
        assert st.x == 100


# ─────────────────────────────────────────────────────────────────────────────
# Координаты
# ─────────────────────────────────────────────────────────────────────────────

class TestCoordinates:

    def test_abs_pos(self):
        st = TerminalState()
        st.x, st.y = 100, 200
        st.shift_x, st.shift_y = 10, 20
        assert st.abs_pos() == (110, 220)

    def test_to_screen(self):
        st = TerminalState()
        st.shift_x, st.shift_y = 5, 5
        assert st.to_screen(10, 10) == (15, 15)

    def test_in_bounds(self):
        st = TerminalState()
        assert st.in_bounds(0, 0) is True
        assert st.in_bounds(100, 100) is True
        assert st.in_bounds(-1, 0) is False
        assert st.in_bounds(0, -1) is False

    def test_in_bounds_abs(self):
        st = TerminalState()
        st.shift_x = 500
        assert st.in_bounds_abs(0, 0) is True
        assert st.in_bounds_abs(100, 0) is False


# ─────────────────────────────────────────────────────────────────────────────
# Отсечение
# ─────────────────────────────────────────────────────────────────────────────

class TestClipping:

    def test_line_inside(self):
        st = TerminalState()
        assert st.clip_line(10, 10, 100, 100) == (10, 10, 100, 100)

    def test_line_crossing_right(self):
        st = TerminalState()
        result = st.clip_line(100, 100, 700, 100)
        assert result is not None
        x1, y1, x2, y2 = result
        assert x2 == st.width
        assert y1 == y2 == 100

    def test_line_completely_outside(self):
        st = TerminalState()
        assert st.clip_line(-100, -100, -50, -50) is None

    def test_line_through_all_edges(self):
        st = TerminalState()
        result = st.clip_line(-100, -100, 1000, 1000)
        assert result is not None
        x1, y1, x2, y2 = result
        # оба конца внутри экрана
        assert 0 <= x1 <= st.width
        assert 0 <= y1 <= st.height
        assert 0 <= x2 <= st.width
        assert 0 <= y2 <= st.height

    def test_clipping_disabled(self):
        st = TerminalState()
        st.clipping = False
        assert st.clip_line(-100, -100, 1000, 1000) == (-100, -100, 1000, 1000)


# ─────────────────────────────────────────────────────────────────────────────
# Тип линии
# ─────────────────────────────────────────────────────────────────────────────

class TestLineTypes:

    def test_dash_array_solid(self):
        st = TerminalState()
        st.line_type = 0
        assert st.dash_array() == []

    def test_dash_array_dashed(self):
        st = TerminalState()
        st.line_type = 2
        assert st.dash_array() == LINE_DASHES[2]

    def test_line_type_name(self):
        st = TerminalState()
        st.line_type = 3
        assert st.line_type_name() == "dotted"

    def test_is_drawing(self):
        st = TerminalState()
        st.line_type = 5
        assert st.is_drawing() is True
        st.line_type = 6
        assert st.is_drawing() is False

    def test_set_line_type_invalid(self):
        st = TerminalState()
        st.set_line_type(100)
        # остаётся прежний
        assert st.line_type == 0


# ─────────────────────────────────────────────────────────────────────────────
# Перо
# ─────────────────────────────────────────────────────────────────────────────

class TestModes:

    def test_is_xor(self):
        st = TerminalState()
        st.set_mode(MODE_XOR)
        assert st.is_xor() is True

    def test_is_erase(self):
        st = TerminalState()
        st.set_mode(MODE_ERASE)
        assert st.is_erase() is True

    def test_invalid_mode(self):
        st = TerminalState()
        st.set_mode("invalid")
        assert st.mode == MODE_OR


# ─────────────────────────────────────────────────────────────────────────────
# Шрифт
# ─────────────────────────────────────────────────────────────────────────────

class TestFont:

    def test_set_font(self):
        st = TerminalState()
        st.set_font(FONT_RU)
        assert st.font == FONT_RU
        assert st.is_cyrillic() is True

    def test_set_font_invalid(self):
        st = TerminalState()
        with pytest.raises(ValueError):
            st.set_font("XX")

    def test_toggle_font(self):
        st = TerminalState()
        assert st.toggle_font() == FONT_RU
        assert st.toggle_font() == FONT_EN


# ─────────────────────────────────────────────────────────────────────────────
# Save/restore
# ─────────────────────────────────────────────────────────────────────────────

class TestSaveRestore:

    def test_save_restore_simple(self):
        st = TerminalState()
        st.x, st.y = 100, 200
        st.save_state()
        st.x, st.y = 500, 500
        assert st.restore_state() is True
        assert (st.x, st.y) == (100, 200)

    def test_restore_empty(self):
        st = TerminalState()
        assert st.restore_state() is False

    def test_nested_save(self):
        st = TerminalState()
        st.x = 1
        st.save_state()
        st.x = 2
        st.save_state()
        st.x = 3
        assert st.restore_state() is True
        assert st.x == 2
        assert st.restore_state() is True
        assert st.x == 1

    def test_saved_depth(self):
        st = TerminalState()
        assert st.saved_depth == 0
        st.save_state()
        assert st.saved_depth == 1
        st.save_state()
        assert st.saved_depth == 2
        st.restore_state()
        assert st.saved_depth == 1

    def test_reset_clears_stack(self):
        st = TerminalState()
        st.save_state()
        st.save_state()
        st.reset()
        assert st.saved_depth == 0


# ─────────────────────────────────────────────────────────────────────────────
# apply_command
# ─────────────────────────────────────────────────────────────────────────────

class TestApplyCommand:

    def test_move_abs(self):
        st = TerminalState()
        st.apply_command("4", [100, 200])
        assert (st.x, st.y) == (100, 200)

    def test_move_rel(self):
        st = TerminalState()
        st.x, st.y = 10, 10
        st.apply_command("5", [5, 5])
        assert (st.x, st.y) == (15, 15)

    def test_line_abs_updates_position(self):
        st = TerminalState()
        st.apply_command("7", [2, 300, 400])
        assert (st.x, st.y) == (300, 400)
        assert st.line_type == 2

    def test_line_rel(self):
        st = TerminalState()
        st.x, st.y = 10, 10
        st.apply_command("8", [1, 5, 5])
        assert (st.x, st.y) == (15, 15)

    def test_shift(self):
        st = TerminalState()
        st.apply_command("}", [100, 50])
        assert (st.shift_x, st.shift_y) == (100, 50)
        st.apply_command("}", [10, 10])
        assert (st.shift_x, st.shift_y) == (110, 60)

    def test_plus_xor(self):
        st = TerminalState()
        st.apply_command("+", [])
        assert st.mode == MODE_OR
        st.apply_command("≠", [])
        assert st.mode == MODE_XOR

    def test_j0_j1(self):
        st = TerminalState()
        st.apply_command("J1", [])
        assert st.graphics is True
        st.apply_command("J0", [])
        assert st.graphics is False

    def test_k_resets(self):
        st = TerminalState()
        st.x = 100
        st.shift_x = 50
        st.apply_command("K", [])
        assert st.x == 0
        assert st.shift_x == 0

    def test_9_sets_text_params(self):
        st = TerminalState()
        st.apply_command("9", [3, 1])
        assert st.text_size == 3
        assert st.text_orient == 1


# ─────────────────────────────────────────────────────────────────────────────
# reset
# ─────────────────────────────────────────────────────────────────────────────

class TestReset:

    def test_reset_all(self):
        st = TerminalState()
        st.x = 100
        st.y = 200
        st.shift_x = 50
        st.shift_y = 30
        st.line_type = 4
        st.font = FONT_RU
        st.graphics = True
        st.save_state()

        st.reset()

        assert st.x == 0
        assert st.y == 0
        assert st.shift_x == 0
        assert st.shift_y == 0
        assert st.line_type == 0
        assert st.font == FONT_EN
        assert st.graphics is False
        assert st.saved_depth == 0


# ─────────────────────────────────────────────────────────────────────────────
# describe / repr
# ─────────────────────────────────────────────────────────────────────────────

class TestRepresentation:

    def test_repr(self):
        st = TerminalState()
        s = repr(st)
        assert "TerminalState" in s

    def test_describe(self):
        st = TerminalState()
        s = st.describe()
        assert "Экран" in s
        assert "Позиция" in s