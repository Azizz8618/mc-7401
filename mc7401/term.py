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

    Возвращает канонические формы, которые понимает `tui.handle_key`:
    обычный символ, `'\r'`/`'\n'` (ВК/ПС), `'KEY_BACKSPACE'`,
    `'KEY_UP'`/`'KEY_DOWN'`/`'KEY_LEFT'`/`'KEY_RIGHT'`/`'KEY_PAGEUP'`/
    `'KEY_PAGEDOWN'`/`'KEY_HOME'`/`'KEY_END'`/`'KEY_F1'`…`'KEY_F4'`,
    управляющие сочетания `'^C'`, последовательность мыши
    (`'\x1b[<…'` — световое перо), `'ESC'`, либо `None` по таймауту.
    """
    if IS_WINDOWS:
        return _read_key_windows(timeout)
    return _read_key_posix(timeout)


def _plain_name(ch: str) -> str:
    """Каноническое имя нажатия без escape-последовательности.

    `ВК`/`ПС` возвращаются как есть (`'\r'`/`'\n'`), Backspace — именем
    `'KEY_BACKSPACE'` (и `0x7F`, и `0x08` — на Windows это он же), остальные
    управляющие символы — каретной нотацией `'^X'`.  Раньше `0x08` давал
    `'^H'`, и `tui.py` принимал его за переключатель `Ctrl`, а не за
    удаление знака.
    """
    if ch in ('\r', '\n'):
        return ch
    if ch in ('\x08', '\x7f'):
        return 'KEY_BACKSPACE'
    if ord(ch) < 32:
        return '^' + chr(ord(ch) + 64)
    return ch


#: Служебные клавиши сеанса: имена → то же, что в `keyboard.py` 15ИЭ.
SESSION_KEYS = {
    'KEY_CLEAR': 'ОЧИСТКА',
    'KEY_ECHO': 'ЭХО',
    'KEY_CMDSET': 'НАБОР',
    'KEY_MODE': 'СЕТЬ',
    'KEY_SEND': 'ПЕРЕДАЧА',
}


#: Набранные байты, ещё не разобранные в нажатия.
#:
#: `sys.stdin.read(1)` делать нельзя: TextIO читает с опережением, и
#: `select` перестаёт видеть остаток escape-последовательности — стрелки
#: и функциональные клавиши обрывались посреди записи («клавиши не
#: работают»).  Байты читаются одной операцией (`read1`) и разбираются
#: из собственного буфера.
_KEY_BYTES = bytearray()


def push_input(data: bytes) -> None:
    """Вернуть неразобранные байты в буфер ввода.

    Нужно для служебных читателей (протокол графики): они читают вход
    напрямую, и нажатия, попавшие в окно чтения, иначе бы потерялись.
    """
    if data:
        _KEY_BYTES[:0] = data


def _fill_keys(timeout: Optional[float]) -> bool:
    """Дочитать байты ввода в буфер; False — данных нет (таймаут)."""
    import select
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if not ready:
        return False
    buf = getattr(sys.stdin, 'buffer', None)
    if buf is not None and hasattr(buf, 'read1'):
        chunk = buf.read1(256)            # одна операция, без read-ahead
    else:
        data = sys.stdin.read(1)          # подменённый поток (тесты)
        chunk = data.encode('utf-8') if data else b''
    if not chunk:
        return False
    _KEY_BYTES.extend(chunk)
    return True


def _take_char() -> Optional[str]:
    """Достать один символ из буфера (UTF-8 может быть многобайтовым)."""
    while _KEY_BYTES:
        b = _KEY_BYTES[0]
        if b < 0x80:
            del _KEY_BYTES[0]
            return chr(b)
        n = 2 if b < 0xE0 else 3 if b < 0xF0 else 4
        if len(_KEY_BYTES) < n:
            return None
        raw = bytes(_KEY_BYTES[:n])
        del _KEY_BYTES[:n]
        try:
            return raw.decode('utf-8')
        except UnicodeDecodeError:        # pragma: no cover
            return '?'
    return None


def _read_key_posix(timeout: Optional[float]) -> Optional[str]:   # pragma: no cover
    if not _KEY_BYTES and not _fill_keys(timeout):
        return None
    ch = _take_char()
    if ch is None:                        # оборванный многобайтовый символ
        if not _fill_keys(0.02):
            return None
        ch = _take_char()
        if ch is None:
            return None
    if ch != '\x1b':
        return _plain_name(ch)
    # Последовательность ESC: добираем из буфера — остаток мог прийти
    # тем же чтением, и повторный `select` его уже не видит.
    if not _KEY_BYTES and not _fill_keys(0.02):
        return 'ESC'
    code = _take_char()
    if code not in ('[', 'O'):
        return 'ESC'
    # `seq` — полная последовательность: `OP` (F1), `[A` (↑), `[15~` (F5).
    # Раньше префикс `O`/`[` съедался, и F1…F4 приходили как `P`…`S`.
    seq = code
    mouse = False
    while True:
        if not _KEY_BYTES and not _fill_keys(0.02):
            break
        c = _take_char()
        if c is None:
            break
        seq += c
        if mouse:
            if c in 'Mm':                # конец SGR-мыши
                break
            continue
        if c == '<' and code == '[' and seq == '[<':
            # SGR-мышь `\x1b[<b;x;yM|m` — возвращается целиком: это
            # световое перо (см. `pen.py`), разбирать её как нажатие нельзя.
            mouse = True
            continue
        if c.isalpha() or c == '~':
            break
    # Мышь возвращается целиком, с `ESC`; остальное — имя клавиши.
    return ('\x1b' + seq) if mouse else _arrow_name(seq)


#: Служебные клавиши 15ВВВ-97-006 — те же, что на 15ИЭ.
#: Буквы под команды не занимаются: на реальной клавиатуре это
#: отдельные клавиши, иначе нельзя набрать текст (см. JOURNAL.md).
KEY_CLEAR = 'KEY_CLEAR'       # F5  — ОЧИСТКА
KEY_ECHO = 'KEY_ECHO'         # F6  — ЭХО
KEY_CMDSET = 'KEY_CMDSET'     # F8  — НАБОР
KEY_MODE = 'KEY_MODE'         # F9  — СЕТЬ: АВТОНОМНО ↔ С ЭВМ
KEY_SEND = 'KEY_SEND'         # F10 — ПЕРЕДАЧА

#: Номера функциональных клавиш ПК → служебные клавиши 15ВВВ-97-006.
#: Ключ — число из последовательности `ESC [ <число> ~`.
SEQ_FUNCTIONAL = {
    '15': KEY_CLEAR,          # F5 — ОЧИСТКА
    '17': KEY_ECHO,           # F6 — ЭХО
    '18': 'KEY_BLINK',        # F7 — УПР.СИМВ (не используется)
    '19': KEY_CMDSET,         # F8 — НАБОР
    '20': KEY_MODE,           # F9 — СЕТЬ
    '21': KEY_SEND,           # F10 — ПЕРЕДАЧА
}

#: Остальные числовые последовательности `ESC [ <число> ~`.
SEQ_SPECIAL = {
    '1': 'KEY_HOME', '2': 'KEY_INSERT', '3': 'KEY_DELETE',
    '4': 'KEY_END', '5': 'KEY_PAGEUP', '6': 'KEY_PAGEDOWN',
    '7': 'KEY_HOME', '8': 'KEY_END',
}


def _arrow_name(seq: str) -> str:
    """Имя клавиши по escape-последовательности (без `ESC`).

    Последовательность приходит с префиксом `ESC [` (`'[A'` — ↑) или
    `ESC O` (`'OP'` — F1), но принимается и без него (`'15~'`).
    """
    # F1…F4 — `ESC O P`…`ESC O S`: полная последовательность, с префиксом.
    if seq in ('OP', 'OQ', 'OR', 'OS'):
        return {'OP': 'KEY_F1', 'OQ': 'KEY_F2', 'OR': 'KEY_F3',
                'OS': 'KEY_F4'}[seq]
    if seq[:1] in ('[', 'O'):
        seq = seq[1:]
    table = {'A': 'KEY_UP', 'B': 'KEY_DOWN', 'C': 'KEY_RIGHT',
             'D': 'KEY_LEFT', 'H': 'KEY_HOME', 'F': 'KEY_END',
             'Z': 'KEY_BTAB'}
    if seq in table:
        return table[seq]
    if seq.endswith('~') and seq[:-1].isdigit():
        num = seq[:-1]
        if num in SEQ_FUNCTIONAL:
            return SEQ_FUNCTIONAL[num]
        return SEQ_SPECIAL.get(num, 'ESC')
    # Модификаторы: `1;5C` — Ctrl+→.
    if ';' in seq and len(seq) > 2:
        kind = seq[-1]
        base = {'A': 'KEY_UP', 'B': 'KEY_DOWN', 'C': 'KEY_RIGHT',
                'D': 'KEY_LEFT'}.get(kind)
        if base:
            return 'KEY_CTRL' + base[3:]
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
                    ';': 'KEY_F1', '<': 'KEY_F2', '=': 'KEY_F3',
                    '>': 'KEY_F4',
                }.get(code, 'ESC')
            # Backspace на Windows даёт `0x08` — это удаление знака,
            # а не каретная нотация `'^H'` (см. `_plain_name`).
            return _plain_name(ch)
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