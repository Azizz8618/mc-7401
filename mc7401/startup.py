"""
startup.py — начальный экран терминала.

Пока данные от ЭВМ не пришли, показываем сводку: модель, разрешение,
время, состояние линии и подсказки по клавишам.  Экран перерисовывается
раз в секунду по часам, поэтому время на нём живое.
"""

from __future__ import annotations

from datetime import datetime

from .constants import SCREEN_HEIGHT, SCREEN_WIDTH

MODEL = 'Электроника МС 7401'

TITLE_LINES = [
    '',
    f'  {MODEL}',
    f'  растровый графический терминал  {SCREEN_WIDTH}×{SCREEN_HEIGHT}',
    '',
]


def build(lines_width: int, *, state: str, summary: str,
          hint: str, clock: datetime | None = None) -> str:
    """Собрать начальный экран.

    `state` — строка состояния линии («соединение…», «ДКС 127.0.0.1:4202»),
    `summary` — сводка настроек, `hint` — подсказка по клавишам.
    """
    now = (clock or datetime.now()).strftime('%d.%m.%Y  %H:%M:%S')
    body = list(TITLE_LINES)
    body.append(f'  {now}')
    body.append('')
    body.append(f'  {state}')
    body.append('')
    for part in summary.split(', '):
        body.append(f'  · {part}')
    body.append('')
    body.append('  Ожидание данных от ЭВМ…')
    body.append('')
    body.append(f'  {hint}')
    body.append('')
    body.append('  q — выход')
    return '\n'.join(l[:max(8, lines_width)] for l in body)


def hint_text() -> str:
    """Подсказка по клавишам терминала."""
    return ('L режим  S передать  e эхо  x очистить  z набор  p перо  '
            '↑↓←→ сдвиг  + - масштаб')


__all__ = ['MODEL', 'build', 'hint_text', 'TITLE_LINES']