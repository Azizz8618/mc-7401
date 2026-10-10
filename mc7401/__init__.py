"""
mc7401 — эмулятор растрового графического терминала «Электроника МС 7401».

Пакет реализует полный цикл работы с потоком команд терминала:

    поток байтов → CommandStreamReader → CommandParser
                 → TerminalEmulator → Framebuffer → backend (SVG/PNG/Tk/Pygame)

Быстрый старт:

    >>> from mc7401 import TerminalEmulator
    >>> fb = TerminalEmulator().run(b'J1U4 10 10U7 1 400 200U')
    >>> fb.count_set() > 0
    True
"""

from __future__ import annotations

from .chars import UserCharTable, USER_TABLE, dot_pattern, dots_matrix, glyph_matrix
from .constants import (
    AR2,
    LINE_PATTERNS,
    PEN_ERASE,
    PEN_OR,
    PEN_XOR,
    SCREEN_HEIGHT,
    SCREEN_WIDTH,
    S0,
    S1,
    VK,
)
from .emulator import TerminalEmulator, TerminalState, Window
from .framebuffer import Framebuffer
from .parser import Command, CommandParser, Op
from .reader import CommandStreamReader, Event, EventKind

__version__ = "1.0.0"

__all__ = [
    'AR2', 'S0', 'S1', 'VK',
    'SCREEN_WIDTH', 'SCREEN_HEIGHT',
    'LINE_PATTERNS', 'PEN_OR', 'PEN_XOR', 'PEN_ERASE',
    'Framebuffer', 'TerminalEmulator', 'TerminalState', 'Window',
    'CommandStreamReader', 'Event', 'EventKind',
    'CommandParser', 'Command', 'Op',
    'UserCharTable', 'USER_TABLE',
    'glyph_matrix', 'dot_pattern', 'dots_matrix',
    '__version__',
]