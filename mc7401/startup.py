"""
startup.py — начальный экран терминала.

Пока данные от ЭВМ не пришли, показываем сводку: модель, разрешение,
время, состояние линии и подсказки по клавишам.  Экран перерисовывается
раз в секунду по часам, поэтому время на нём живое.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .constants import SCREEN_HEIGHT, SCREEN_WIDTH

MODEL = 'Электроника МС 7401'

TITLE_LINES = [
    '',
    f'  {MODEL}',
    f'  растровый графический терминал  {SCREEN_WIDTH}×{SCREEN_HEIGHT}',
    '',
]


WELCOME = ('Электроника МС 7401 — растровый графический терминал БЭСМ-6. '
           'Готов к работе: введите команду.')

DEFAULT_PICTURE = 'ПТИ'


def picture_path(name: str = DEFAULT_PICTURE):
    """Найти рисунок заставки.

    `name` — имя файла без расширения: ищется в `golden/render/` рядом с
    исходниками, затем в текущем каталоге и в каталоге пользователя.
    Пустое имя или `none` — заставка без картинки.
    """
    if not name or name.lower() in ('none', '-', 'нет'):
        return None
    stem = name
    for suf in ('.png', '.svg', '.PNG'):
        if stem.endswith(suf):
            stem = stem[:-len(suf)]
    roots = [Path(__file__).resolve().parent.parent, Path.cwd(),
             Path.home() / '.local' / 'share' / 'mc7401']
    for root in roots:
        for cand in (root / 'golden' / 'render' / (stem + '.png'),
                     root / 'render' / (stem + '.png'),
                     root / 'golden' / 'render' / (stem + '.svg'),
                     root / (stem + '.png')):
            if cand.exists():
                return cand
    return None


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
    body.append('  ' + WELCOME)
    body.append('')
    body.append(f'  {hint}')
    body.append('')
    body.append('  q — выход')
    return '\n'.join(l[:max(8, lines_width)] for l in body)


def hint_text() -> str:
    """Подсказка по клавишам терминала."""
    return ('L режим  S передать  e эхо  x очистить  z набор  p перо  '
            '↑↓←→ сдвиг  + - масштаб')


__all__ = ['MODEL', 'build', 'hint_text', 'TITLE_LINES',
           'WELCOME', 'picture_path', 'DEFAULT_PICTURE']