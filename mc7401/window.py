"""
window.py — графическое окно терминала МС 7401.

Зачем: экран терминала 560×400 точек. Знакомест терминала хоста — это
сетка знаков, поэтому в консоли картинка и текст вынужденно оказываются
в разных областях и мельчают. В графическом окне рисуется **настоящий
кадр**: графика и текст вместе, один к одному, как на реальном
индикаторе МС7401.

Окно состоит из трёх частей:

* холст 560×400 — сам кадр терминала (графика и текст вместе);
* строка состояния — модель, время, режим, линия, набор знаков;
* поле ввода — то, что уходит на ЭВМ по линии ДКС.

Требуется tkinter (входит в поставку Python, в некоторых сборках нужно
доустановить пакет `python3-tk`).

    python3 -m mc7401 --gui
    python3 -m mc7401 --gui --link 127.0.0.1:4203
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from pathlib import Path

from . import startup
from .constants import SCREEN_HEIGHT, SCREEN_WIDTH
from .emulator import TerminalEmulator
from .framebuffer import Framebuffer
from .reader import CommandStreamReader, EventKind
from .session import Session

log = logging.getLogger('mc7401.window')

BG = '#08140c'
FG = '#33ff66'


def fb_to_ppm(fb: Framebuffer, path) -> None:
    """Кадр в PPM P6 (простейший формат, понимается Tk без библиотек)."""
    rows = []
    for y in range(fb.height - 1, -1, -1):      # Y вверх → строки сверху
        row = bytearray()
        for x in range(fb.width):
            on = fb.get(x, y)
            row += bytes(FG_ON if on else BG_ON)
        rows.append(bytes(row))
    data = (f'P6\n{fb.width} {fb.height}\n255\n').encode('ascii')
    Path(path).write_bytes(data + b''.join(rows))

#: Пиксели свечения и фона как байты RGB.
FG_ON = bytes((0x33, 0xFF, 0x66))
BG_ON = bytes((0x08, 0x14, 0x0C))


class TerminalWindow:
    """Окно терминала: кадр, состояние, ввод."""

    def __init__(self, driver=None, *, scale: int = 1, endpoint=None,
                 demo: bool = False) -> None:
        self.driver = driver
        self.scale = max(1, scale)
        self.endpoint = endpoint
        self.demo = demo
        self.emu = TerminalEmulator()
        self.reader = CommandStreamReader()
        self.session = Session()
        self.best, self.best_n = self.emu.fb.copy(), 0
        self.running = True
        self._tk = None

    # ── данные ──────────────────────────────────────────────────────────────

    def feed(self, data: bytes) -> None:
        """Принять данные: графику — в кадр, текст ОС — в поле ответа."""
        if not data:
            return
        if not _is_commands(data):
            self.session_text(data)
            return
        self.reader.feed(data)
        for ev in self.reader.pop():
            if ev.kind is EventKind.COMMAND:
                self.emu.execute(self.emu.parser.parse(ev.data))
                n = self.emu.fb.count_set()
                if n > self.best_n:               # кадр оператора
                    self.best, self.best_n = self.emu.fb.copy(), n
            else:
                self.emu.handle_event(ev)

    def session_text(self, data: bytes) -> None:
        """Текст, присланный ЭВМ по линии (приглашения ОС Диспак)."""
        self._last_text = getattr(self, '_last_text', '')
        self._last_text += data.decode('koi8-r', errors='replace')

    # ── окно ────────────────────────────────────────────────────────────────

    def build(self):
        import tkinter as tk
        root = tk.Tk()
        root.title(startup.MODEL)
        root.configure(bg=BG)
        self._tk = root
        w, h = SCREEN_WIDTH * self.scale, SCREEN_HEIGHT * self.scale

        top = tk.Frame(root, bg=BG)
        top.pack(side='top', fill='x')
        self.lbl_status = tk.Label(top, bg=BG, fg=FG, anchor='w',
                                   font=('monospace', 10))
        self.lbl_status.pack(side='left', fill='x', expand=True)
        self.lbl_mode = tk.Label(top, bg=BG, fg=FG, anchor='e',
                                 font=('monospace', 10))
        self.lbl_mode.pack(side='right')

        canvas = tk.Canvas(root, width=w, height=h, bg=BG,
                           highlightthickness=0)
        canvas.pack(side='top')
        self._photo = None
        self._canvas = canvas
        self._image_id = canvas.create_image(0, 0, anchor='nw')

        bottom = tk.Frame(root, bg=BG)
        bottom.pack(side='top', fill='x')
        tk.Label(bottom, text='ВВОД>', bg=BG, fg=FG,
                 font=('monospace', 11)).pack(side='left')
        self.entry = tk.Entry(bottom, bg=BG, fg=FG, insertbackground=FG,
                              relief='flat', font=('monospace', 11))
        self.entry.pack(side='left', fill='x', expand=True)
        self.entry.focus_set()
        self.entry.bind('<Return>', self._on_return)
        self.entry.bind('<Escape>', lambda e: self.entry.delete(0, 'end'))

        root.bind('<Key>', self._on_key)
        root.protocol('WM_DELETE_WINDOW', self.close)
        return root

    # ── ввод ────────────────────────────────────────────────────────────────

    def _on_return(self, _event=None) -> str:
        text = self.entry.get()
        self.entry.delete(0, 'end')
        if text and self.driver is not None:
            # Режим терминала: в HOST знак уходит сразу, в LOCAL копится
            # и уходит по ПЕРЕДАЧА (клавиша S).
            out = self.session.type_line(text)
            if not out:
                out = self.session.send()
            if out:
                self.driver.send_keys(out.decode('latin-1'))
        return 'break'

    def _on_key(self, event) -> None:
        ch = event.char
        key = event.keysym
        if key == 'Escape':
            self.close()
        elif key in ('l', 'L'):
            self.session.toggle_mode()
        elif key in ('e', 'E', 'F6'):
            self.session.toggle_echo()
        elif key in ('x', 'X', 'F5'):
            self.emu.execute(self.emu.parser.parse(b'S\x55'))
        elif ch and ch.isprintable():
            out = self.session.type_key(ch)
            if out and self.driver is not None:
                self.driver.send_keys(out.decode('latin-1'))

    def close(self) -> None:
        self.running = False
        if self._tk is not None:
            self._tk.quit()

    # ── отрисовка кадра ─────────────────────────────────────────────────────

    def paint(self) -> None:
        import tkinter as tk
        fb = self.best if self.best_n else self.emu.fb
        photo = tk.PhotoImage(master=self._tk, width=SCREEN_WIDTH,
                              height=SCREEN_HEIGHT)
        # Заливка фоном, затем точки — так быстрее, чем по одной.
        self._canvas.itemconfig(self._image_id, image='')
        photo.put(BG_ON * (SCREEN_WIDTH * SCREEN_HEIGHT),
                  to=(0, 0, SCREEN_WIDTH - 1, SCREEN_HEIGHT - 1))
        for y in range(SCREEN_HEIGHT):
            xs = [x for x in range(SCREEN_WIDTH) if fb.get(x, y)]
            if xs:
                photo.put(FG_ON * len(xs), to=(min(xs), SCREEN_HEIGHT - 1 - y,
                                              max(xs), SCREEN_HEIGHT - 1 - y))
        if self.scale > 1:
            photo = photo.subsample(self.scale)
        self._photo = photo                      # держим ссылку
        self._canvas.itemconfig(self._image_id, image=photo)
        now = time.strftime('%d.%m.%Y %H:%M:%S')
        self.lbl_status.configure(
            text=f' {startup.MODEL}  {SCREEN_WIDTH}×{SCREEN_HEIGHT}  {now}   '
                 f'точек {fb.count_set()}   {self.session.panel()}')
        self.lbl_mode.configure(
            text='РЕЖИМ=' + ('С ЭВМ' if self.session.mode == 'host'
                             else 'АВТОНОМНО'))

    # ── цикл ────────────────────────────────────────────────────────────────

    def run(self) -> int:
        try:
            root = self.build()
        except ImportError as exc:                 # pragma: no cover
            print('нужен tkinter: ' + str(exc), flush=True)
            return 2
        try:
            self.paint()
            root.after(50, self._tick)
            root.mainloop()
        finally:
            if self.driver is not None:
                self.driver.close()
        return 0

    def _tick(self) -> None:
        if not self.running:
            return
        if self.driver is not None:
            data = self.driver.poll(0.05)
            if data:
                self.feed(data)
        self.paint()
        self._tk.after(50, self._tick)


def _is_commands(data: bytes) -> bool:
    """Похоже ли это на поток команд терминала, а не на текст ОС."""
    from .constants import AR2, VK
    if bytes((AR2,)) + b'F' in data or b'/' + bytes((VK,)) in data:
        return True
    letters = b'J01245789]}>*3+KS\x1b/'
    for rec in data.split(bytes((VK,))):
        if rec[:1] in (bytes((c,)) for c in letters):
            return True
    return False


def run_window(*, endpoint: Optional[str] = None, scale: int = 1,
                demo: bool = False, config=None) -> int:
    """Открыть графическое окно терминала."""
    from .tui import BuiltinDriver, DEFAULT_ENDPOINT, FileDriver, LiveDriver
    from .tui import LinkRefused
    cfg = config or None
    if endpoint:
        try:
            driver = LiveDriver(endpoint)
        except LinkRefused as exc:
            print(str(exc))
            return 2
    elif demo:
        driver = BuiltinDriver()
    else:
        driver = None                          # без линии: только кадр
    win = TerminalWindow(driver, scale=scale, endpoint=endpoint)
    return win.run()


__all__ = ['TerminalWindow', 'run_window', 'fb_to_ppm']