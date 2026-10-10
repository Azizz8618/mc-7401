"""
backends/svg_backend.py — backend отрисовки в SVG для эмулятора
терминала «Электроника МС 7401».

Backend накапливает графические примитивы и сохраняет их в файл
формата SVG. Подходит для статических изображений, отладки и
экспорта в документацию.

Особенности:
    - Инверсия оси Y (терминал — снизу вверх, SVG — сверху вниз);
    - Штриховые шаблоны по типам линий (0..7);
    - Поддержка кириллицы (через шрифт monospace + Unicode);
    - Кодировка файла — UTF-8;
    - Группировка элементов по слоям (линии/текст/формы);
    - Опциональная «белая подложка» и рамка.

Пример:
    state = TerminalState()
    backend = SVGBackend(state)
    emu = TerminalEmulator(state, backend)
    ...
    backend.save("output.svg")

Автор: <ваше имя>
Лицензия: MIT
"""

from __future__ import annotations

import html
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from state import (
    TerminalState,
    LINE_DASHES,
    FONT_EN,
    FONT_RU,
    MODE_OR,
    MODE_XOR,
    MODE_ERASE,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Вспомогательные
# ─────────────────────────────────────────────────────────────────────────────

# Шрифты для русского и латинского текста.
# SVG не гарантирует наличие конкретного шрифта — задаём fallback-цепочку.
FONT_FAMILY_EN = "'Courier New', 'Consolas', monospace"
FONT_FAMILY_RU = "'Courier New', 'Consolas', monospace"

# Множитель размера шрифта: size=1 → ~8px, size=2 → ~16px и т. д.
FONT_SCALE = 8

# Цвета для разных режимов пера.
STROKE_COLOR_OR    = "#000000"
STROKE_COLOR_XOR   = "#000000"   # SVG не поддерживает XOR — рисуем чёрным
STROKE_COLOR_ERASE = "#ffffff"   # стирающее перо — белым


# ─────────────────────────────────────────────────────────────────────────────
# SVG-элемент
# ─────────────────────────────────────────────────────────────────────────────

class _Element:
    """Один SVG-элемент с координатами для отладки."""
    __slots__ = ("kind", "attrs", "raw")

    def __init__(self, kind: str, attrs: dict, raw: str = ""):
        self.kind = kind
        self.attrs = attrs
        self.raw = raw

    def to_svg(self) -> str:
        if self.raw:
            return self.raw
        parts = [f"<{self.kind}"]
        for k, v in self.attrs.items():
            parts.append(f' {k}="{html.escape(str(v), quote=True)}"')
        parts.append("/>")
        return "".join(parts)

    def __repr__(self) -> str:
        return f"_Element({self.kind}, {self.attrs})"


# ─────────────────────────────────────────────────────────────────────────────
# SVG Backend
# ─────────────────────────────────────────────────────────────────────────────

class SVGBackend:
    """
    Backend для генерации SVG-файла.

    Параметры:
        state        — объект TerminalState (источник размеров и параметров);
        stroke_width — толщина линий в пикселях (по умолчанию 1);
        background   — цвет фона (по умолчанию белый);
        border       — рисовать рамку по краям экрана (по умолчанию False);
        metadata     — добавлять в SVG метаданные (дата, эмулятор).
    """

    def __init__(
        self,
        state: TerminalState,
        *,
        stroke_width: int = 1,
        background: str = "#ffffff",
        border: bool = False,
        metadata: bool = True,
    ):
        self.state = state
        self.stroke_width = stroke_width
        self.background = background
        self.border = border
        self.metadata = metadata

        self._elements: list[_Element] = []
        self._mode = MODE_OR
        self._scale_x = 1.0
        self._scale_y = 1.0

    # ─── Свойства ────────────────────────────────────────────────────────────

    @property
    def width(self) -> int:
        return self.state.width

    @property
    def height(self) -> int:
        return self.state.height

    @property
    def element_count(self) -> int:
        """Число накопленных примитивов."""
        return len(self._elements)

    # ─── Преобразование координат ────────────────────────────────────────────

    def _flip_y(self, y: float) -> float:
        """Инверсия оси Y: SVG считает Y сверху вниз, терминал — снизу вверх."""
        return self.height - y

    def _xy(self, x: float, y: float) -> tuple[float, float]:
        """Преобразовать координаты терминала в координаты SVG."""
        return x * self._scale_x, self._flip_y(y) * self._scale_y

    # ─── Управление режимом ──────────────────────────────────────────────────

    def set_mode(self, mode: str) -> None:
        """Установить режим пера (OR/XOR/ERASE)."""
        if mode not in (MODE_OR, MODE_XOR, MODE_ERASE):
            log.warning("SVGBackend: неизвестный режим %r", mode)
            return
        self._mode = mode

    def _stroke_color(self) -> str:
        if self._mode == MODE_ERASE:
            return STROKE_COLOR_ERASE
        if self._mode == MODE_XOR:
            # SVG не умеет XOR — используем чёрный с пометкой в data-атрибуте.
            return STROKE_COLOR_XOR
        return STROKE_COLOR_OR

    def _dash_attr(self, line_type: int) -> str:
        """Атрибут stroke-dasharray для типа линии."""
        dashes = LINE_DASHES.get(line_type, [])
        if not dashes:
            return ""
        return " ".join(str(d) for d in dashes)

    # ─── Примитивы backend'а ─────────────────────────────────────────────────

    def draw_line(
        self,
        x1: int, y1: int,
        x2: int, y2: int,
        line_type: int,
    ) -> None:
        sx1, sy1 = self._xy(x1, y1)
        sx2, sy2 = self._xy(x2, y2)

        attrs = {
            "x1": round(sx1, 2),
            "y1": round(sy1, 2),
            "x2": round(sx2, 2),
            "y2": round(sy2, 2),
            "stroke": self._stroke_color(),
            "stroke-width": self.stroke_width,
            "stroke-linecap": "round" if line_type in (3, 5) else "butt",
        }
        dash = self._dash_attr(line_type)
        if dash:
            attrs["stroke-dasharray"] = dash
        if self._mode == MODE_XOR:
            attrs["data-mode"] = "xor"

        self._elements.append(_Element("line", attrs))

    def draw_circle(
        self,
        cx: int, cy: int,
        r: int,
        line_type: int,
    ) -> None:
        scx, scy = self._xy(cx, cy)

        attrs = {
            "cx": round(scx, 2),
            "cy": round(scy, 2),
            "r": r,
            "fill": "none",
            "stroke": self._stroke_color(),
            "stroke-width": self.stroke_width,
        }
        dash = self._dash_attr(line_type)
        if dash:
            attrs["stroke-dasharray"] = dash
        if self._mode == MODE_XOR:
            attrs["data-mode"] = "xor"

        self._elements.append(_Element("circle", attrs))

    def draw_rect(
        self,
        x1: int, y1: int,
        x2: int, y2: int,
        line_type: int,
    ) -> None:
        # Приводим координаты так, чтобы x2 > x1, y2 > y1.
        sx1, sy1 = self._xy(min(x1, x2), min(y1, y2))
        sx2, sy2 = self._xy(max(x1, x2), max(y1, y2))

        # После инверсии Y sy1 может оказаться больше sy2.
        top = min(sy1, sy2)
        left = min(sx1, sx2)
        w = abs(sx2 - sx1)
        h = abs(sy2 - sy1)

        attrs = {
            "x": round(left, 2),
            "y": round(top, 2),
            "width": round(w, 2),
            "height": round(h, 2),
            "fill": "none",
            "stroke": self._stroke_color(),
            "stroke-width": self.stroke_width,
        }
        dash = self._dash_attr(line_type)
        if dash:
            attrs["stroke-dasharray"] = dash
        if self._mode == MODE_XOR:
            attrs["data-mode"] = "xor"

        self._elements.append(_Element("rect", attrs))

    def draw_text(
        self,
        x: int, y: int,
        text: str,
        size: int,
        orient: int,
        font: str,
    ) -> None:
        sx, sy = self._xy(x, y)

        # Размер шрифта.
        font_size = max(6, size * FONT_SCALE)

        # Шрифт.
        family = FONT_FAMILY_RU if font == "RU" else FONT_FAMILY_EN

        # Ориентация: в SVG атрибут transform.
        # orient=0 — горизонтально, orient=1 — вертикально.
        transform = None
        if orient == 1:
            # Поворот на -90° вокруг точки вставки.
            transform = f"rotate(-90 {sx:.2f} {sy:.2f})"
        elif orient == 2:
            transform = f"rotate(180 {sx:.2f} {sy:.2f})"
        elif orient == 3:
            transform = f"rotate(90 {sx:.2f} {sy:.2f})"

        attrs = {
            "x": round(sx, 2),
            "y": round(sy, 2),
            "font-family": family,
            "font-size": font_size,
            "fill": self._stroke_color(),
            # Anchor="start" — точка (x, y) слева от базовой линии.
            "text-anchor": "start",
            # dominant-baseline="middle" ставит текст по центру высоты строки.
            "dominant-baseline": "middle",
        }
        if transform:
            attrs["transform"] = transform
        if self._mode == MODE_XOR:
            attrs["data-mode"] = "xor"

        # Экранированный текст как отдельный элемент.
        escaped = html.escape(text, quote=False)
        self._elements.append(_Element(
            "text", attrs, raw=None,
        ))
        # Заменяем raw на содержимое.
        self._elements[-1].raw = _render_text(attrs, escaped)

    def clear(self) -> None:
        """Очистить накопленные примитивы."""
        self._elements.clear()

    def present(self) -> None:
        """Для SVG present — no-op (рендер откладывается до save)."""
        pass

    # ─── Сохранение ──────────────────────────────────────────────────────────

    def save(
        self,
        path: str | Path = "output.svg",
        *,
        standalone: bool = True,
    ) -> Path:
        """
        Сохранить накопленные примитивы в SVG-файл.

        Параметры:
            path        — путь к файлу;
            standalone  — добавлять ли XML-заголовок и метаданные.

        Возвращает путь к сохранённому файлу.
        """
        path = Path(path)
        svg = self.to_svg(standalone=standalone)
        path.write_text(svg, encoding="utf-8")
        log.info(
            "SVG сохранён: %s (%d примитивов, %d байт)",
            path, len(self._elements), len(svg),
        )
        return path

    def to_svg(self, *, standalone: bool = True) -> str:
        """Собрать SVG-документ как строку."""
        lines: list[str] = []

        if standalone:
            lines.append('<?xml version="1.0" encoding="UTF-8"?>')

        # ─── Заголовок <svg> ────────────────────────────────────────────────
        lines.append(
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'width="{self.width}" height="{self.height}" '
            f'viewBox="0 0 {self.width} {self.height}">'
        )

        if self.metadata:
            lines.append("  <metadata>")
            lines.append(
                f"    <emulator>mc7401-emulator</emulator>"
            )
            lines.append(
                f"    <generated>{datetime.now().isoformat(timespec='seconds')}</generated>"
            )
            lines.append(f"  </metadata>")

        # ─── Фон ────────────────────────────────────────────────────────────
        if self.background and self.background.lower() != "none":
            lines.append(
                f'  <rect x="0" y="0" '
                f'width="{self.width}" height="{self.height}" '
                f'fill="{self.background}"/>'
            )

        # ─── Рамка ──────────────────────────────────────────────────────────
        if self.border:
            lines.append(
                f'  <rect x="0.5" y="0.5" '
                f'width="{self.width - 1}" height="{self.height - 1}" '
                f'fill="none" stroke="#888888" stroke-width="1"/>'
            )

        # ─── Примитивы ──────────────────────────────────────────────────────
        lines.append('  <g id="graphics">')
        for el in self._elements:
            lines.append("    " + el.to_svg())
        lines.append("  </g>")

        lines.append("</svg>")
        return "\n".join(lines)

    # ─── Отладка ─────────────────────────────────────────────────────────────

    def dump_stats(self) -> dict[str, int]:
        """Подсчитать примитивы по типам."""
        stats: dict[str, int] = {}
        for el in self._elements:
            stats[el.kind] = stats.get(el.kind, 0) + 1
        return stats

    def __repr__(self) -> str:
        return (
            f"SVGBackend({self.width}x{self.height}, "
            f"elements={len(self._elements)}, mode={self._mode})"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Утилиты
# ─────────────────────────────────────────────────────────────────────────────

def _render_text(attrs: dict, escaped: str) -> str:
    """Собрать SVG-элемент <text> с содержимым."""
    parts = ["<text"]
    for k, v in attrs.items():
        parts.append(f' {k}="{html.escape(str(v), quote=True)}"')
    parts.append(">")
    parts.append(escaped)
    parts.append("</text>")
    return "".join(parts)


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
        cmd(b"+") +
        cmd(b"9 2") +
        cmd(b";0 HELLO MC 7401")
    )

    state = TerminalState()
    backend = SVGBackend(state, border=True, metadata=True)
    emu = TerminalEmulator(state, backend)

    parser = CommandParser(CommandStreamReader(stream))
    for c in parser:
        emu.execute(c)

    print("Статистика backend'а:", backend.dump_stats())

    out = backend.save("test_svg_backend.svg")
    print(f"Файл сохранён: {out}")

    # Небольшая проверка: SVG должен содержать нужные элементы.
    text = out.read_text(encoding="utf-8")
    for token in ("<svg", "<line", "<circle", "<rect", "<text", "</svg>"):
        assert token in text, f"В SVG нет {token}"
    print("Все ожидаемые элементы присутствуют в SVG.")