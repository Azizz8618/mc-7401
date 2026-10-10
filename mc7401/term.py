"""
term.py — работа с терминалом хоста: размер окна, «сырой» режим, ввод.

Только то, что нужно фронтенду МС 7401, без curses: curses есть не везде
(на Windows его приносят отдельно), а нужны лишь четыре вещи —
узнать размер окна, отключить отображение ввода, читать нажатия и
восстановить терминал при выходе.
"""

from __future__ import annotations

import os
import shutil
import sys
from typing import Callable, Optional

IS_WINDOWS = os.name == 'nt'

#: Коды ANSI для оформления интерфейса.
CLEAR = '\x1b[2J'
HOME = '\x1b[H'
ERASE_EOL = '\x1b[K'


def at(row: int, col: int = 1) -> str:
    """Перевести курсор в строку `row` (с единицы) и столбец `col`.

    Кадр выводится построчным позиционированием, а не переводами строки:
    перевод строки в последней строке окна прокручивает экран, и следующий
    кадр ложится со сдвигом — на экране остаются куски прежнего.  Здесь
    каждый ряд ставится на своё место, поэтому прокрутки нет вовсе.
    """
    return f'\x1b[{row};{col}H'
HIDE_CURSOR = '\x1b[?25l'
SHOW_CURSOR = '\x1b[?25h'
ALT_SCREEN_ON = '\x1b[?1049h'
ALT_SCREEN_OFF = '\x1b[?1049l'
REVERSE = '\x1b[7m'
BOLD = '\x1b[1m'
RESET = '\x1b[0m'


def terminal_size(default=(80, 24)) -> tuple[int, int]:
    """Размер терминала в знакоместах: (столбцы, строки)."""
    try:
        size = shutil.get_terminal_size(fallback=default)
        return max(20, size.columns), max(8, size.lines)
    except Exception:                              # pragma: no cover
        return default


class RawTerminal:
    """«Сырой» режим терминала с гарантированным восстановлением.

    Используется как контекстный менеджер:

        with RawTerminal():
            ...
    """

    def __init__(self, stream=None) -> None:
        self.stream = stream or sys.stdin
        self._saved = None
        self.active = False

    def __enter__(self) -> 'RawTerminal':
        self.enter()
        return self

    def __exit__(self, *exc) -> None:
        self.leave()

    def enter(self) -> None:
        if self.active or IS_WINDOWS:
            self.active = True
            return
        try:                                        # POSIX
            import termios
            import tty
            self._saved = termios.tcgetattr(self.stream.fileno())
            tty.setcbreak(self.stream.fileno())
            self.active = True
        except Exception:                           # pragma: no cover
            self.active = True                      # работаем и без сырого режима

    def leave(self) -> None:
        if not self.active:
            return
        self.active = False
        if IS_WINDOWS or self._saved is None:
            return
        try:                                        # POSIX
            import termios
            termios.tcsetattr(self.stream.fileno(),
                              termios.TCSADRAIN, self._saved)
        except Exception:                           # pragma: no cover
            pass


def read_key(timeout: Optional[float] = None) -> Optional[str]:
    """Прочитать одно нажатие.

    Возвращает: обычный символ, `'KEY_UP'`/`'KEY_DOWN'`/`'KEY_LEFT'`/
    `'KEY_RIGHT'`/`'KEY_PAGEUP'`/`'KEY_PAGEDOWN'`/`'KEY_HOME'`/`'KEY_END'`,
    управляющие сочетания `'^C'`, `'ESC'`, либо `None` по таймауту.
    """
    if IS_WINDOWS:
        return _read_key_windows(timeout)
    return _read_key_posix(timeout)


def _read_key_posix(timeout: Optional[float]) -> Optional[str]:   # pragma: no cover
    import select
    if timeout is not None:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return None
    ch = sys.stdin.read(1)
    if not ch:
        return 'EOF'
    if ch != '\x1b':
        if ord(ch) < 32:
            return '^' + chr(ord(ch) + 64)
        return ch
    # Последовательность ESC: читаем следующие байты, если они уже есть.
    import select as _s
    nxt = _s.select([sys.stdin], [], [], 0.02)[0]
    if not nxt:
        return 'ESC'
    code = sys.stdin.read(1)
    if code != '[' and code != 'O':
        return 'ESC'
    seq = ''
    while True:
        if not _s.select([sys.stdin], [], [], 0.02)[0]:
            break
        c = sys.stdin.read(1)
        seq += c
        if c.isalpha() or c == '~':
            break
    return _arrow_name(seq)


def _arrow_name(seq: str) -> str:
    table = {'A': 'KEY_UP', 'B': 'KEY_DOWN', 'C': 'KEY_RIGHT',
             'D': 'KEY_LEFT', 'H': 'KEY_HOME', 'F': 'KEY_END'}
    if seq in table:
        return table[seq]
    if seq.startswith('5') and seq.endswith('~'):
        return 'KEY_PAGEUP'
    if seq.startswith('6') and seq.endswith('~'):
        return 'KEY_PAGEDOWN'
    if seq.startswith('1') and seq.endswith('~'):
        return 'KEY_HOME'
    if seq.startswith('4') and seq.endswith('~'):
        return 'KEY_END'
    return 'ESC'


def _read_key_windows(timeout: Optional[float]) -> Optional[str]:  # pragma: no cover
    import msvcrt
    import time
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        if msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch in ('\x00', '\xe0'):             # префикс функциональных
                code = msvcrt.getwch()
                return {
                    'H': 'KEY_UP', 'P': 'KEY_DOWN', 'M': 'KEY_RIGHT',
                    'K': 'KEY_LEFT', 'I': 'KEY_PAGEUP', 'Q': 'KEY_PAGEDOWN',
                    'G': 'KEY_HOME', 'O': 'KEY_END',
                }.get(code, 'ESC')
            if ord(ch) < 32:
                return '^' + chr(ord(ch) + 64)
            return ch
        if deadline is not None and time.monotonic() > deadline:
            return None
        time.sleep(0.01)


def paint(lines: list[str], *, reverse_last: bool = False) -> str:
    """Собрать кадр экрана: содержимое, затем строка состояния."""
    out = [HOME]
    for line in lines:
        out.append(line.replace('\x1b', '') if reverse_last else line)
    return ''.join(out)


def bar(text: str, width: int) -> str:
    """Строка состояния во всю ширину окна, с обрезкой."""
    clean = text.replace('\x1b', '')
    if len(clean) > width:
        text = text[:max(0, width - 3)] + '...'
        clean = clean[:max(0, width - 3)] + '...'
    pad = max(0, width - len(clean))
    return f'{REVERSE}{text}{" " * pad}{RESET}'