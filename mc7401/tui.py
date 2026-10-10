"""
tui.py — интерактивный фронтенд эмулятора МС 7401.

Пользователь работает с терминалом в диалоговом режиме: видит кадр,
масштабирует и двигает его, и вводит команды с клавиатуры — те же,
что ждёт программа на стороне ЭВМ (`СЕТ `, `ВЫХ `, произвольный текст
с `ВК`).

Источник данных — драйвер:

* :class:`FileDriver` — поток команд из файла (то, что воспроизводит
  `tools/gen_golden.py` из скомпилированной Паскаль-программы);
* :class:`LiveDriver` — линия ДКС БЭСМ-6 (`mc7401.link`);
* :class:`ScriptedDriver` — эталонный поток плюс сценарий нажатий,
  для показа работы без ЭВМ.

    python3 -m mc7401 --tui golden/streams/ГРА.bin
    python3 -m mc7401 --tui --link 127.0.0.1:4202
    python3 -m mc7401 --tui --demo
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from .emulator import TerminalEmulator
from .pen import LightPen
from .reader import CommandStreamReader, EventKind
from .screen import ScreenRenderer, Viewport
from .term import (ALT_SCREEN_OFF, ALT_SCREEN_ON, BOLD, CLEAR, HIDE_CURSOR,
                   HOME, RESET, SHOW_CURSOR, RawTerminal, bar,
                   read_key, terminal_size)

log = logging.getLogger('mc7401.tui')

HELP = [
    ('↑ ↓ ← →', 'сдвинуть кадр'),
    ('+  -', 'приблизить / отдалить'),
    ('0', 'вписать кадр в окно'),
    ('c', 'центрировать'),
    ('m', 'цветной / чёрно-белый вывод'),
    ('g', 'сетка поверх картинки'),
    ('r', 'перерисовать'),
    ('x', 'очистить экран (команда S)'),
    ('ввести текст', 'команда на ЭВМ; `ВК` (Enter) — конец строки'),
    ('мышь', 'световое перо: нажатие и протяжка рисуют линию'),
    ('q или Ctrl-C', 'выход'),
]

#: Сценарий нажатий для --demo: меню `graf.pas` рисует картины по
#: очереди, текст уходит в программу, ответ приходит в поток.
DEMO_KEYS = 'ЛИН 3 ВЫХ '


# ── драйверы ────────────────────────────────────────────────────────────────

class Driver:
    """Источник данных терминала и приёмник нажатий."""

    name = 'драйвер'

    def poll(self, timeout: float) -> bytes:
        raise NotImplementedError

    def send_keys(self, text: str) -> None:
        pass

    def close(self) -> None:
        pass


class FileDriver(Driver):
    """Поток команд из файла; нажатия никуда не уходят."""

    name = 'файл'

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = path.read_bytes()
        self.pos = 0

    def poll(self, timeout: float) -> bytes:
        # Отдаём порциями, чтобы кадры появлялись по мере проигрывания.
        if self.pos >= len(self.data):
            time.sleep(min(timeout, 0.05))
            return b''
        chunk = self.data[self.pos:self.pos + 512]
        self.pos += len(chunk)
        return chunk

    def send_keys(self, text: str) -> None:
        log.info('клавиатура: %r (поток из файла, отправка не требуется)',
                 text)


class LiveDriver(Driver):
    """Линия ДКС БЭСМ-6."""

    name = 'ДКС'

    def __init__(self, endpoint: str) -> None:
        from .link import DKSLink
        self.link = DKSLink(*_split(endpoint))
        self.link.connect()

    def poll(self, timeout: float) -> bytes:
        try:
            return self.link.recv()
        except (TimeoutError, OSError):
            return b''

    def send_keys(self, text: str) -> None:
        self.link.send(text.encode('koi8-r', errors='replace'))

    def close(self) -> None:
        self.link.close()


class ScriptedDriver(Driver):
    """Эталонный поток плюс сценарий нажатий — для показа без ЭВМ."""

    name = 'демонстрация'

    def __init__(self, path: Path, keys: str = DEMO_KEYS,
                 delay: float = 0.4) -> None:
        self.inner = FileDriver(path)
        self.keys = keys
        self.delay = delay
        self._next = 0.0

    def poll(self, timeout: float) -> bytes:
        now = time.monotonic()
        if self._next == 0.0:
            self._next = now + self.delay
        elif self._next <= now and self._next < now + 10:
            self._next = now + self.delay
            if self._next <= now:
                self._next = now + self.delay
            self.inner.send_keys(self.keys[:1] if self.keys else '')
            self.keys = self.keys[1:]
        return self.inner.poll(timeout)


def _split(endpoint: str):
    from .link import split_endpoint
    return split_endpoint(endpoint)


# ── приложение ──────────────────────────────────────────────────────────────

class TerminalApp:
    """Диалоговый режим: кадр, масштаб, ввод."""

    def __init__(self, driver: Driver, *, mode: str = 'color',
                 grid: bool = False, intro: str = '') -> None:
        self.driver = driver
        self.emu = TerminalEmulator()
        self.reader = CommandStreamReader()
        #: Световое перо: мышь терминала хоста.
        self.pen = LightPen(self.emu.fb)
        self.renderer = ScreenRenderer(self.emu.fb, mode=mode)
        self.grid = grid
        self.intro = intro
        self.status_extra = ''
        self.input_line = ''
        self.message = ''
        self.running = True
        self._grid_fb = None

    # ── приём данных ────────────────────────────────────────────────────────

    def feed(self, data: bytes) -> None:
        if not data:
            return
        self.reader.feed(data)
        for ev in self.reader.pop():
            if ev.kind is EventKind.COMMAND:
                self.emu.execute(self.emu.parser.parse(ev.data))
            else:
                self.emu.handle_event(ev)

    # ── рисование ───────────────────────────────────────────────────────────

    def layout(self) -> tuple[int, int]:
        """Столбцы и строки под изображение: одна строка уходит на панель."""
        cols, rows = terminal_size()
        return cols, max(3, rows - 3)

    def draw(self) -> None:
        cols, rows = self.layout()
        self.renderer.vp.clamp(self.emu.fb, cols, rows)
        if not self.renderer.vp.scale:
            return
        if self.pen.enabled:
            self.pen.stroke(self.emu.fb)
        body = self.renderer.render(cols, rows)
        if self.grid:
            body = self._overlay_grid(body, cols, rows)
        if self.pen.enabled:
            body = self._overlay_pen(body, cols, rows)
        out = [HOME, body, '\n']
        out.append(bar(f' {self._title()}', cols) + '\n')
        out.append(bar(self._hint(), cols) + '\n')
        out.append(bar(self._input_row(), cols))
        sys.stdout.write(''.join(out))
        sys.stdout.flush()

    def _title(self) -> str:
        st = self.renderer.status(self.layout()[0], self.layout()[1])
        parts = [f'МС 7401 — {self.driver.name}', st, self.status_extra]
        return ' | '.join(p for p in parts if p)

    def _hint(self) -> str:
        if self.message:
            return self.message
        return ('↑↓←→ сдвиг  + - масштаб  0 вписать  c центр  m цвет  '
                'g сетка  p перо  x очистить  q выход')

    def _input_row(self) -> str:
        return f' ВВОД: {self.input_line}▏'

    def _overlay_pen(self, body: str, cols: int, rows: int) -> str:
        """Показать положение пера обратным видео — как курсор на экране."""
        lines = body.split('\n')
        vp = self.renderer.vp
        col = (self.pen.x - vp.x) // vp.scale
        row = (self.pen.y - vp.y) // (vp.scale * 2)
        if 0 <= row < len(lines) and 0 <= col < cols:
            ch = lines[row][col]
            if ch not in ('', ' '):
                lines[row] = (lines[row][:col] + '\x1b[7m'
                              + ch + '\x1b[0m' + lines[row][col + 1:])
        return '\n'.join(lines)

    def _overlay_grid(self, body: str, cols: int, rows: int) -> str:
        """Тонкая сетка по границам знакоместов — для привязки к координатам."""
        lines = body.split('\n')
        vp = self.renderer.vp
        for r, line in enumerate(lines):
            buf = list(line)
            for c in range(0, min(cols, len(buf)), vp.scale * 8):
                if buf[c] != ' ':
                    buf[c] = '·'
            lines[r] = ''.join(buf)
        return '\n'.join(lines)

    # ── ввод ────────────────────────────────────────────────────────────────

    def handle_key(self, key: str) -> None:
        vp = self.renderer.vp
        cols, rows = self.layout()
        step = max(8, vp.scale * 8)

        if key in ('^C', 'q', 'Q', 'EOF'):
            self.running = False
            return
        if key == 'KEY_UP':
            vp.y = min(self.emu.fb.height, vp.y + step)
        elif key == 'KEY_DOWN':
            vp.y = max(0, vp.y - step)
        elif key == 'KEY_LEFT':
            vp.x = max(0, vp.x - step)
        elif key == 'KEY_RIGHT':
            vp.x = min(self.emu.fb.width, vp.x + step)
        elif key == 'KEY_PAGEUP':
            vp.scale = min(16, vp.scale + 1)
        elif key == 'KEY_PAGEDOWN':
            vp.scale = max(1, vp.scale - 1)
        elif key == '+':
            vp.scale = max(1, vp.scale - 1)
        elif key == '-':
            vp.scale = min(16, vp.scale + 1)
        elif key == '0':
            vp.fit(self.emu.fb, cols, rows)
        elif key == 'c':
            vp.center(self.emu.fb, cols, rows)
        elif key == 'm':
            self.renderer.mode = ('mono' if self.renderer.mode == 'color'
                                  else 'color')
        elif key == 'g':
            self.grid = not self.grid
        elif key == 'p':
            self.pen.enabled = not self.pen.enabled
            self.pen._last = None
            self.message = ('перо включено' if self.pen.enabled
                            else 'перо выключено')
            sys.stdout.write((LightPen.disable_sequence() if not self.pen.enabled
                              else LightPen.enable_sequence()))
        elif key and key.startswith('\x1b[<') and self.pen.enabled:
            self.pen.feed_mouse(key, cols=cols, rows=rows,
                                scale=vp.scale, vx=vp.x, vy=vp.y)
            for ev in self.pen.take_events():
                self.driver.send_keys(ev.encode().decode('ascii'))
        elif key == 'r':
            self.message = 'перерисовано'
        elif key == 'x':
            self.emu.execute(self.emu.parser.parse(b'S\x55'))
            self.message = 'экран очищен'
        elif key == 'KEY_BACKSPACE' or key == '\x7f':
            self.input_line = self.input_line[:-1]
        elif key == '\r' or key == '\n' or key == 'KEY_ENTER':
            self._submit()
        elif key == 'ESC':
            self.input_line = ''
            self.message = 'ввод сброшен'
        elif key and len(key) == 1 and key.isprintable():
            self.input_line += key
        else:
            return
        vp.clamp(self.emu.fb, cols, rows)

    def _submit(self) -> None:
        text = self.input_line
        self.input_line = ''
        if not text:
            return
        # Программа ждёт ВК в конце строки.
        self.driver.send_keys(text + '\n')
        self.message = f'отправлено: {text!r}'

    # ── главный цикл ────────────────────────────────────────────────────────

    def run(self) -> int:
        cols, rows = self.layout()
        self.renderer.vp.fit(self.emu.fb, cols, rows)
        sys.stdout.write(ALT_SCREEN_ON + CLEAR + HIDE_CURSOR
                         + LightPen.enable_sequence())
        try:
            with RawTerminal():
                while self.running:
                    self.draw()
                    data = self.driver.poll(0.05)
                    if data:
                        self.feed(data)
                        continue
                    key = read_key(timeout=0.08)
                    if key:
                        self.handle_key(key)
        except KeyboardInterrupt:
            pass
        finally:
            sys.stdout.write(LightPen.disable_sequence() + SHOW_CURSOR
                             + ALT_SCREEN_OFF + RESET + '\n')
            sys.stdout.flush()
            self.driver.close()
        return 0


# ── запуск из командной строки ──────────────────────────────────────────────

def _default_stream() -> Optional[Path]:
    here = Path(__file__).resolve().parent.parent
    for name in ('ГРА', 'СЕТ', 'ЧАС'):
        p = here / 'golden' / 'streams' / f'{name}.bin'
        if p.exists():
            return p
    return None


def run_tui(path: Optional[Path] = None, *, endpoint: Optional[str] = None,
            demo: bool = False, mode: str = 'color',
            grid: bool = False, pen: bool = False) -> int:
    if endpoint:
        driver: Driver = LiveDriver(endpoint)
    elif demo or path is None:
        p = path or _default_stream()
        if p is None:
            print('нет эталонных потоков; укажите файл или --link',
                  file=sys.stderr)
            return 2
        driver = ScriptedDriver(p)
    else:
        driver = FileDriver(path)

    app = TerminalApp(driver, mode=mode, grid=grid)
    if pen:
        app.pen.enabled = True
    if demo:
        app.status_extra = 'сценарий нажатий: ЛИН 3 ВЫХ'
    return app.run()


__all__ = ['TerminalApp', 'Driver', 'FileDriver', 'LiveDriver',
           'ScriptedDriver', 'run_tui', 'HELP']