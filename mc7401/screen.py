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

        Кадр встаёт в **верхний левый угол** окна (`x = y = 0`), а не по
        центру: так же ведёт себя настоящий 15ИЭ-00-013, у которого кадр
        ЭВМ начинается с левого верхнего угла окна и растёт вниз
        (см. `ie15/README.md`: «кадр ЭВМ начинается с четвёртого ряда
        окна»).  Центрирование осталось отдельной командой — `Ctrl-c`.
        """
        by_x = -(-fb.width // max(1, cols))          # ceil
        by_y = -(-fb.height // max(1, rows * 2))
        self.scale = max(1, max(by_x, by_y))
        self.x = 0
        self.y = 0

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

    def _cell_runs(self, py_top: int) -> list[list[tuple[int, int]]]:
        """Серии зажжённых точек в двух половинах ячейки знакоместа.

        Выборка идёт **по сериям**, а не по точкам: обход `fb.get()` в
        каждой ячейке на кадре 560×400 стоил 20 мс, и перерисовка окна на
        каждое нажатие отставала.  Серии ряда берутся один раз и
        переиспользуются всеми ячейками ряда.

        Выборка «максимальная», а не усреднение: тонкая линия на
        уменьшенном масштабе остаётся видимой, тогда как при усреднении
        исчезает.
        """
        fb, s = self.fb, self.vp.scale
        top, bottom = [], []
        for dy in range(s):
            for runs, row in ((top, py_top + dy), (bottom, py_top + s + dy)):
                yy = fb.height - 1 - row             # Y вверх → строки вниз
                if 0 <= yy < fb.height:
                    runs.extend(fb.row_runs(yy))
        return top, bottom

    @staticmethod
    def _hit(runs: list[tuple[int, int]], px: int, span: int) -> bool:
        """Пересекает ли блок `px … px+span` хоть одна серия."""
        x1 = px + span
        for x0, x2 in runs:
            if x0 >= x1:
                return False
            if x2 > px:
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
            top, bottom = self._cell_runs(py_top)
            line: list[str] = []
            for col in range(cols):
                px = vp.x + col * vp.scale
                up = self._hit(top, px, vp.scale)
                down = self._hit(bottom, px, vp.scale)
                # Зажжён любой из двух пикселей ячейки — «включено»,
                # пустая — «выключено» (а не пробел: иначе горизонтальные
                # линии на нечётных рядах исчезали).  Раньше нижний пиксель
                # давал символ «выключено».
                line.append(MONO_ON if (up or down) else MONO_OFF)
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
            top, bottom = self._cell_runs(py_top)
            parts: list[str] = []
            for col in range(cols):
                px = vp.x + col * vp.scale
                up = self._hit(top, px, vp.scale)
                down = self._hit(bottom, px, vp.scale)
                if not up and not down:
                    # По знаку на ячейку: `continue` схлопывал колонки, и
                    # горизонтальные координаты кадра терялись.
                    parts.append(' ')
                elif up and not down:
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