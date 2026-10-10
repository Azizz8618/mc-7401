"""
backends.py — вывод кадра терминала в разные представления.

Реализованы:

* :class:`SVGBackend`   — векторное изображение (для документации);
* :class:`PNGBackend`   — растровое изображение без внешних зависимостей
  (собственный кодировщик PNG, как в эмуляторе терминала 15ИЭ);
* :class:`TkBackend`    — интерактивный просмотр (Tkinter);
* :class:`PygameBackend`— просмотр анимации (pygame).

Все backend'ы принимают объект :class:`~mc7401.framebuffer.Framebuffer`.
"""

from __future__ import annotations

import logging
import struct
import zlib
from pathlib import Path
from typing import Iterable, Optional

from ..constants import SCREEN_HEIGHT, SCREEN_WIDTH
from ..framebuffer import Framebuffer

log = logging.getLogger(__name__)

#: Цвета по умолчанию.
FG = (0x33, 0xFF, 0x66)      # светящаяся зелёная точка
BG = (0x08, 0x14, 0x0C)      # тёмный фон


# ─────────────────────────────────────────────────────────────────────────────
# Общие помощники
# ─────────────────────────────────────────────────────────────────────────────

def framebuffer_pixels(fb: Framebuffer, fg=FG, bg=BG) -> Iterable[tuple]:
    """Строки пикселей сверху вниз (как на экране, Y вверх)."""
    w, h = fb.width, fb.height
    for row in range(h - 1, -1, -1):
        line = []
        base = row * (w // 8)
        for byte in range(w // 8):
            b = fb._bits[base + byte]
            for bit in range(8):
                line.append(fg if b & (1 << (7 - bit)) else bg)
        yield line


def framebuffer_rows(fb: Framebuffer) -> Iterable[str]:
    """Строки кадра сверху вниз символами `#` и `.` (для отладки)."""
    w, h = fb.width, fb.height
    for row in range(h - 1, -1, -1):
        base = row * (w // 8)
        out = []
        for byte in range(w // 8):
            b = fb._bits[base + byte]
            out.append(''.join('#' if b & (1 << (7 - bit)) else '.'
                               for bit in range(8)))
        yield ''.join(out)


# ─────────────────────────────────────────────────────────────────────────────
# SVG
# ─────────────────────────────────────────────────────────────────────────────

class SVGBackend:
    """Экспорт кадра в SVG: одна горизонтальная линия на ряд точек."""

    name = 'svg'

    def __init__(self, fb: Framebuffer, *, fg: str = '#33ff66',
                 bg: str = '#08140c', scale: int = 1):
        self.fb = fb
        self.fg = fg
        self.bg = bg
        self.scale = max(1, scale)

    def to_svg(self) -> str:
        w = self.fb.width * self.scale
        h = self.fb.height * self.scale
        out = [
            f'<?xml version="1.0" encoding="UTF-8"?>',
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {self.fb.width} {self.fb.height}" '
            f'shape-rendering="crispEdges">',
            f'  <rect width="{self.fb.width}" height="{self.fb.height}" '
            f'fill="{self.bg}"/>',
            f'  <g stroke="{self.fg}" stroke-width="1">',
        ]
        fb = self.fb
        stride = fb.width // 8
        for row in range(fb.height):
            y = fb.height - 1 - row          # экранная Y (снизу вверх)
            base = row * stride
            run_start = None
            for byte in range(stride):
                b = fb._bits[base + byte]
                for bit in range(9):
                    on = bit < 8 and (b & (1 << (7 - bit)))
                    if on and run_start is None:
                        run_start = byte * 8 + bit
                    elif not on and run_start is not None:
                        x = byte * 8 + bit - 1
                        out.append(f'    <line x1="{run_start}" y1="{y}" '
                                   f'x2="{x + 1}" y2="{y}"/>')
                        run_start = None
            if run_start is not None:
                x = fb.width - 1
                out.append(f'    <line x1="{run_start}" y1="{y}" '
                           f'x2="{x + 1}" y2="{y}"/>')
        out.append('  </g>')
        out.append('</svg>')
        return '\n'.join(out) + '\n'

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.write_text(self.to_svg(), encoding='utf-8')
        return p


# ─────────────────────────────────────────────────────────────────────────────
# PNG (без внешних зависимостей)
# ─────────────────────────────────────────────────────────────────────────────

class PNGBackend:
    """Экспорт кадра в PNG средствами стандартной библиотеки."""

    name = 'png'

    def __init__(self, fb: Framebuffer, *, fg=FG, bg=BG, scale: int = 1):
        self.fb = fb
        self.fg = fg
        self.bg = bg
        self.scale = max(1, scale)

    def to_png_bytes(self) -> bytes:
        w = self.fb.width * self.scale
        h = self.fb.height * self.scale
        raw = bytearray()
        fg, bg = self.fg, self.bg
        for line in framebuffer_pixels(self.fb, fg, bg):
            for _ in range(self.scale):
                raw.append(0)                 # фильтр строки: None
                if self.scale == 1:
                    for px in line:
                        raw.extend(px)
                else:
                    row = bytearray()
                    for px in line:
                        row.extend(px * self.scale)
                    raw.extend(row)
        def chunk(tag: bytes, data: bytes) -> bytes:
            return (struct.pack('>I', len(data)) + tag + data +
                    struct.pack('>I', zlib.crc32(tag + data) & 0xFFFFFFFF))
        ihdr = struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)
        return (b'\x89PNG\r\n\x1a\n' +
                chunk(b'IHDR', ihdr) +
                chunk(b'IDAT', zlib.compress(bytes(raw), 9)) +
                chunk(b'IEND', b''))

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.write_bytes(self.to_png_bytes())
        return p


# ─────────────────────────────────────────────────────────────────────────────
# Tkinter
# ─────────────────────────────────────────────────────────────────────────────

class TkBackend:
    """Интерактивный просмотр в окне Tkinter."""

    name = 'tk'

    def __init__(self, fb: Framebuffer, *, fg: str = '#33ff66',
                 bg: str = '#08140c', scale: int = 2, title: str = 'МС 7401'):
        self.fb = fb
        self.fg = fg
        self.bg = bg
        self.scale = max(1, scale)
        self.title = title

    def show(self) -> None:  # pragma: no cover - требует GUI
        import tkinter as tk

        w = self.fb.width * self.scale
        h = self.fb.height * self.scale
        root = tk.Tk()
        root.title(self.title)
        canvas = tk.Canvas(root, width=w, height=h, bg=self.bg,
                           highlightthickness=0)
        canvas.pack()
        fb = self.fb
        for row in range(fb.height):
            y = (fb.height - 1 - row) * self.scale
            base = row * (fb.width // 8)
            for byte in range(fb.width // 8):
                b = fb._bits[base + byte]
                if not b:
                    continue
                for bit in range(8):
                    if b & (1 << (7 - bit)):
                        x = (byte * 8 + bit) * self.scale
                        canvas.create_rectangle(x, y, x + self.scale,
                                                y + self.scale,
                                                fill=self.fg, outline='')
        root.mainloop()


# ─────────────────────────────────────────────────────────────────────────────
# Pygame
# ─────────────────────────────────────────────────────────────────────────────

class PygameBackend:
    """Просмотр (в том числе анимации по кадрам) через pygame."""

    name = 'pygame'

    def __init__(self, fb: Framebuffer, *, fg=FG, bg=BG, scale: int = 2,
                 fps: int = 25):
        self.fb = fb
        self.fg = fg
        self.bg = bg
        self.scale = max(1, scale)
        self.fps = fps

    def show(self, frames: Optional[list[Framebuffer]] = None) -> None:  # pragma: no cover
        import pygame

        pygame.init()
        seq = frames or [self.fb]
        w = self.fb.width * self.scale
        h = self.fb.height * self.scale
        screen = pygame.display.set_mode((w, h))
        pygame.display.set_caption('Электроника МС 7401')
        clock = pygame.time.Clock()
        running = True
        i = 0
        while running:
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    running = False
                elif ev.type == pygame.KEYDOWN and ev.key in (27, ord('q')):
                    running = False
            surface = pygame.Surface((self.fb.width, self.fb.height))
            surface.fill(self.bg)
            fb = seq[i % len(seq)]
            for row in range(fb.height):
                base = row * (fb.width // 8)
                for byte in range(fb.width // 8):
                    b = fb._bits[base + byte]
                    if not b:
                        continue
                    for bit in range(8):
                        if b & (1 << (7 - bit)):
                            x = byte * 8 + bit
                            y = fb.height - 1 - row
                            surface.set_at((x, y), self.fg)
            screen.blit(pygame.transform.scale(surface, (w, h)), (0, 0))
            pygame.display.flip()
            i += 1
            clock.tick(self.fps)
        pygame.quit()


# ─────────────────────────────────────────────────────────────────────────────
# Фабрика
# ─────────────────────────────────────────────────────────────────────────────

BACKENDS = {
    'svg': SVGBackend,
    'png': PNGBackend,
    'tk': TkBackend,
    'pygame': PygameBackend,
}


def render(fb: Framebuffer, kind: str = 'png', **kw):
    """Создать backend по имени."""
    try:
        cls = BACKENDS[kind]
    except KeyError:
        raise ValueError(f'неизвестный backend: {kind!r} '
                         f'(доступны: {", ".join(BACKENDS)})') from None
    return cls(fb, **kw)


def save(fb: Framebuffer, path: str | Path, **kw) -> Path:
    """Сохранить кадр; формат выбирается по расширению файла."""
    p = Path(path)
    suffix = p.suffix.lower()
    kind = {'.svg': 'svg', '.png': 'png'}.get(suffix)
    if kind is None:
        raise ValueError(f'неизвестный формат {suffix!r}')
    return render(fb, kind, **kw).save(p)


def dump_text(fb: Framebuffer) -> str:
    """Текстовое представление кадра (ASCII-арт) для отладки."""
    return '\n'.join(framebuffer_rows(fb)) + '\n'


__all__ = [
    'SVGBackend', 'PNGBackend', 'TkBackend', 'PygameBackend',
    'BACKENDS', 'render', 'save', 'dump_text', 'framebuffer_rows',
    'framebuffer_pixels', 'FG', 'BG',
    'SCREEN_WIDTH', 'SCREEN_HEIGHT',
]