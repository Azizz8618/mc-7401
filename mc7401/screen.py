"""
screen.py — представление кадра МС 7401 в терминале хоста.

Модуль чистый: не трогает терминал, не читает ввод, не печатает.
Отвечает только за математику окна просмотра и за превращение
растрового кадра в текст ANSI. Это позволяет тестировать вывод,
не запуская интерактивную программу.

Раскладка пикселей: один знакомест терминала показывает **два**
вертикальных пикселя — верхний половинный блок `▀` и нижний `▄`.
Поэтому картинка 560×400 превращается в прямоугольник 560×200 знакоместов
при масштабе 1.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .framebuffer import Framebuffer

#: Знаки полублоков: верхний, нижний, оба-пусто, оба-залито.
BLOCK_UP = '▀'
BLOCK_DOWN = '▄'
BLOCK_EMPTY = ' '
BLOCK_FULL = '█'

#: Знак включённого пикселя для монохромного режима.
MONO_ON = '#'
MONO_OFF = '·'


@dataclass
class Viewport:
    """Окно просмотра кадра: масштаб и смещение верхнего левого угла."""

    scale: int = 2          # знакомест терминала на пиксель
    x: int = 0              # левый край видимой области, в пикселях кадра
    y: int = 0              # верхний край, в пикселях кадра (Y вверх)

    def clamp(self, fb: Framebuffer, cols: int, rows: int) -> None:
        """Удержать окно внутри кадра."""
        span_x = max(1, cols * self.scale)
        span_y = max(1, rows * self.scale * 2)
        self.x = max(0, min(self.x, max(0, fb.width - span_x)))
        # `y` отсчитывается сверху, поэтому максимум — верх кадра.
        self.y = max(0, min(self.y, max(0, fb.height - span_y)))

    def fit(self, fb: Framebuffer, cols: int, rows: int) -> None:
        """Подогнать масштаб и смещение так, чтобы кадр занял окно.

        Один знакомест закрывает `scale` пикселей по горизонтали и
        `2 * scale` по вертикали.  Кадр вписывается целиком, когда

            width  <= cols * scale  и  height <= rows * 2 * scale,

        то есть минимальный подходящий масштаб — это **наибольшее** из
        двух требований.  Раньше здесь стояло наименьшее, и кадр не
        влезал: в окне оставался его угол, а остальное — пустота.
        """
        by_x = -(-fb.width // max(1, cols))          # ceil
        by_y = -(-fb.height // max(1, rows * 2))
        self.scale = max(1, max(by_x, by_y))
        self.center(fb, cols, rows)

    def center(self, fb: Framebuffer, cols: int, rows: int) -> None:
        span_x = max(1, cols * self.scale)
        span_y = max(1, rows * self.scale * 2)
        self.x = max(0, (fb.width - span_x) // 2)
        self.y = max(0, (fb.height - span_y) // 2)


class ScreenRenderer:
    """Превращает кадр в текст для вывода в терминал.

    `mode` — `'color'` (цветные половинные блоки) или `'mono'`
    (один знак на знакомест, работает везде).
    """

    def __init__(self, fb: Framebuffer, *, mode: str = 'color',
                 vp: Optional[Viewport] = None) -> None:
        self.fb = fb
        self.mode = mode
        self.vp = vp or Viewport()

    # ── выборка пикселей ────────────────────────────────────────────────────

    def _on(self, px: int, py_top: int) -> bool:
        """Есть ли зажжённый пиксель в ячейке знакоместа.

        `px`, `py_top` — координаты **сверху вниз**, как их видит терминал;
        кадр использует `Y` вверх, поэтому `Y` отражается.

        Выборка «максимальная», а не усреднение: тонкая линия на
        уменьшенном масштабе остаётся видимой, тогда как при усреднении
        исчезает.
        """
        s = self.vp.scale
        for dy in range(s):
            row = py_top + dy                       # сверху вниз
            yy = self.fb.height - 1 - row           # Y кадра (вверх)
            if not 0 <= yy < self.fb.height:
                continue
            for dx in range(s):
                xx = px + dx
                if 0 <= xx < self.fb.width and self.fb.get(xx, yy):
                    return True
        return False

    # ── вывод ───────────────────────────────────────────────────────────────

    def render(self, cols: int, rows: int) -> str:
        """Собрать прямоугольник `cols`×`rows` знакоместов."""
        if self.mode == 'mono':
            return self._render_mono(cols, rows)
        return self._render_color(cols, rows)

    def _render_mono(self, cols: int, rows: int) -> str:
        vp = self.vp
        vp.clamp(self.fb, cols, rows)
        out: list[str] = []
        for row in range(rows):
            py_top = vp.y + row * vp.scale * 2      # Y вниз от верхнего края
            line: list[str] = []
            for col in range(cols):
                px = vp.x + col * vp.scale
                up = self._on(px, py_top)
                down = self._on(px, py_top + vp.scale)
                if up and down:
                    line.append(BLOCK_FULL if self.mode == 'color' else MONO_ON)
                elif up:
                    line.append(BLOCK_UP if self.mode == 'color' else MONO_ON)
                elif down:
                    line.append(BLOCK_DOWN if self.mode == 'color' else MONO_OFF)
                else:
                    line.append(BLOCK_EMPTY)
            out.append(''.join(line))
        return '\n'.join(out)

    def _render_color(self, cols: int, rows: int) -> str:
        """Цветной вывод: два пикселя через ANSI-цвет верхней/нижней части.

        Цвет задаётся escape-последовательностью: верхняя половина знака
        красится в цвет свечения, нижняя остаётся фоном. Один знак
        закрывает два пикселя.
        """
        vp = self.vp
        vp.clamp(self.fb, cols, rows)
        out: list[str] = []
        for row in range(rows):
            py_top = vp.y + row * vp.scale * 2
            parts: list[str] = []
            for col in range(cols):
                px = vp.x + col * vp.scale
                up = self._on(px, py_top)
                down = self._on(px, py_top + vp.scale)
                if not up and not down:
                    continue
                if up and not down:
                    parts.append(f'\x1b[38;2;{FG[0]};{FG[1]};{FG[2]}m▀'
                                 f'\x1b[0m')
                elif down and not up:
                    parts.append(f'\x1b[38;2;{FG[0]};{FG[1]};{FG[2]}m▄'
                                 f'\x1b[0m')
                else:
                    parts.append(f'\x1b[38;2;{FG[0]};{FG[1]};{FG[2]}m█'
                                 f'\x1b[0m')
            out.append(''.join(parts))
        return '\n'.join(out)

    # ── статистика для строки состояния ──────────────────────────────────────

    def status(self, cols: int, rows: int) -> str:
        """Строка сведений: масштаб, смещение, содержимое кадра."""
        bb = self.fb.bbox()            # координаты уже «сверху вниз»
        inside = 0
        if bb:
            x0, y0, x1, y1 = bb
            # Границы включительные: иначе фигура высотой в один пиксель
            # даёт нулевую площадь.
            ix0 = max(x0, self.vp.x)
            ix1 = min(x1 + 1, self.vp.x + cols * self.vp.scale)
            iy0 = max(y0, self.vp.y)
            iy1 = min(y1 + 1, self.vp.y + rows * self.vp.scale * 2)
            inside = max(0, ix1 - ix0) * max(0, iy1 - iy0)
        total = 1
        if bb:
            total = (bb[2] - bb[0] + 1) * (bb[3] - bb[1] + 1)
        pct = 100 * inside // total if bb else 0
        return (f'масштаб 1:{self.vp.scale}  смещение {self.vp.x},{self.vp.y}  '
                f'точек {self.fb.count_set()}  видно {pct}%  '
                f'{"цвет" if self.mode == "color" else "ч/б"}')


#: Цвет свечения пикселя (зелёный, как у реального индикатора).
FG = (0x33, 0xFF, 0x66)