"""
backends/tk_backend.py — backend отрисовки через Tkinter.

Backend для интерактивного просмотра и отладки эмулятора
терминала «Электроника МС 7401».

Особенности:
    - Окно фиксированного размера с масштабированием;
    - Инверсия оси Y (терминал — снизу вверх, Tk — сверху вниз);
    - Поддержка всех трёх перьев (OR/XOR/ERASE);
    - XOR реализован через попиксельную инверсию с использованием
      PIL/Pillow: примитивы кэшируются, и каждый XOR-примитив
      инвертирует фон. Это медленно, но соответствует семантике
      терминала;
    - Для OR-режима используется стандартный Canvas;
    - Горячие клавиши: S — сохранить SVG, Q/Esc — выход, C — очистить;
    - Автоматическое закрытие через mainloop.

Пример:
    state = TerminalState()
    backend = TkBackend(state, scale=1)
    emu = TerminalEmulator(state, backend)
    for cmd in parser:
        emu.execute(cmd)
    backend.run()

Автор: <ваше имя>
Лицензия: MIT
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Optional

from state import (
    TerminalState,
    LINE_DASHES,
    MODE_OR,
    MODE_XOR,
    MODE_ERASE,
)

log = logging.getLogger(__name__)


# Цвета по умолчанию.
COLOR_FG = "#000000"
COLOR_BG = "#ffffff"
COLOR_ERASE = "#ffffff"

# Множитель размера шрифта (как в SVG backend).
FONT_SCALE = 8


# ─────────────────────────────────────────────────────────────────────────────
# Вспомогательные
# ─────────────────────────────────────────────────────────────────────────────

def _dash_pattern(line_type: int, scale: int = 1) -> tuple[int, ...]:
    """Штриховой шаблон для Tkinter (кортеж целых)."""
    dashes = LINE_DASHES.get(line_type, [])
    if not dashes:
        return ()
    return tuple(max(1, d * scale) for d in dashes)


# ─────────────────────────────────────────────────────────────────────────────
# Tk Backend
# ─────────────────────────────────────────────────────────────────────────────

class TkBackend:
    """
    Backend отрисовки в окне Tkinter.

    Параметры:
        state       — объект TerminalState;
        scale       — масштаб окна (1 = 1:1, 2 = увеличение ×2);
        background  — цвет фона;
        title       — заголовок окна;
        save_svg    — путь по умолчанию для сохранения SVG (по клавише S);
        max_xor_ops — сколько XOR-операций допустимо до предупреждения.
    """

    def __init__(
        self,
        state: TerminalState,
        *,
        scale: int = 1,
        background: str = COLOR_BG,
        title: str = "Электроника МС 7401 — эмулятор",
        save_svg: str = "output_tk.svg",
        max_xor_ops: int = 5000,
    ):
        # Импорт внутри метода — чтобы не тянуть tkinter, если backend не нужен.
        try:
            import tkinter as tk
        except ImportError as exc:
            raise ImportError(
                "Tkinter не установлен. Установите python3-tk "
                "(Debian/Ubuntu: apt install python3-tk)."
            ) from exc

        self._tk = tk
        self.state = state
        self.scale = max(1, int(scale))
        self.background = background
        self.save_svg = save_svg
        self.max_xor_ops = max_xor_ops

        # Размеры окна.
        self._win_w = state.width * self.scale
        self._win_h = state.height * self.scale

        # Создаём окно.
        self.root = tk.Tk()
        self.root.title(title)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Canvas.
        self.canvas = tk.Canvas(
            self.root,
            width=self._win_w,
            height=self._win_h,
            bg=background,
            highlightthickness=0,
        )
        self.canvas.pack(fill=tk.BOTH, expand=False)

        # Фон: PIL Image для XOR-режима.
        try:
            from PIL import Image, ImageDraw, ImageTk
            self._PIL = (Image, ImageDraw, ImageTk)
            self._pil_image = Image.new("RGB", (self._win_w, self._win_h), background)
            self._pil_draw = ImageDraw.Draw(self._pil_image)
            self._pil_available = True
        except ImportError:
            log.warning(
                "Pillow не установлен — XOR-режим будет недоступен "
                "в TkBackend. Установите Pillow: pip install Pillow."
            )
            self._PIL = None
            self._pil_image = None
            self._pil_draw = None
            self._pil_available = False

        # Накопленные примитивы для перерисовки.
        self._elements: list[tuple] = []
        self._mode = MODE_OR
        self._xor_count = 0
        self._closed = False
        self._dirty = False

        # Горячие клавиши.
        self.root.bind("<Escape>", lambda e: self._on_close())
        self.root.bind("<q>", lambda e: self._on_close())
        self.root.bind("<Q>", lambda e: self._on_close())
        self.root.bind("<s>", lambda e: self._on_save_svg())
        self.root.bind("<S>", lambda e: self._on_save_svg())
        self.root.bind("<c>", lambda e: self.clear())
        self.root.bind("<C>", lambda e: self.clear())
        self.root.bind("<Key-r>", lambda e: self.redraw())

        log.info(
            "TkBackend: окно %dx%d (scale=%d, PIL=%s)",
            self._win_w, self._win_h, self.scale, self._pil_available,
        )

    # ─── Преобразование координат ────────────────────────────────────────────

    def _scale_xy(self, x: float, y: float) -> tuple[float, float]:
        """Терминальные координаты → координаты окна Tk (с инверсией Y)."""
        sx = x * self.scale
        sy = (self.state.height - y) * self.scale
        return sx, sy

    def _scale_len(self, v: float) -> float:
        return v * self.scale

    # ─── Режим пера ──────────────────────────────────────────────────────────

    def set_mode(self, mode: str) -> None:
        """Сменить режим пера."""
        if mode not in (MODE_OR, MODE_XOR, MODE_ERASE):
            log.warning("TkBackend: неизвестный режим %r", mode)
            return
        self._mode = mode

    def _fg_color(self) -> str:
        if self._mode == MODE_ERASE:
            return COLOR_ERASE
        return COLOR_FG

    # ─── Примитивы ───────────────────────────────────────────────────────────

    def draw_line(
        self,
        x1: int, y1: int,
        x2: int, y2: int,
        line_type: int,
    ) -> None:
        sx1, sy1 = self._scale_xy(x1, y1)
        sx2, sy2 = self._scale_xy(x2, y2)

        if self._mode == MODE_XOR and self._pil_available:
            self._xor_count += 1
            if self._xor_count > self.max_xor_ops:
                log.warning(
                    "TkBackend: превышен лимит XOR-операций (%d)",
                    self.max_xor_ops,
                )
            self._elements.append(("line", x1, y1, x2, y2, line_type, MODE_XOR))
            self._draw_xor_line(sx1, sy1, sx2, sy2)
            self._update_image()
            return

        dash = _dash_pattern(line_type, self.scale)
        self.canvas.create_line(
            sx1, sy1, sx2, sy2,
            fill=self._fg_color(),
            width=max(1, self.scale),
            dash=dash if dash else None,
            capstyle="round" if line_type in (3, 5) else "butt",
        )
        self._elements.append(("line", x1, y1, x2, y2, line_type, MODE_OR))

    def draw_circle(
        self,
        cx: int, cy: int,
        r: int,
        line_type: int,
    ) -> None:
        scx, scy = self._scale_xy(cx, cy)
        sr = self._scale_len(r)

        if self._mode == MODE_XOR and self._pil_available:
            self._xor_count += 1
            self._elements.append(("circle", cx, cy, r, line_type, MODE_XOR))
            self._draw_xor_circle(scx, scy, sr, line_type)
            self._update_image()
            return

        dash = _dash_pattern(line_type, self.scale)
        self.canvas.create_oval(
            scx - sr, scy - sr,
            scx + sr, scy + sr,
            outline=self._fg_color(),
            width=max(1, self.scale),
            dash=dash if dash else None,
        )
        self._elements.append(("circle", cx, cy, r, line_type, MODE_OR))

    def draw_rect(
        self,
        x1: int, y1: int,
        x2: int, y2: int,
        line_type: int,
    ) -> None:
        # Нормализуем.
        rx1, ry1 = min(x1, x2), min(y1, y2)
        rx2, ry2 = max(x1, x2), max(y1, y2)

        sx1, sy1 = self._scale_xy(rx1, ry1)
        sx2, sy2 = self._scale_xy(rx2, ry2)

        # После инверсии Y sy1 может стать больше sy2.
        top, bottom = min(sy1, sy2), max(sy1, sy2)
        left, right = min(sx1, sx2), max(sx1, sx2)

        if self._mode == MODE_XOR and self._pil_available:
            self._elements.append(("rect", x1, y1, x2, y2, line_type, MODE_XOR))
            self._draw_xor_rect(left, top, right, bottom, line_type)
            self._update_image()
            return

        dash = _dash_pattern(line_type, self.scale)
        self.canvas.create_rectangle(
            left, top, right, bottom,
            outline=self._fg_color(),
            width=max(1, self.scale),
            dash=dash if dash else None,
        )
        self._elements.append(("rect", x1, y1, x2, y2, line_type, MODE_OR))

    def draw_text(
        self,
        x: int, y: int,
        text: str,
        size: int,
        orient: int,
        font: str,
    ) -> None:
        sx, sy = self._scale_xy(x, y)

        font_size = max(6, size * FONT_SCALE * self.scale)
        # В Tk по умолчанию шрифт Courier, подходит и для кириллицы.
        font_tuple = ("Courier New", font_size, "normal")

        # Ориентация.
        angle = {0: 0, 1: 90, 2: 180, 3: 270}.get(orient, 0)

        if angle == 0:
            self.canvas.create_text(
                sx, sy,
                text=text,
                fill=self._fg_color(),
                font=font_tuple,
                anchor="w",
            )
        else:
            # Tk не умеет поворачивать текст — эмулируем через
            # последовательный вывод символов вдоль направления.
            self._draw_rotated_text(sx, sy, text, font_tuple, angle)

        self._elements.append(("text", x, y, text, size, orient, font, MODE_OR))

    # ─── Очистка ─────────────────────────────────────────────────────────────

    def clear(self) -> None:
        """Очистить canvas и сбросить накопленные элементы."""
        self.canvas.delete("all")
        self._elements.clear()
        self._xor_count = 0
        if self._pil_available:
            Image, _, _ = self._PIL  # type: ignore
            self._pil_image = Image.new(
                "RGB", (self._win_w, self._win_h), self.background,
            )
            self._pil_draw = __import__("PIL.ImageDraw", fromlist=["ImageDraw"]).Draw(
                self._pil_image,
            )
        self._dirty = True
        log.debug("TkBackend: очищено")

    def redraw(self) -> None:
        """Полная перерисовка всех накопленных элементов."""
        log.debug("TkBackend: перерисовка %d элементов", len(self._elements))
        elements = list(self._elements)
        self.clear()
        for el in elements:
            kind = el[0]
            if kind == "line":
                _, x1, y1, x2, y2, lt, _ = el
                self.draw_line(x1, y1, x2, y2, lt)
            elif kind == "circle":
                _, cx, cy, r, lt, _ = el
                self.draw_circle(cx, cy, r, lt)
            elif kind == "rect":
                _, x1, y1, x2, y2, lt, _ = el
                self.draw_rect(x1, y1, x2, y2, lt)
            elif kind == "text":
                _, x, y, text, size, orient, font, _ = el
                self.draw_text(x, y, text, size, orient, font)

    def present(self) -> None:
        """Обновить окно (вызывается из команды 2)."""
        try:
            self.root.update_idletasks()
            self.root.update()
        except Exception as exc:  # noqa: BLE001
            log.debug("present: %s", exc)

    # ─── Вспомогательные для XOR и поворота ──────────────────────────────────

    def _draw_xor_line(self, x1, y1, x2, y2) -> None:
        """Попиксельная XOR-линия через PIL."""
        if not self._pil_available:
            return
        # Упрощение: рисуем линию через ImageDraw, инвертируя пиксели
        # вдоль неё. PIL не умеет XOR напрямую — используем линию
        # фиксированного цвета, который «переворачивает» пиксели.
        # Реально приходится делать вручную через Bresenham и getpixel/putpixel.
        self._bresenham_xor(x1, y1, x2, y2)

    def _bresenham_xor(self, x1, y1, x2, y2) -> None:
        """Bresenham + XOR пикселей."""
        img = self._pil_image
        if img is None:
            return
        px = img.load()
        w, h = img.size

        x1, y1, x2, y2 = int(round(x1)), int(round(y1)), int(round(x2)), int(round(y2))
        dx = abs(x2 - x1)
        dy = abs(y2 - y1)
        sx = 1 if x1 < x2 else -1
        sy = 1 if y1 < y2 else -1
        err = dx - dy

        while True:
            if 0 <= x1 < w and 0 <= y1 < h:
                r, g, b = px[x1, y1]
                px[x1, y1] = (255 - r, 255 - g, 255 - b)
            if x1 == x2 and y1 == y2:
                break
            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                x1 += sx
            if e2 < dx:
                err += dx
                y1 += sy

    def _draw_xor_circle(self, cx, cy, r, line_type) -> None:
        """XOR-окружность: генерируем точки и инвертируем."""
        steps = max(24, int(2 * math.pi * r / 2))  # ~2 пикселя на шаг
        prev = None
        for i in range(steps + 1):
            t = 2 * math.pi * i / steps
            x = cx + r * math.cos(t)
            y = cy + r * math.sin(t)
            if prev is not None:
                self._bresenham_xor(prev[0], prev[1], x, y)
            prev = (x, y)

    def _draw_xor_rect(self, x1, y1, x2, y2, line_type) -> None:
        """XOR-прямоугольник: четыре отрезка."""
        self._bresenham_xor(x1, y1, x2, y1)
        self._bresenham_xor(x2, y1, x2, y2)
        self._bresenham_xor(x2, y2, x1, y2)
        self._bresenham_xor(x1, y2, x1, y1)

    def _update_image(self) -> None:
        """Обновить Canvas из PIL-изображения."""
        if not self._pil_available:
            return
        ImageTk = self._PIL[2]  # type: ignore
        self._tk_image = ImageTk.PhotoImage(self._pil_image)
        self.canvas.create_image(0, 0, anchor="nw", image=self._tk_image)

    def _draw_rotated_text(
        self,
        sx: float, sy: float,
        text: str,
        font_tuple,
        angle: int,
    ) -> None:
        """Поворот текста через посимвольный вывод."""
        # Для краткости — только вертикальный вариант.
        if angle == 90:
            step = max(8, font_tuple[1])
            for i, ch in enumerate(text):
                self.canvas.create_text(
                    sx, sy - i * step,
                    text=ch,
                    fill=self._fg_color(),
                    font=font_tuple,
                    anchor="w",
                )
        elif angle == 180:
            self.canvas.create_text(
                sx, sy,
                text=text[::-1],
                fill=self._fg_color(),
                font=font_tuple,
                anchor="e",
            )
        elif angle == 270:
            step = max(8, font_tuple[1])
            for i, ch in enumerate(text):
                self.canvas.create_text(
                    sx, sy + i * step,
                    text=ch,
                    fill=self._fg_color(),
                    font=font_tuple,
                    anchor="w",
                )
        else:
            self.canvas.create_text(
                sx, sy,
                text=text,
                fill=self._fg_color(),
                font=font_tuple,
                anchor="w",
            )

    # ─── Сохранение / выход ─────────────────────────────────────────────────

    def _on_save_svg(self) -> None:
        """Сохранить текущий canvas в SVG-файл."""
        try:
            from backends.svg_backend import SVGBackend
        except ImportError:
            log.warning("SVGBackend недоступен — сохранение пропущено")
            return

        svg = SVGBackend(self.state)
        # Проигрываем накопленные элементы через SVG.
        for el in self._elements:
            kind = el[0]
            if kind == "line":
                _, x1, y1, x2, y2, lt, _ = el
                svg.draw_line(x1, y1, x2, y2, lt)
            elif kind == "circle":
                _, cx, cy, r, lt, _ = el
                svg.draw_circle(cx, cy, r, lt)
            elif kind == "rect":
                _, x1, y1, x2, y2, lt, _ = el
                svg.draw_rect(x1, y1, x2, y2, lt)
            elif kind == "text":
                _, x, y, text, size, orient, font, _ = el
                svg.draw_text(x, y, text, size, orient, font)

        path = Path(self.save_svg)
        svg.save(path)
        log.info("SVG сохранён: %s", path)

    def _on_close(self) -> None:
        """Обработчик закрытия окна."""
        if self._closed:
            return
        self._closed = True
        log.info("TkBackend: закрытие окна")
        try:
            self.root.quit()
            self.root.destroy()
        except Exception as exc:  # noqa: BLE001
            log.debug("Ошибка при закрытии: %s", exc)

    # ─── Главный цикл ───────────────────────────────────────────────────────

    def run(self) -> None:
        """Запустить главный цикл Tk."""
        log.info("TkBackend: запуск mainloop")
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            log.info("TkBackend: прервано пользователем")
        finally:
            self._closed = True

    # ─── Отладка ─────────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return (
            f"TkBackend({self._win_w}x{self._win_h}, "
            f"scale={self.scale}, elements={len(self._elements)}, "
            f"mode={self._mode})"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Самотест
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    from state import TerminalState
    from emulator import TerminalEmulator
    from parser import CommandParser
    from reader import CommandStreamReader, VK

    def cmd(s: bytes) -> bytes:
        return s + bytes([VK])

    stream = (
        cmd(b"J1") +
        cmd(b"S") +
        cmd(b"] 8 1 552 398") +
        cmd(b"4 280 200") +
        cmd(b"> 1 50") +
        cmd(b"> 2 80") +
        cmd(b"> 3 110") +
        cmd(b"4 50 350") +
        cmd(b"7 2 500 350") +
        cmd(b"≠") +
        cmd(b"7 2 500 50") +
        cmd(b"7 2 500 350") +   # XOR повторно — вернёт исходное
        cmd(b"+") +
        cmd(b"9 2") +
        cmd(b";0 HELLO MC 7401")
    )

    state = TerminalState()
    backend = TkBackend(state, scale=1, save_svg="test_tk_backend.svg")
    emu = TerminalEmulator(state, backend)

    parser = CommandParser(CommandStreamReader(stream))
    for c in parser:
        emu.execute(c)

    print(f"Накоплено элементов: {len(backend._elements)}")
    print("Backend:", backend)
    print()
    print("Открывается окно. Управление:")
    print("  S — сохранить SVG (test_tk_backend.svg)")
    print("  C — очистить")
    print("  R — перерисовать")
    print("  Q/Esc — выход")

    backend.run()