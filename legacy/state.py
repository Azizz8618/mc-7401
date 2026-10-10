"""
state.py — состояние эмулятора терминала «Электроника МС 7401».

Модуль описывает класс TerminalState — единое изменяемое состояние
эмулятора: текущая позиция, смещение окна, тип линии, перо, шрифт,
режим (текст/графика), размеры экрана и т. д.

Класс также предоставляет:
    - методы работы с координатами (абсолютные / относительные);
    - методы сохранения и восстановления состояния (аппаратная
      возможность терминала, упомянутая в статье МПСС 5/1989);
    - отсечение отрезков по краю экрана (терминал отсекает изображение
      всегда, отключить нельзя);
    - преобразование типа линии в штриховой шаблон (dash array).

Автор: <ваше имя>
Лицензия: MIT
"""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Константы
# ─────────────────────────────────────────────────────────────────────────────

# Размеры экрана MC 7401 (из статьи МПСС 5/1989 и из graf.pas).
SCREEN_WIDTH  = 560
SCREEN_HEIGHT = 400

# Режимы рисования.
MODE_OR   = "OR"
MODE_XOR  = "XOR"
MODE_ERASE = "ERASE"    # стирающее перо
MODE_INVERT = "INVERT"  # синоним XOR

# Шрифты.
FONT_EN = "EN"
FONT_RU = "RU"

# Режимы терминала.
MODE_TEXT     = "TEXT"
MODE_GRAPHICS = "GRAPHICS"

# Типы линий (0..7). Тип 6 отключён в терминале.
LINE_TYPES: dict[int, str] = {
    0: "solid",
    1: "solid",
    2: "dashed",
    3: "dotted",
    4: "dash-dot",
    5: "dot-dot",
    6: "disabled",
    7: "complex",
}

# Штриховые шаблоны для SVG (list of dash, gap, ...).
# Ключ — тип линии, значение — список длин штрихов/пробелов.
LINE_DASHES: dict[int, list[int]] = {
    0: [],
    1: [],
    2: [4, 2],
    3: [2, 2],
    4: [6, 2, 2, 2],
    5: [1, 2],
    6: [],
    7: [8, 2, 2, 2, 2, 2],
}


# ─────────────────────────────────────────────────────────────────────────────
# Состояние
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TerminalState:
    """
    Изменяемое состояние терминала.

    Координатная система:
        - Начало координат — левый нижний угол экрана.
        - X — вправо, Y — вверх.
        - Диапазон: 0 <= X <= width, 0 <= Y <= height.
        - Реальные координаты могут выходить за пределы экрана,
          но при отрисовке отсекаются.

    Смещение окна (shift_x, shift_y) накапливается командой `}` и
    прибавляется ко всем абсолютным координатам в командах `4`, `7`, `]`.
    """

    # ─── Экран ───────────────────────────────────────────────────────────────
    width: int = SCREEN_WIDTH
    height: int = SCREEN_HEIGHT

    # ─── Позиция ─────────────────────────────────────────────────────────────
    x: int = 0
    y: int = 0

    # ─── Смещение окна ───────────────────────────────────────────────────────
    shift_x: int = 0
    shift_y: int = 0

    # ─── Параметры пера и линии ──────────────────────────────────────────────
    line_type: int = 0
    mode: str = MODE_OR
    color: int = 1

    # ─── Шрифт ───────────────────────────────────────────────────────────────
    font: str = FONT_EN
    text_size: int = 1
    text_orient: int = 0

    # ─── Режим терминала ─────────────────────────────────────────────────────
    graphics: bool = False          # J0 → False, J1 → True
    clipping: bool = True           # MC 7401 отсекает всегда

    # ─── Служебное ───────────────────────────────────────────────────────────
    _saved_states: list["TerminalState"] = field(
        default_factory=list, repr=False,
    )
    _last_op: Optional[str] = field(default=None, repr=False)

    # ─── Копирование ─────────────────────────────────────────────────────────

    def clone(self) -> "TerminalState":
        """Полная независимая копия состояния (без стека saved_states)."""
        s = TerminalState(
            width=self.width,
            height=self.height,
            x=self.x,
            y=self.y,
            shift_x=self.shift_x,
            shift_y=self.shift_y,
            line_type=self.line_type,
            mode=self.mode,
            color=self.color,
            font=self.font,
            text_size=self.text_size,
            text_orient=self.text_orient,
            graphics=self.graphics,
            clipping=self.clipping,
        )
        s._last_op = self._last_op
        return s

    # ─── Координаты ──────────────────────────────────────────────────────────

    def abs_pos(self) -> tuple[int, int]:
        """Абсолютные координаты текущей точки (со смещением окна)."""
        return self.x + self.shift_x, self.y + self.shift_y

    def to_screen(self, x: int, y: int) -> tuple[int, int]:
        """
        Преобразовать «логические» координаты (со смещением окна)
        в экранные. Удобно для backend'ов.
        """
        return x + self.shift_x, y + self.shift_y

    def in_bounds(self, x: int, y: int) -> bool:
        """Попадает ли точка (без смещения) в область экрана."""
        return 0 <= x <= self.width and 0 <= y <= self.height

    def in_bounds_abs(self, x: int, y: int) -> bool:
        """Попадает ли абсолютная точка (со смещением) в область экрана."""
        ax, ay = self.to_screen(x, y)
        return 0 <= ax <= self.width and 0 <= ay <= self.height

    def clip_line(
        self,
        x1: int, y1: int,
        x2: int, y2: int,
    ) -> Optional[tuple[int, int, int, int]]:
        """
        Отсечь отрезок по границам экрана.

        Принимает абсолютные экранные координаты.
        Возвращает (x1, y1, x2, y2) или None, если отрезок полностью
        вне экрана. Использует алгоритм Коэна — Сазерленда.
        """
        if not self.clipping:
            return x1, y1, x2, y2

        INSIDE, LEFT, RIGHT, BOTTOM, TOP = 0, 1, 2, 4, 8

        def code(px: int, py: int) -> int:
            c = INSIDE
            if px < 0:
                c |= LEFT
            elif px > self.width:
                c |= RIGHT
            if py < 0:
                c |= BOTTOM
            elif py > self.height:
                c |= TOP
            return c

        c1 = code(x1, y1)
        c2 = code(x2, y2)

        while True:
            if not (c1 | c2):
                # оба конца внутри
                return x1, y1, x2, y2
            if c1 & c2:
                # оба конца в одной внешней полуплоскости
                return None

            c_out = c1 or c2
            if c_out & TOP:
                x = x1 + (x2 - x1) * (self.height - y1) // (y2 - y1)
                y = self.height
            elif c_out & BOTTOM:
                x = x1 + (x2 - x1) * (0 - y1) // (y2 - y1)
                y = 0
            elif c_out & RIGHT:
                y = y1 + (y2 - y1) * (self.width - x1) // (x2 - x1)
                x = self.width
            elif c_out & LEFT:
                y = y1 + (y2 - y1) * (0 - x1) // (x2 - x1)
                x = 0
            else:
                return x1, y1, x2, y2  # на всякий случай

            if c_out == c1:
                x1, y1 = x, y
                c1 = code(x1, y1)
            else:
                x2, y2 = x, y
                c2 = code(x2, y2)

    # ─── Тип линии ───────────────────────────────────────────────────────────

    def dash_array(self) -> list[int]:
        """Штриховой шаблон для текущего типа линии."""
        return LINE_DASHES.get(self.line_type, [])

    def line_type_name(self) -> str:
        """Человекочитаемое имя текущего типа линии."""
        return LINE_TYPES.get(self.line_type, "unknown")

    def is_drawing(self) -> bool:
        """Рисует ли текущий тип линии (6 — отключён)."""
        return self.line_type != 6

    # ─── Перо ────────────────────────────────────────────────────────────────

    def is_xor(self) -> bool:
        """Активен ли XOR-режим."""
        return self.mode in (MODE_XOR, MODE_INVERT)

    def is_erase(self) -> bool:
        """Активен ли стирающий режим."""
        return self.mode == MODE_ERASE

    # ─── Шрифт ───────────────────────────────────────────────────────────────

    def set_font(self, font: str) -> None:
        if font not in (FONT_EN, FONT_RU):
            raise ValueError(f"Неизвестный шрифт: {font!r}")
        self.font = font

    def toggle_font(self) -> str:
        self.font = FONT_RU if self.font == FONT_EN else FONT_EN
        return self.font

    def is_cyrillic(self) -> bool:
        return self.font == FONT_RU

    # ─── Сохранение и восстановление ─────────────────────────────────────────

    def save_state(self) -> None:
        """
        Запомнить текущее состояние (аппаратная команда терминала).
        Стек неограничен — на практике глубина небольшая.
        """
        self._saved_states.append(self.clone())
        log.debug("save_state: глубина стека = %d", len(self._saved_states))

    def restore_state(self) -> bool:
        """
        Восстановить последнее сохранённое состояние.
        Возвращает False, если стек пуст.
        """
        if not self._saved_states:
            log.warning("restore_state: стек пуст")
            return False
        s = self._saved_states.pop()
        # сохраняем стек, чтобы не потерять вложенные сохранения
        saved_stack = self._saved_states
        self.__dict__.update(s.__dict__)
        self._saved_states = saved_stack
        log.debug("restore_state: глубина стека = %d", len(self._saved_states))
        return True

    @property
    def saved_depth(self) -> int:
        """Текущая глубина стека сохранённых состояний."""
        return len(self._saved_states)

    # ─── Сброс ───────────────────────────────────────────────────────────────

    def reset(self) -> None:
        """Сбросить состояние до начального (команда K)."""
        self.x = 0
        self.y = 0
        self.shift_x = 0
        self.shift_y = 0
        self.line_type = 0
        self.mode = MODE_OR
        self.color = 1
        self.font = FONT_EN
        self.text_size = 1
        self.text_orient = 0
        self.graphics = False
        self.clipping = True
        self._saved_states.clear()
        self._last_op = None

    # ─── Установка параметров ────────────────────────────────────────────────

    def set_line_type(self, t: int) -> None:
        """Установить тип линии (0..7). Тип 6 отключён, но принимается."""
        if not 0 <= t <= 7:
            log.warning("Недопустимый тип линии: %d", t)
            return
        self.line_type = t

    def set_mode(self, mode: str) -> None:
        if mode in (MODE_OR, MODE_XOR, MODE_ERASE, MODE_INVERT):
            self.mode = mode
        else:
            log.warning("Неизвестный режим: %r", mode)

    def set_graphics(self, on: bool) -> None:
        """Переключить режим терминала."""
        self.graphics = bool(on)

    # ─── Применение команды ──────────────────────────────────────────────────

    def apply_command(self, op: str, args: list[int]) -> None:
        """
        Обновить состояние по команде. Вызывается из TerminalEmulator
        до передачи примитивов в backend.

        Здесь — только те изменения, которые касаются самого состояния,
        а не отрисовки (отрисовка — в backend'е).
        """
        self._last_op = op

        if op == "4":                       # абсолютное перемещение
            if len(args) >= 2:
                self.x, self.y = args[0], args[1]

        elif op == "5":                     # относительное перемещение
            if len(args) >= 2:
                self.x += args[0]
                self.y += args[1]

        elif op == "7":                     # линия к абсолютной точке
            if len(args) >= 3:
                t, self.x, self.y = args[0], args[1], args[2]
                self.set_line_type(t)

        elif op == "8":                     # линия относительно точки
            if len(args) >= 3:
                t, dx, dy = args[0], args[1], args[2]
                self.set_line_type(t)
                self.x += dx
                self.y += dy

        elif op == "0":                     # перо/цвет
            if args:
                self.color = args[0]

        elif op == "1":                     # тип/режим
            if args:
                self.set_line_type(args[0])

        elif op == "}":                     # смещение окна
            if len(args) >= 2:
                self.shift_x += args[0]
                self.shift_y += args[1]

        elif op == "+":
            self.set_mode(MODE_OR)

        elif op == "≠":
            self.set_mode(MODE_XOR)

        elif op == "J0":
            self.set_graphics(False)

        elif op == "J1":
            self.set_graphics(True)

        elif op == "K":
            self.reset()

        elif op == "9":                     # текст
            if len(args) >= 2:
                self.text_size = args[0]
                self.text_orient = args[1]

        # для остальных команд (2, S, F, ], >) состояние не меняется —
        # вся работа уходит в backend.

    # ─── Представление ───────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return (
            f"TerminalState("
            f"{self.width}x{self.height}, "
            f"pos=({self.x},{self.y}), "
            f"shift=({self.shift_x},{self.shift_y}), "
            f"line={self.line_type}, "
            f"mode={self.mode}, "
            f"font={self.font}, "
            f"graphics={self.graphics})"
        )

    def describe(self) -> str:
        """Многострочное описание состояния (для отладки)."""
        lines = [
            f"Экран:      {self.width} x {self.height}",
            f"Позиция:    ({self.x}, {self.y})",
            f"Смещение:   ({self.shift_x}, {self.shift_y})",
            f"Абс. позиция: {self.abs_pos()}",
            f"Тип линии:  {self.line_type} ({self.line_type_name()})",
            f"Режим:      {self.mode}",
            f"Шрифт:      {self.font}",
            f"Размер текста: {self.text_size}, ориентация: {self.text_orient}",
            f"Графика:    {self.graphics}",
            f"Отсечение:  {self.clipping}",
            f"Стек saved: {self.saved_depth}",
        ]
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Самотест
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")

    st = TerminalState()
    print(st.describe())
    print()

    # Тест применения команд.
    st.apply_command("4", [100, 200])
    st.apply_command("}", [50, 30])
    st.apply_command("7", [2, 400, 300])
    st.apply_command("+", [])
    st.apply_command("9", [2, 1])
    print(st.describe())
    print()

    # Тест save/restore.
    st.save_state()
    st.apply_command("4", [0, 0])
    st.apply_command("}", [1000, 1000])
    print(f"После изменений: pos=({st.x},{st.y}), shift=({st.shift_x},{st.shift_y})")
    st.restore_state()
    print(f"После restore:   pos=({st.x},{st.y}), shift=({st.shift_x},{st.shift_y})")
    print()

    # Тест отсечения.
    print("clip_line(100,100, 700,500) =", st.clip_line(100, 100, 700, 500))
    print("clip_line(-100,-50, -20,-10) =", st.clip_line(-100, -50, -20, -10))
    print("clip_line(10,10, 500,300)  =", st.clip_line(10, 10, 500, 300))

    # Тест dash.
    print()
    for t in range(8):
        st.set_line_type(t)
        print(f"  тип {t}: {st.line_type_name():10s} dash={st.dash_array()}")