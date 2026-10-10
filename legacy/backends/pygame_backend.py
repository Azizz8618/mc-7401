"""
backends/pygame_backend.py — backend отрисовки через Pygame.

Backend для анимаций и интерактивного просмотра. Основное
преимущество перед Tkinter — нативная поддержка XOR-режима
через BLEND_RGB_XOR и высокая скорость обновления.

Особенности:
    - Аппаратный XOR через `pygame.Surface.blit(..., special_flags=BLEND_RGB_XOR)`;
    - Двойная буферизация (offscreen surface → экран);
    - Анимации: команда `2` (конец кадра) вызывает `present()`,
      который делает flip и обрабатывает события;
    - Управление частотой кадров через `fps`;
    - Горячие клавиши: Esc — выход, S — сохранить PNG, Space — пауза;
    - Опциональная запись анимации в GIF через Pillow (по клавише R).

Пример:
    state = TerminalState()
    backend = PygameBackend(state, fps=30)
    emu = TerminalEmulator(state, backend)
    for cmd in parser:
        emu.execute(cmd)
    backend.run()   # удерживает окно открытым до закрытия

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


# ─── Цвета ───────────────────────────────────────────────────────────────────

COLOR_FG = (0, 0, 0)         # чёрный
COLOR_BG = (255, 255, 255)   # белый
COLOR_ERASE = (255, 255, 255)

# Множитель размера шрифта (соответствует SVG/Tk backend'ам).
FONT_SCALE = 8


# ─────────────────────────────────────────────────────────────────────────────
# Pygame Backend
# ─────────────────────────────────────────────────────────────────────────────

class PygameBackend:
    """
    Backend отрисовки в окне Pygame.

    Параметры:
        state           — объект TerminalState;
        scale           — масштаб окна (1 = 1:1);
        background      — цвет фона (RGB);
        title           — заголовок окна;
        fps             — частота кадров (для анимаций);
        save_png        — путь по умолчанию для сохранения PNG;
        record_gif      — путь по умолчанию для записи GIF;
        auto_flip       — делать flip на каждой команде 2 (по умолчанию True);
        max_xor_ops     — предупреждать после стольких XOR-операций.
    """

    def __init__(
        self,
        state: TerminalState,
        *,
        scale: int = 1,
        background: tuple[int, int, int] = COLOR_BG,
        title: str = "Электроника МС 7401 — эмулятор",
        fps: int = 30,
        save_png: str = "output_pygame.png",
        record_gif: str = "output_pygame.gif",
        auto_flip: bool = True,
        max_xor_ops: int = 100000,
    ):
        try:
            import pygame
        except ImportError as exc:
            raise ImportError(
                "Pygame не установлен. Установите: pip install pygame"
            ) from exc

        self._pg = pygame
        self.state = state
        self.scale = max(1, int(scale))
        self.background = background
        self.fps = max(1, int(fps))
        self.save_png = save_png
        self.record_gif = record_gif
        self.auto_flip = auto_flip
        self.max_xor_ops = max_xor_ops

        self._win_w = state.width * self.scale
        self._win_h = state.height * self.scale

        # Инициализация pygame.
        pygame.init()
        pygame.display.set_caption(title)
        self.screen = pygame.display.set_mode((self._win_w, self._win_h))
        self.clock = pygame.time.Clock()

        # Offscreen surface — здесь рисуем.
        self.surface = pygame.Surface((self._win_w, self._win_h))
        self.surface.fill(background)

        # Режим и состояние.
        self._mode = MODE_OR
        self._running = True
        self._paused = False
        self._xor_count = 0
        self._closed = False
        self._frames: list = []          # для записи GIF
        self._recording = False

        # Накопленные примитивы для сохранения PNG.
        self._elements: list[tuple] = []

        # Шрифты кэшируются по размеру.
        self._font_cache: dict[tuple[int, str], "pygame.font.Font"] = {}

        # Попытаемся найти моноширинный шрифт с кириллицей.
        self._font_name = self._find_font_name()

        log.info(
            "PygameBackend: окно %dx%d (scale=%d, fps=%d)",
            self._win_w, self._win_h, self.scale, self.fps,
        )

    # ─── Шрифты ──────────────────────────────────────────────────────────────

    def _find_font_name(self) -> Optional[str]:
        """Найти системный моноширинный шрифт (Courier New / DejaVu Sans Mono)."""
        self._pg.font.init()
        candidates = [
            "couriernew", "courier", "consolas",
            "dejavusansmono", "liberationmono", "monospace",
        ]
        try:
            fonts = set(self._pg.font.get_fonts())
        except Exception:
            fonts = set()

        for name in candidates:
            if name in fonts:
                log.debug("PygameBackend: выбран шрифт %r", name)
                return name

        log.debug("PygameBackend: используется шрифт по умолчанию")
        return None

    def _get_font(self, size: int, font_kind: str) -> "pygame.font.Font":
        """Получить (с кэшированием) шрифт заданного размера."""
        key = (size, font_kind)
        if key not in self._font_cache:
            if self._font_name:
                f = self._pg.font.SysFont(self._font_name, size)
            else:
                f = self._pg.font.Font(None, size)
            self._font_cache[key] = f
        return self._font_cache[key]

    # ─── Преобразование координат ────────────────────────────────────────────

    def _xy(self, x: float, y: float) -> tuple[int, int]:
        """Терминальные координаты → экранные (инверсия Y)."""
        sx = int(round(x * self.scale))
        sy = int(round((self.state.height - y) * self.scale))
        return sx, sy

    def _len(self, v: float) -> int:
        return max(1, int(round(v * self.scale)))

    # ─── Режим пера ──────────────────────────────────────────────────────────

    def set_mode(self, mode: str) -> None:
        """Сменить режим пера."""
        if mode not in (MODE_OR, MODE_XOR, MODE_ERASE):
            log.warning("PygameBackend: неизвестный режим %r", mode)
            return
        self._mode = mode

    def _fg_color(self) -> tuple[int, int, int]:
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
        p1 = self._xy(x1, y1)
        p2 = self._xy(x2, y2)

        if self._mode == MODE_XOR:
            self._xor_count += 1
            if self._xor_count > self.max_xor_ops:
                log.warning(
                    "PygameBackend: превышен лимит XOR-операций (%d)",
                    self.max_xor_ops,
                )
            self._draw_xor_line(p1, p2)
            self._elements.append(("line", x1, y1, x2, y2, line_type, MODE_XOR))
            return

        color = self._fg_color()
        width = self._len(1)

        if line_type in (2, 3, 4, 5, 7):
            self._draw_dashed_line(p1, p2, line_type, color, width)
        else:
            self._pg.draw.line(self.surface, color, p1, p2, width)

        self._elements.append(("line", x1, y1, x2, y2, line_type, MODE_OR))

    def draw_circle(
        self,
        cx: int, cy: int,
        r: int,
        line_type: int,
    ) -> None:
        scx, scy = self._xy(cx, cy)
        sr = self._len(r)

        if self._mode == MODE_XOR:
            self._xor_count += 1
            self._draw_xor_circle(scx, scy, sr)
            self._elements.append(("circle", cx, cy, r, line_type, MODE_XOR))
            return

        color = self._fg_color()
        width = self._len(1)

        if line_type in (2, 3, 4, 5, 7):
            self._draw_dashed_circle(scx, scy, sr, line_type, color, width)
        else:
            self._pg.draw.circle(self.surface, color, (scx, scy), sr, width)

        self._elements.append(("circle", cx, cy, r, line_type, MODE_OR))

    def draw_rect(
        self,
        x1: int, y1: int,
        x2: int, y2: int,
        line_type: int,
    ) -> None:
        rx1, ry1 = min(x1, x2), min(y1, y2)
        rx2, ry2 = max(x1, x2), max(y1, y2)

        sx1, sy1 = self._xy(rx1, ry1)
        sx2, sy2 = self._xy(rx2, ry2)

        # После инверсии Y sy1 может быть больше sy2.
        left, right = min(sx1, sx2), max(sx1, sx2)
        top, bottom = min(sy1, sy2), max(sy1, sy2)
        w, h = right - left, bottom - top

        if self._mode == MODE_XOR:
            self._draw_xor_rect(left, top, w, h)
            self._elements.append(("rect", x1, y1, x2, y2, line_type, MODE_XOR))
            return

        color = self._fg_color()
        width = self._len(1)
        rect = self._pg.Rect(left, top, w, h)

        if line_type in (2, 3, 4, 5, 7):
            # Рисуем четыре штриховых отрезка.
            self._draw_dashed_line((left, top), (right, top), line_type, color, width)
            self._draw_dashed_line((right, top), (right, bottom), line_type, color, width)
            self._draw_dashed_line((right, bottom), (left, bottom), line_type, color, width)
            self._draw_dashed_line((left, bottom), (left, top), line_type, color, width)
        else:
            self._pg.draw.rect(self.surface, color, rect, width)

        self._elements.append(("rect", x1, y1, x2, y2, line_type, MODE_OR))

    def draw_text(
        self,
        x: int, y: int,
        text: str,
        size: int,
        orient: int,
        font: str,
    ) -> None:
        sx, sy = self._xy(x, y)

        font_size = max(8, size * FONT_SCALE * self.scale)
        f = self._get_font(font_size, font)
        color = self._fg_color()

        # Рендер текста.
        surf = f.render(text, True, color)

        # Ориентация.
        if orient == 1:
            surf = self._pg.transform.rotate(surf, -90)
        elif orient == 2:
            surf = self._pg.transform.rotate(surf, 180)
        elif orient == 3:
            surf = self._pg.transform.rotate(surf, 90)

        # При XOR — используем специальный флаг.
        if self._mode == MODE_XOR:
            self.surface.blit(
                surf, (sx, sy),
                special_flags=self._pg.BLEND_RGB_XOR,
            )
        else:
            # anchor="w" — left-middle
            rect = surf.get_rect(midleft=(sx, sy))
            self.surface.blit(surf, rect)

        self._elements.append(("text", x, y, text, size, orient, font, MODE_OR))

    # ─── XOR-примитивы ───────────────────────────────────────────────────────

    def _draw_xor_line(self, p1, p2) -> None:
        """
        XOR-линия через отдельный surface.

        Идея: создаём surface с чёрным цветом вдоль линии,
        затем blit с BLEND_RGB_XOR — это инвертирует пиксели.
        """
        # Ограничивающий прямоугольник.
        x1, y1 = p1
        x2, y2 = p2
        w = max(abs(x2 - x1), 1)
        h = max(abs(y2 - y1), 1)
        temp = self._pg.Surface((w + 1, h + 1))
        temp.fill((0, 0, 0))

        # Рисуем диагональ в локальных координатах.
        lx1 = 0 if x1 <= x2 else w
        ly1 = 0 if y1 <= y2 else h
        lx2 = w if x1 <= x2 else 0
        ly2 = h if y1 <= y2 else 0

        self._pg.draw.line(temp, (255, 255, 255), (lx1, ly1), (lx2, ly2), self._len(1))
        self.surface.blit(
            temp, (min(x1, x2), min(y1, y2)),
            special_flags=self._pg.BLEND_RGB_XOR,
        )

    def _draw_xor_circle(self, cx: int, cy: int, r: int) -> None:
        """XOR-окружность: генерируем точки и XORим их попиксельно."""
        if r < 1:
            return
        steps = max(32, int(2 * math.pi * r / 2))
        prev = None
        for i in range(steps + 1):
            t = 2 * math.pi * i / steps
            px = int(round(cx + r * math.cos(t)))
            py = int(round(cy + r * math.sin(t)))
            if prev is not None:
                self._draw_xor_line(prev, (px, py))
            prev = (px, py)

    def _draw_xor_rect(self, left: int, top: int, w: int, h: int) -> None:
        """XOR-прямоугольник: четыре XOR-линии."""
        self._draw_xor_line((left, top), (left + w, top))
        self._draw_xor_line((left + w, top), (left + w, top + h))
        self._draw_xor_line((left + w, top + h), (left, top + h))
        self._draw_xor_line((left, top + h), (left, top))

    # ─── Штриховые примитивы ─────────────────────────────────────────────────

    def _dash_pattern(self, line_type: int) -> list[int]:
        """Штриховой шаблон в пикселях окна."""
        base = LINE_DASHES.get(line_type, [])
        return [max(1, d * self.scale) for d in base]

    def _draw_dashed_line(
        self,
        p1: tuple[int, int],
        p2: tuple[int, int],
        line_type: int,
        color: tuple[int, int, int],
        width: int,
    ) -> None:
        """
        Рисует штриховую линию вручную: делит отрезок на сегменты
        согласно шаблону и рисует их через pygame.draw.line.
        """
        pattern = self._dash_pattern(line_type)
        if not pattern:
            self._pg.draw.line(self.surface, color, p1, p2, width)
            return

        x1, y1 = p1
        x2, y2 = p2
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy)
        if length == 0:
            return

        ux, uy = dx / length, dy / length

        pos = 0.0
        idx = 0
        draw_on = True  # начинаем со штриха

        while pos < length:
            seg_len = pattern[idx % len(pattern)]
            seg_len = min(seg_len, length - pos)

            if draw_on:
                sx1 = x1 + ux * pos
                sy1 = y1 + uy * pos
                sx2 = x1 + ux * (pos + seg_len)
                sy2 = y1 + uy * (pos + seg_len)
                self._pg.draw.line(
                    self.surface, color,
                    (int(round(sx1)), int(round(sy1))),
                    (int(round(sx2)), int(round(sy2))),
                    width,
                )

            pos += seg_len
            idx += 1
            draw_on = not draw_on

    def _draw_dashed_circle(
        self,
        cx: int, cy: int, r: int,
        line_type: int,
        color: tuple[int, int, int],
        width: int,
    ) -> None:
        """Штриховая окружность — сегментами по 2π/шагов."""
        if r < 1:
            return
        steps = max(48, int(2 * math.pi * r / 3))
        pattern = self._dash_pattern(line_type)
        if not pattern:
            self._pg.draw.circle(self.surface, color, (cx, cy), r, width)
            return

        # Считаем общую длину окружности и раскладываем шаблон по ней.
        circumference = 2 * math.pi * r
        # Растягиваем шаблон, чтобы уложить его целое число раз.
        total = sum(pattern)
        if total == 0:
            return
        repeats = max(1, int(circumference / total))
        seg_len_arc = circumference / (repeats * len(pattern))

        idx = 0
        draw_on = True
        pos = 0.0

        while idx < repeats * len(pattern):
            step = seg_len_arc
            if draw_on:
                a1 = 2 * math.pi * pos / circumference
                a2 = 2 * math.pi * (pos + step) / circumference
                # pygame.draw.arc хочет углы в радианах, но с осью Y вниз
                # и против часовой — инвертируем.
                rect = self._pg.Rect(cx - r, cy - r, 2 * r, 2 * r)
                self._pg.draw.arc(
                    self.surface, color, rect,
                    a1, a2, width,
                )
            pos += step
            idx += 1
            draw_on = not draw_on

    # ─── Очистка ─────────────────────────────────────────────────────────────

    def clear(self) -> None:
        """Очистить экран и накопленные элементы."""
        self.surface.fill(self.background)
        self._elements.clear()
        self._xor_count = 0
        log.debug("PygameBackend: очищено")

    def present(self) -> None:
        """
        Показать текущий кадр: blit offscreen surface на экран,
        сделать flip, обработать события, дождаться fps.
        """
        if not self.auto_flip:
            return

        # Обработка событий.
        for event in self._pg.event.get():
            if event.type == self._pg.QUIT:
                self._running = False
            elif event.type == self._pg.KEYDOWN:
                if event.key == self._pg.K_ESCAPE:
                    self._running = False
                elif event.key == self._pg.K_s:
                    self._save_png_now()
                elif event.key == self._pg.K_SPACE:
                    self._paused = not self._paused
                    log.info("Пауза: %s", self._paused)
                elif event.key == self._pg.K_r:
                    self._toggle_recording()

        if self._paused:
            # В паузе не обновляем экран, но не выходим из цикла.
            self.clock.tick(self.fps)
            return

        # Копируем offscreen surface на экран.
        self.screen.blit(self.surface, (0, 0))
        self._pg.display.flip()

        # Запись кадра в GIF.
        if self._recording:
            self._record_frame()

        self.clock.tick(self.fps)

    # ─── Сохранение / запись ─────────────────────────────────────────────────

    def _save_png_now(self) -> None:
        """Сохранить текущий surface в PNG."""
        path = Path(self.save_png)
        try:
            self._pg.image.save(self.surface, str(path))
            log.info("PNG сохранён: %s", path)
        except Exception as exc:  # noqa: BLE001
            log.error("Не удалось сохранить PNG: %s", exc)

    def _toggle_recording(self) -> None:
        """Начать/закончить запись GIF."""
        if self._recording:
            self._recording = False
            self._save_gif()
        else:
            self._frames = []
            self._recording = True
            log.info("Запись GIF начата (R — остановить)")

    def _record_frame(self) -> None:
        """Сохранить текущий кадр в список (для GIF)."""
        try:
            from PIL import Image
        except ImportError:
            log.warning("Pillow не установлен — запись GIF недоступна")
            self._recording = False
            return

        # Копируем surface в PIL Image.
        raw = self._pg.image.tostring(self.surface, "RGB")
        img = Image.frombytes("RGB", (self._win_w, self._win_h), raw)
        # Уменьшим для GIF (иначе файл огромный).
        img = img.resize(
            (self._win_w // 2, self._win_h // 2),
            Image.NEAREST,
        )
        self._frames.append(img)

    def _save_gif(self) -> None:
        """Сохранить накопленные кадры в GIF."""
        try:
            from PIL import Image
        except ImportError:
            log.warning("Pillow не установлен — GIF не сохранён")
            return

        if not self._frames:
            log.warning("Нет кадров для записи")
            return

        path = Path(self.record_gif)
        try:
            self._frames[0].save(
                path,
                save_all=True,
                append_images=self._frames[1:],
                duration=int(1000 / self.fps),
                loop=0,
            )
            log.info("GIF сохранён: %s (%d кадров)", path, len(self._frames))
        except Exception as exc:  # noqa: BLE001
            log.error("Не удалось сохранить GIF: %s", exc)

    def save_png(self, path: Optional[str] = None) -> Path:
        """Публичный метод сохранения PNG."""
        p = Path(path or self.save_png)
        self._pg.image.save(self.surface, str(p))
        log.info("PNG сохранён: %s", p)
        return p

    # ─── Главный цикл ────────────────────────────────────────────────────────

    def run(self, *, hold_open: bool = True) -> None:
        """
        Запустить цикл обработки событий.

        Параметры:
            hold_open — удерживать окно открытым после исполнения команд
                        (по умолчанию True — ждём закрытия).
        """
        log.info("PygameBackend: запуск цикла событий (hold_open=%s)", hold_open)

        try:
            if hold_open:
                while self._running:
                    self.present()
            else:
                # Одиночный flip без ожидания.
                self.screen.blit(self.surface, (0, 0))
                self._pg.display.flip()
        except KeyboardInterrupt:
            log.info("PygameBackend: прервано пользователем")
        finally:
            self._shutdown()

    def step(self) -> None:
        """
        Обработать события и обновить экран один раз (без цикла).
        Полезно для тестов и покадровой отладки.
        """
        self.present()

    def _shutdown(self) -> None:
        """Завершение работы Pygame."""
        if self._closed:
            return
        self._closed = True
        log.info("PygameBackend: завершение")
        if self._recording:
            self._save_gif()
        try:
            self._pg.quit()
        except Exception as exc:  # noqa: BLE001
            log.debug("Ошибка при завершении pygame: %s", exc)

    # ─── Публичные свойства ──────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._running

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def xor_count(self) -> int:
        return self._xor_count

    @property
    def element_count(self) -> int:
        return len(self._elements)

    # ─── Отладка ─────────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return (
            f"PygameBackend({self._win_w}x{self._win_h}, "
            f"scale={self.scale}, fps={self.fps}, "
            f"elements={self.element_count}, mode={self._mode})"
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

    # Сценарий: рамка, три окружности, две OR-линии, две XOR-линии,
    # текст. Затем окно висит, пока пользователь не закроет его.
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
        cmd(b"+") +
        cmd(b"4 50 50") +
        cmd(b"7 2 500 350") +
        cmd(b"≠") +
        cmd(b"4 500 50") +
        cmd(b"7 2 50 350") +
        cmd(b"7 2 500 350") +   # XOR-наложение
        cmd(b"+") +
        cmd(b"9 2") +
        cmd(b";0 HELLO MC 7401")
    )

    state = TerminalState()
    backend = PygameBackend(state, scale=1, fps=30)
    emu = TerminalEmulator(state, backend)

    parser = CommandParser(CommandStreamReader(stream))
    for c in parser:
        emu.execute(c)

    print(f"Накоплено элементов: {backend.element_count}")
    print(f"XOR-операций: {backend.xor_count}")
    print("Backend:", backend)
    print()
    print("Открыто окно Pygame. Управление:")
    print("  Esc — выход")
    print("  S — сохранить PNG (output_pygame.png)")
    print("  Space — пауза")
    print("  R — начать/закончить запись GIF (output_pygame.gif)")

    backend.run()