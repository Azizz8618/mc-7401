"""
window.py — графическое окно терминала МС 7401.

Зачем: экран терминала 560×400 точек — это **настоящий кадр**: графика и
текст вместе, один к одному, как на реальном индикаторе МС7401.

Окно состоит из двух частей:

* холст 560×400 — кадр терминала: графика и текст **на одном экране**;
* служебная панель — режим, набор знаков, линия, клавиши (Tk-лейблами).

Отдельного текстового режима и текстового поля под кадром нет.  Текст
из БЭСМ-6 (ответы ОС Диспак по линии ДКС) рисуется на кадре глифами
эталона СИМ (`mc7401.console`) в позиции курсора.  Ввод — набор прямо
на экране: стрелки или мышь ставят курсор в любой знакомёст, нажатые
знаки рисуются там же глифами СИМ и уходят на ЭВМ по линии ДКС.

Клавиши (те же функции, что и в текстовом режиме):

* стрелки — курсор по экрану; мышь — курсор в знакомёст;
* `ВК` (Backspace) — стереть знакомёст слева;
* `ПРПС` (Enter) — конец строки;
* F5 ОЧИСТКА, F6 ЭХО, F8 НАБОР, F9 СЕТЬ, F10 ПЕРЕДАЧА;
* Ctrl-q выход, Ctrl-+/- масштаб, Ctrl-c центр.

Режимы сеанса «АВТОНОМНО» / «С ЭВМ» сохранены (`session.py`): в
АВТОНОМНО набор копится в буфер и уходит по ПЕРЕДАЧА, в С ЭВМ каждый
знак немедленно уходит в линию.

Требуется tkinter (входит в поставку Python, в некоторых сборках нужно
доустановить пакет `python3-tk`).

    python3 -m mc7401 --gui
    python3 -m mc7401 --gui --link 127.0.0.1:4203
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from pathlib import Path

from . import chrome
from . import console as console_mod
from . import startup
from .constants import SCREEN_HEIGHT, SCREEN_WIDTH
from .console import CELL_H, CELL_W, COLS, RASTER_H, ROWS, Console
from .emulator import TerminalEmulator
from .framebuffer import Framebuffer
from .panel import Diagnostics, Status
from .pen import DEFAULT_TOOL, LightPen
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


@dataclass(frozen=True)
class Line:
    """Сведения о линии для строки состояния.

    Имя и канал знает окно само, а тип линии, ЭВМ и терминал приходят из
    баннера SIMH (`link.parse_greeting`): линия, объявленная «Connected
    to DKS», — это «ДКС», а не «КВУ», и номер терминала известен только
    оттуда.
    """
    line: str = 'НЕТ ЛИНИИ'
    channel: str = ''
    banner: bool = False
    kind: str = ''
    machine: int = 0
    terminal: int = 0
    wire: str = ''


class TerminalWindow:
    """Окно терминала: кадр, курсор, ввод, служебная панель."""

    def __init__(self, driver=None, *, scale: int = 1, endpoint=None,
                 demo: bool = False, config=None) -> None:
        self.driver = driver
        self.scale = max(1, scale)
        #: Масштаб, запрошенный конфигурацией (`[display] scale`): по
        #: умолчанию кадр занимает весь экран, но просьба «не мельче»
        #: уважается, если она в экран влезает.
        self._wanted = self.scale
        #: Развернуть окно на весь экран (`[display] fullscreen`).
        self.fullscreen = bool(getattr(config, 'fullscreen', True))
        self.endpoint = endpoint
        self.demo = demo
        self.config = config
        #: Символ конца строки на линии ДКС (настройка `[line] eol`).
        #: По умолчанию ПР (0o015): по эталону 15ИЭ окончание строки
        #: на ДКС — ПР, ETX для линии просто знак.
        self.eol = getattr(config, 'eol', 0o015)
        #: Размер знака текстового слоя — настройка `[terminal] glyph`.
        #: Применяется до создания консоли: от него зависят знакомест и
        #: сетка кадра, а по ним — и служебные строки окна.
        console_mod.set_glyph_scale(getattr(config, 'glyph',
                                            console_mod.GLYPH_SCALE))
        self.emu = TerminalEmulator()
        #: Текстовый слой на кадре: вывод БЭСМ-6 и эхо ввода глифами СИМ.
        self.console = Console(self.emu.fb)
        self.reader = CommandStreamReader()
        self.session = Session()
        if config is not None:
            # Настройки сеанса — как в `tui.py`: режим, набор знаков, эхо.
            self.session.mode = getattr(config, 'mode', self.session.mode)
            self.session.set_charset(
                getattr(config, 'charset', self.session.charset_set))
            self.session.echo = getattr(config, 'echo', self.session.echo)
        self.best, self.best_n = self.emu.fb.copy(), 0
        self.running = True
        self._tk = None
        #: Последняя выданная клавиша — строка «ПОСЛ:» панели.
        self.last_key = ''
        #: Световое перо (F2): мышь рисует по кадру, координаты уходят
        #: на ЭВМ.  На настоящем МС 7401 такого устройства нет, поэтому
        #: перо — возможность фронтенда (`pen.py`).
        self.pen = LightPen(self.emu.fb)
        self.pen.enabled = bool(getattr(config, 'pen', False))
        #: Инструмент пера — перебирается клавишей F3.
        self.pen_tool = DEFAULT_TOOL
        #: История выдачи: кадры, ушедшие по `F5 ОЧИСТКА`; колесо и
        #: PgUp/PgDn листают её (`_hist_at` — индекс, None = текущий кадр).
        self._history: list[Framebuffer] = []
        self._hist_at: Optional[int] = None
        #: Диагностика — журнал сообщений; последнее идёт последним рядом
        #: подвала и рисуется глифами ПЗУ, как и весь подвал.
        self.diag = Diagnostics()
        #: Показывать ряды-легенды подвала (`[terminal] help`).
        self.show_help = bool(getattr(config, 'help', True))
        #: Сетка поверх кадра (`Ctrl-g`) — для привязки к координатам.
        self.grid = bool(getattr(config, 'grid', False))
        #: Кадр или состояние изменились — окно пора перерисовать.
        self._dirty = True
        self._clock_at = 0.0

    # ── данные ──────────────────────────────────────────────────────────────

    def feed(self, data: bytes) -> None:
        """Принять данные: графику — в кадр, текст ОС — на кадр глифами."""
        if not data:
            return
        self._dirty = True
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
        """Текст, присланный ЭВМ по линии (приглашения ОС Диспак).

        Коды знаков ДКС показываются таблицей `chars.display_dks` и
        рисуются на кадре глифами эталона СИМ в позиции курсора.
        """
        try:
            text = data.decode('utf-8')
            if any(ord(c) > 0x7F for c in text):
                self.console.write_text(text.replace('\r', ''))
                self._touch_frame()
                return
        except UnicodeDecodeError:
            pass
        self.console.write(data)
        self._touch_frame()

    def _touch_frame(self) -> None:
        """Локальное изменение кадра (текст, эхо) — оно и есть текущее."""
        self.best, self.best_n = self.emu.fb.copy(), self.emu.fb.count_set()

    # ── окно ────────────────────────────────────────────────────────────────

    def build(self):
        import tkinter as tk
        root = tk.Tk()
        root.title(startup.MODEL)
        root.configure(bg=BG)
        self._tk = root
        # По умолчанию кадр занимает весь экран.  Разворачиваемся
        # **до** подбора масштаба: подбирать надо по фактическому размеру
        # окна, а не по экрану — у развёрнутого окна есть рамка и панель
        # задач, и кадр иначе уедет за нижнюю кромку.  `[display]
        # fullscreen = false` отключает.
        if self.fullscreen:
            self._maximize(root)
        root.update_idletasks()
        self._fit_scale()
        # Один холст из трёх полос: строка состояния, кадр 560×400 и
        # подвал.  Кадр **вне** служебных строк — рабочее поле не должно
        # наезжать на подвал, текст ОС виден целиком.  Всё нарисовано
        # глифами ПЗУ того же размера, что и текст в кадре.
        w = SCREEN_WIDTH * self.scale
        h = (SCREEN_HEIGHT + self.top_px() + self.footer_px()) * self.scale

        canvas = tk.Canvas(root, width=w, height=h, bg=BG,
                           highlightthickness=0)
        # Подвал привязан к нижней границе экрана: холст стоит снизу,
        # поле остаётся над строкой состояния.
        canvas.pack(side='bottom', anchor='s')
        self._photo = None
        self._canvas = canvas
        self._image_id = canvas.create_image(0, 0, anchor='nw')
        canvas.bind('<Button-1>', self._on_click)
        canvas.bind('<B1-Motion>', self._on_drag)
        canvas.bind('<ButtonRelease-1>', self._on_release)
        canvas.bind('<Button-2>', self._on_middle)
        canvas.bind('<Button-3>', self._on_right)
        canvas.bind('<B2-Motion>', self._on_drag)
        canvas.bind('<B3-Motion>', self._on_drag)
        canvas.bind('<ButtonRelease-2>', self._on_release)
        canvas.bind('<ButtonRelease-3>', self._on_release)
        canvas.bind('<MouseWheel>', self._on_wheel)
        canvas.bind('<Button-4>', lambda e: self._wheel_step(-1))
        canvas.bind('<Button-5>', lambda e: self._wheel_step(1))

        root.bind('<Key>', self._on_key)
        root.protocol('WM_DELETE_WINDOW', self.close)
        return root

    def _maximize(self, root) -> bool:
        """Развернуть окно на весь экран.

        `state('zoomed')` есть только в Tk для Windows и macOS: на X11
        он выдаёт `bad argument "zoomed"` (проверено на xfwm4).  Поэтому
        сначала пробуем его, а если оконный менеджер не умеет — встаём
        во весь экран через `-fullscreen`, а если и это нельзя —
        задаём геометрию по размеру экрана.
        """
        for state in ('zoomed',):
            try:
                root.state(state)
                root.update_idletasks()
                if root.state() == state:
                    return True
            except Exception:
                pass
        try:
            root.attributes('-fullscreen', True)
            root.update_idletasks()
            if root.attributes('-fullscreen'):
                return True
        except Exception:
            pass
        try:
            sw = int(root.winfo_screenwidth())
            sh = int(root.winfo_screenheight())
            root.geometry(f'{sw}x{sh}+0+0')
            return True
        except Exception:                              # pragma: no cover
            return False

    def chrome_parts(self):
        """Служебные строки: (над кадром, под кадром).

        Обе нарисованы глифами ПЗУ того же размера, что и знак в кадре,
        и лежат **вне** кадра 560×400 — как служебная строка и подвал
        у настоящего 15ИЭ.
        """
        st = self.status()
        return (chrome.status_rows(st),
                chrome.footer_rows(st, help_=self.show_help, diag=self.diag))

    def chrome_px(self) -> int:
        """Высота служебных строк в точках — вместе со строкой состояния."""
        top, bottom = self.chrome_parts()
        return chrome.chrome_height(top, bottom)

    def top_px(self) -> int:
        """Высота строки состояния у верхней кромки кадра, в точках."""
        top, _ = self.chrome_parts()
        return chrome.top_height(top)

    def footer_px(self) -> int:
        """Высота подвала у нижней кромки кадра, в точках."""
        _, bottom = self.chrome_parts()
        return len(bottom) * chrome.ROW_H

    def _fit_scale(self) -> None:
        """Вписать кадр вместе со служебными строками в окно.

        Масштаб **не растёт** сверх `[display] scale`: подгонка только
        уменьшает его, если кадр не влезает.  Иначе на экране 1920×1080
        кадр вписывается в полосу над подвалом во вдвое, и знак ПЗУ на
        экране становится 28×32 вместо затребованных 14×16.
        """
        w, h = self._avail_px()
        if w <= 1 or h <= 1:
            return
        full = SCREEN_HEIGHT + self.top_px() + self.footer_px()
        fits = max(1, min(w // SCREEN_WIDTH, h // max(1, full)))
        self.scale = min(self._wanted, fits)

    def _avail_px(self) -> tuple[int, int]:
        """Размер окна под кадр: само окно, а если оно ещё не показано —
        экран за вычетом рамки, заголовка и панели задач."""
        try:
            w = self._tk.winfo_width()
            h = self._tk.winfo_height()
        except Exception:                              # pragma: no cover
            return 0, 0
        if w > 1 and h > 1:                            # окно уже показано
            return int(w * 0.99), int(h * 0.99)
        try:                                           # окно ещё не создано
            return (int(self._tk.winfo_screenwidth() * 0.98),
                    int(self._tk.winfo_screenheight() * 0.94))
        except Exception:                              # pragma: no cover
            return 0, 0

    # ── ввод ────────────────────────────────────────────────────────────────

    def _on_return(self, _event=None) -> str:
        """`ПРПС` (Enter): конец строки.

        В режиме С ЭВМ набранные знаки уже ушли в линию по одному, на
        ЭВМ посылается символ конца строки.  В АВТОНОМНО накопленный
        буфер уходит вместе с концом строки (как по ПЕРЕДАЧА).
        """
        self.console.newline()
        self.last_key = 'ПРПС'
        if self.driver is not None:
            if self.session.mode == 'host':
                out = bytes((self.eol,))
            else:
                out = self.session.send(line_end=self.eol)
            if out:
                self.driver.send_keys(out.decode('latin-1'))
        self._touch_frame()
        self.paint()
        return 'break'

    def _on_backspace(self) -> str:
        """`ВК` (Backspace): стереть знакомёст слева от курсора.

        В С ЭВМ в линию уходит код `ВК` (0x08), чтобы знак стёрла и
        ЭВМ; в АВТОНОМНО стирается последний знак локального буфера.
        """
        self.console.backspace()
        self.last_key = 'ВК'
        if self.driver is not None:
            if self.session.mode == 'host':
                self.driver.send_keys('\x08')
            elif self.session.buffer:
                self.session.buffer.pop()
        self._touch_frame()
        self.paint()
        return 'break'

    def _on_escape(self, _event=None) -> str:
        """`Escape`: отменить набор — стереть от начала строки до курсора.

        Окно не закрывается: `Escape` относится к набору.
        """
        while self.console.col > 0:
            self.console.backspace()
        self.session.reset_buffer()
        self.last_key = 'ESC'
        self._touch_frame()
        self.paint()
        return 'break'

    # ── мышь ────────────────────────────────────────────────────────────────
    #
    # Поведение — как в терминале 15ИЭ-00-013 (README, раздел «Клавиши
    # терминала»): левая кнопка работает с кадром, колесо листает экран.
    # Для МС 7401 добавлено световое перо (F2): когда оно включено, левая
    # кнопка рисует, а правая стирает — иначе, левая ставит текстовый
    # курсор в знакомёст под указателем.

    #: Шаг колеса мыши — строк кадра за щелчок (как в 15ИЭ: `WHEEL_STEP`).
    WHEEL_STEP = 3

    def _pen_at(self, event) -> tuple[int, int]:
        """Координаты пера в системе кадра (Y вверх) по событию Tk."""
        x = max(0, min(self.emu.fb.width - 1, event.x // self.scale))
        return x, self.emu.fb.height - 1 - self._frame_row_img(event.y)

    def _frame_row_img(self, y: int) -> int:
        """Ряд кадра под указателем, **сверху вниз** (0 — верх кадра).

        В изображении сверху вниз идут строка состояния, кадр и подвал;
        указатель вне кадра прижимается к ближайшему его краю.
        """
        row = y // self.scale - (self.top_px() + self.footer_px())
        return max(0, min(row, SCREEN_HEIGHT - 1))

    def _on_click(self, event) -> None:
        """Левая кнопка: перо (если включено) либо курсор в знакомёст."""
        if self.pen.enabled:
            self._pen_down(event, button=1)
            return
        col = (event.x // self.scale) // CELL_W
        row = self._frame_row_img(event.y) // CELL_H
        if self._in_chrome(event.y):
            self.last_key = 'ПОДВАЛ'          # клик не по кадру
            self.paint()
            return
        self.console.goto(col, row)
        self.last_key = 'МЫШЬ'
        self.paint()

    def _in_chrome(self, y: int) -> bool:
        """Указатель попал в строку состояния или подвал — это не кадр."""
        off = self.top_px() + self.footer_px()
        y_img = y // self.scale
        return not (off <= y_img < off + SCREEN_HEIGHT)

    def _pen_down(self, event, *, button: int = 1) -> None:
        """Нажатие пера: начать штрих (правой кнопкой — стирание)."""
        x, y = self._pen_at(event)
        self.pen.x, self.pen.y = x, y
        self.pen.down = True
        self.pen.button = button
        self.pen._last = (x, y)
        if button == 1:
            self.pen.tool = self.pen_tool
        else:
            # Правая (и средняя) кнопка — стирающее перо, как `*` у
            # терминала: рисовать ей нечем, зато можно стереть.
            self.pen.tool = 'стирание'
        self.pen.dot(self.emu.fb)
        self.last_key = 'ПЕРО'
        self._touch_frame()
        self.paint()

    def _on_drag(self, event) -> None:
        """Протяжка с нажатой кнопкой — штрих пера (если перо включено)."""
        if not self.pen.enabled:
            # Без пера протяжка ведёт текстовый курсор — так удобнее
            # ставить курсор в дальний угол кадра.
            self._on_click(event)
            return
        x, y = self._pen_at(event)
        self.pen.line_to(self.emu.fb, x, y)
        self.last_key = 'ПЕРО'
        self._touch_frame()
        self.paint()

    def _on_release(self, _event=None) -> None:
        """Отпускание кнопки: перо заканчивает штрих."""
        if not self.pen.down:
            return
        self.pen.down = False
        self.pen._last = (self.pen.x, self.pen.y)
        self._touch_frame()
        self.paint()

    def _on_middle(self, event) -> None:
        """Средняя кнопка: стирающее перо (если перо включено)."""
        if self.pen.enabled:
            self._pen_down(event, button=2)
        else:
            self._on_click(event)

    def _on_right(self, event) -> None:
        """Правая кнопка: стирающее перо (если перо включено)."""
        if self.pen.enabled:
            self._pen_down(event, button=3)
        else:
            self._on_click(event)

    def _wheel_step(self, direction: int) -> None:
        """Колесо вверх/вниз (X11: кнопки 4/5) — тот же листатель."""
        self.scroll_view(direction)
        self.last_key = 'КОЛЕСО'
        self.paint()

    def _on_wheel(self, event) -> None:
        """Колесо мыши: листать «историю» выдачи, как PgUp/PgDn в 15ИЭ."""
        delta = getattr(event, 'delta', 0)
        # Tk сообщает колесо знаками (Windows) или числами (X11).
        self.scroll_view(-1 if delta > 0 else 1)
        self.last_key = 'КОЛЕСО'
        self.paint()

    def scroll_view(self, direction: int) -> None:
        """Листать историю: `-1` — к более раннему кадру, `1` — к текущему.

        `F5 ОЧИСТКА` складывает уходящий кадр в историю, и колесо (а также
        PgUp/PgDn) возвращает его на экран — так же, как в эталоне 15ИЭ.
        Ходить дальше текущего кадра нельзя: внизу истории только то,
        что терминал показывает сейчас.
        """
        if not self._history:
            return
        if self._hist_at is None:
            self._hist_at = len(self._history)
        self._hist_at = max(0, min(len(self._history) - 1,
                                   self._hist_at + direction))

    def shown_frame(self):
        """Кадр, который сейчас на экране: из истории или текущий."""
        if self._hist_at is not None and self._history:
            return self._history[self._hist_at]
        return self.best if self.best_n else self.emu.fb

    # ── ввод ────────────────────────────────────────────────────────────────

    def _on_key(self, event):
        """Клавиши окна: курсор, набор на экране, служебные функции.

        Стрелки ведут курсор по экрану, буквы набираются прямо в
        позиции курсора и уходят на ЭВМ (режимы АВТОНОМНО/С ЭВМ —
        как в `session.py`).
        """
        key = event.keysym
        ch = event.char
        if key in ('Prior', 'Next'):
            # ЭКРАН↑/ЭКРАН↓ — то же, что колесо мыши.
            self.scroll_view(-1 if key == 'Prior' else 1)
            self.last_key = 'ЭКРАН'
            self.paint()
            return 'break'
        if key in ('Up', 'Down', 'Left', 'Right'):
            # Курсор по ячейкам: ↑ вверх по кадру, ← влево.
            dx = {'Left': -1, 'Right': 1}.get(key, 0)
            dy = {'Up': -1, 'Down': 1}.get(key, 0)
            self.console.goto(self.console.col + dx, self.console.row + dy)
            self.last_key = key
            self.paint()
            return 'break'
        if key in ('Return', 'KP_Enter', 'Enter', 'ISO_Enter') \
                or ch in ('\r', '\n'):
            return self._on_return(event)
        if key == 'BackSpace' or ch in ('\x08', '\x7f'):
            return self._on_backspace()
        if key == 'Escape':
            return self._on_escape(event)
        if key == 'F2':                       # ПЕРО: мышь рисует по кадру
            self.pen.enabled = not self.pen.enabled
            self.pen.down = False
            self.pen._last = None
            self.diag.add('перо включено — мышь рисует' if self.pen.enabled
                          else 'перо выключено', 'ok' if self.pen.enabled
                          else 'info')
            self.last_key = 'ПЕРО'
            self.paint()
            return 'break'
        if key == 'F3':                       # ИНСТРУМЕНТ пера
            self.pen_tool = self.pen.next_tool()
            self.diag.add('инструмент пера: ' + self.pen_tool)
            self.last_key = 'ИНСТР'
            self.paint()
            return 'break'
        if key == 'F5':                       # ОЧИСТКА
            self.emu.execute(self.emu.parser.parse(b'S\x55'))
            self.console.home()
            shown = self.shown_frame()
            if shown.count_set():
                self._history.append(shown)
            self._hist_at = None
            self.best, self.best_n = self.emu.fb.copy(), 0
            self.diag.add('экран очищен', 'ok')
            self.last_key = 'ОЧИСТКА'
            self.paint()
            return 'break'
        if key == 'F6':                       # ЭХО
            self.session.toggle_echo()
            self.diag.add('эхо включено' if self.session.echo
                          else 'эхо выключено — пароль не печатается',
                          'info')
            self.last_key = 'ЭХО'
            self.paint()
            return 'break'
        if key == 'F8':                       # НАБОР
            want = 'koi8r' if self.session.charset_set == 'n2' else 'n2'
            self.session.set_charset(want)
            self.emu.state.font = want
            self.last_key = 'НАБОР'
            self.paint()
            return 'break'
        if key == 'F9':                       # СЕТЬ: АВТОНОМНО ↔ С ЭВМ
            out = self.session.toggle_mode()
            self.last_key = 'СЕТЬ'
            if out and self.driver is not None:
                self.driver.send_keys(out.decode('latin-1'))
            self.paint()
            return 'break'
        if key == 'F10':                      # ПЕРЕДАЧА
            out = self.session.send(line_end=self.eol)
            self.last_key = 'ПЕРЕДАЧА'
            if out and self.driver is not None:
                self.driver.send_keys(out.decode('latin-1'))
            self.paint()
            return 'break'
        if getattr(event, 'state', 0) & 0x4 and ch:   # Ctrl-сочетания
            low = ch.lower()
            if low == 'q':
                self.close()
                return 'break'
            if ch in '+=':                    # МАСШТАБ ближе
                self.scale = min(8, self.scale + 1)
            elif ch == '-':                   # МАСШТАБ дальше
                self.scale = max(1, self.scale - 1)
            elif low == 'c':                  # ЦЕНТР
                self.console.goto(COLS // 2, ROWS // 2)
            else:
                return None
            self._resize_canvas()
            self.last_key = 'УПР-' + low
            self.paint()
            return 'break'
        if ch and ch.isprintable():
            # Набор прямо на экране: знак уходит в линию и рисуется
            # глифом СИМ в позиции курсора (эхо, КОИ-8Р).
            out = self.session.type_key(ch)
            if out and self.driver is not None:
                self.driver.send_keys(out.decode('latin-1'))
            if self.session.echo:
                self.console.put_char(ch)
            self.last_key = self._last_label(ch)
            self._touch_frame()
            self.paint()
            return 'break'
        return None

    def _resize_canvas(self) -> None:
        """Пересчитать холст под масштаб."""
        self._canvas.config(
            width=SCREEN_WIDTH * self.scale,
            height=(SCREEN_HEIGHT + self.chrome_px()) * self.scale)

    def close(self) -> None:
        self.running = False
        if self._tk is not None:
            self._tk.quit()

    # ── отрисовка кадра ─────────────────────────────────────────────────────

    def paint(self) -> None:
        """Вывести окно целиком: служебные строки и кадр с курсором.

        Полоса из двух частей: кадр 560×400, а над ним строка состояния.
        Подвал поверх нижней кромки кадра и высоты не добавляет — иначе
        окно перестало бы влезать в экран целиком.  Всё нарисовано
        глифами ПЗУ того же размера: у экрана одна типографика, как на
        настоящем терминале.
        """
        import tkinter as tk
        top_rows, bottom_rows = self.chrome_parts()
        top_px = self.top_px()
        height = SCREEN_HEIGHT + top_px + self.footer_px()
        photo = tk.PhotoImage(master=self._tk, width=SCREEN_WIDTH,
                              height=height)
        photo.put(BG, to=(0, 0, SCREEN_WIDTH, height))
        # Кадр терминала — между служебными строками: под ним подвал,
        # над ним строка состояния.  Рабочее поле ни с чем не пересекается.
        self._blit(self.shown_frame(), photo, top_px + self.footer_px())
        strip = Framebuffer(SCREEN_WIDTH, height)
        chrome.render_status(strip, top_rows)
        chrome.render_footer(strip, bottom_rows)
        self._blit(strip, photo, 0)
        self._cursor_frame(photo, top_px + self.footer_px())
        if self.scale > 1:
            # Холст сделан scale× больше — изображение увеличивается,
            # а не уменьшается (раньше стояло `subsample`).
            photo = photo.zoom(self.scale)
        self._photo = photo                      # держим ссылку
        self._canvas.itemconfig(self._image_id, image=photo)
        self._dirty = False

    def _blit(self, fb: Framebuffer, photo, offset: int) -> None:
        """Вывести кадр `fb` в изображение, сдвинув его на `offset`."""
        self._blit_band(fb, photo, offset, 0, fb.height)

    def _blit_band(self, fb: Framebuffer, photo, offset: int,
                   y_from: int, y_to: int) -> None:
        """Вывести полосу `fb` по рядам `y_from…y_to` в изображение.

        Координаты `y` — кадровые (`Y` вверх); `offset` — куда встаёт
        **верх** полосы в изображении (строки сверху вниз).  Серии берутся
        готовыми (`Framebuffer.runs`) — обход по точкам на кадре 560×400
        занимал 120 мс и ввод с клавиатуры заметно отставал.
        """
        limit = min(y_to, fb.height)
        skip_to = offset + fb.height - limit      # ряды выше — не рисуем
        for x0, y, x1 in fb.runs():
            if y < y_from or y >= limit:
                continue
            img_y = offset + fb.height - 1 - y
            if img_y < skip_to:
                continue
            photo.put(FG, to=(x0, img_y, x1, img_y + 1))

    def _cursor_frame(self, photo, offset: int) -> None:
        """Рамка знакоместа курсора поверх кадра.

        Рамка — часть экрана терминала, но не часть его памяти: на
        кадре её нет, рисуется она при выводе (как на настоящем
        терминале, где курсор — не зажжённая точка).
        """
        x0, y0, x1, y1 = self.console.cell_rect()
        iy0 = offset + SCREEN_HEIGHT - 1 - y1
        photo.put(FG, to=(x0, iy0, x1 + 1, iy0 + 1))
        photo.put(FG, to=(x0, iy0 + RASTER_H - 1, x1 + 1, iy0 + RASTER_H))
        photo.put(FG, to=(x0, iy0, x0 + 1, iy0 + RASTER_H))
        photo.put(FG, to=(x1, iy0, x1 + 1, iy0 + RASTER_H))

    # ── строка состояния и подвал ────────────────────────────────────────────

    def status(self) -> Status:
        """Собрать состояние для строки состояния и подвала."""
        ses = self.session
        cx, cy = self.console.cursor_pixels()
        shown = self.shown_frame()
        info = self._line_info()
        return Status(
            mode=ses.mode,
            charset=ses.charset_set,
            line=info.line,
            banner=info.banner,
            link_kind=info.kind,
            machine=info.machine,
            terminal=info.terminal,
            wire=info.wire,
            channel=info.channel,
            echo=ses.echo,
            pen=self.pen.enabled,
            pen_tool=self.pen.tool,
            buffer=len(ses.buffer),
            grid=self.grid,
            scale=self.scale,
            points=shown.count_set() if shown else 0,
            last_key=self.last_key,
            input_line=''.join(ses.buffer),
            cursor=f'{cx},{cy}',
            graphics=self._graphics_label(),
        )

    def _line_info(self) -> Line:
        """Сведения о линии: имя, канал, баннер, тип линии, ЭВМ, терминал.

        Всё, кроме имени и канала, берётся из баннера SIMH
        (`link.parse_greeting`): линия, объявленная «Connected to DKS», —
        это «ДКС», а не «КВУ», и номер терминала известен только оттуда.
        """
        link = getattr(self.driver, 'link', None)
        info = getattr(link, 'info', None)
        kind = info.kind if info else ''
        machine = info.machine if info else 0
        terminal = info.terminal if info else 0
        wire = info.wire if info else ''
        banner = bool(info.seen) if info else False
        greeting = getattr(self.driver, 'greeting', b'')
        banner = banner or bool(greeting)

        endpoint = self.endpoint or ''
        if endpoint:
            host, _, port = endpoint.rpartition(':')
            spec = port.split('-')[0] if port else ''
            line = endpoint
        elif self.driver is None:
            line, spec = 'НЕТ ЛИНИИ', ''
        else:
            line, spec = getattr(self.driver, 'name', '—'), ''
        port = getattr(link, 'port', None)
        return Line(line=line,
                    channel=spec or (str(port) if port else ''),
                    banner=banner, kind=kind, machine=machine,
                    terminal=terminal, wire=wire)

    def _graphics_label(self) -> str:
        cfg = self.config
        mode = (getattr(cfg, 'graphics', None) or 'auto') if cfg else 'auto'
        return {'auto': 'протокол', 'kitty': 'протокол',
                'blocks': 'псевдографика', 'mono': 'ч/б'}.get(mode, mode)

    def _panel_lines(self) -> list:
        """Подвал текстом без цвета — для проверок и снимков экрана."""
        from . import panel
        st = self.status()
        width = chrome.cols()
        pl = panel.plan(width, colw=chrome.CELL_COLS)
        return [panel.render_plain(row, pl.width)
                for row in chrome.footer_rows(st, help_=self.show_help,
                                             diag=self.diag)]

    def _status_lines(self) -> list:
        """Верхняя строка состояния текстом — для проверок."""
        from . import panel
        return [panel.render_plain(row)
                for row in chrome.status_rows(self.status())]

    def _last_label(self, key: str) -> str:
        """«ПОСЛ:» — код выданного знака и сам знак (`≡144 'd'`)."""
        if len(key) == 1 and key.isprintable():
            code = key.encode('koi8-r', errors='replace')[0]
            return f'≡{code:o} {key!r}'
        return key[:12]

    # ── цикл ────────────────────────────────────────────────────────────────

    #: Как часто окно опрашивает линию, мс.
    TICK_MS = 50

    def run(self) -> int:
        try:
            root = self.build()
        except ImportError as exc:                 # pragma: no cover
            print('нужен tkinter: ' + str(exc), flush=True)
            return 3                               # графика недоступна
        except Exception as exc:                   # pragma: no cover
            # Нет дисплея (TclError) и прочие сбои Tk — тоже без графики.
            print('окно не открылось: ' + str(exc), flush=True)
            return 3
        try:
            self.paint()
            root.after(self.TICK_MS, self._tick)
            root.mainloop()
        finally:
            if self.driver is not None:
                self.driver.close()
        return 0

    def _tick(self) -> None:
        """Опрашивает линию и перерисовывает окно **только при нужде**.

        Раньше `paint()` вызывался на каждом обороте (20 раз в секунду)
        безусловно: кадр не менялся, а перерисовка стоила десятки
        миллисекунд, поэтому окно дёргалось и набор с клавиатуры шёл
        рывками.  Теперь перерисовка идёт только когда изменился кадр
        или состояние (`dirty`), а часы в строке состояния — раз в
        секунду.
        """
        if not self.running:
            return
        if self.driver is not None:
            data = self.driver.poll(0.05)
            if data:
                self.feed(data)
        now = time.monotonic()
        if self._dirty or now - self._clock_at >= 1.0:
            self._clock_at = now
            self.paint()
        self._tk.after(self.TICK_MS, self._tick)


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
    """Открыть графическое окно терминала.

    Как и `run_tui`: без явного адреса линия берётся из конфигурации
    (`[line] endpoint`), иначе набор и Enter молчали бы — текст не
    уходил бы на ЭВМ.  Недоступная линия — объяснение и код 2, молча
    подменять её нельзя.
    """
    from .tui import BuiltinDriver, DEFAULT_ENDPOINT, LiveDriver, LinkRefused
    cfg = config or None
    if endpoint is None and not demo:
        endpoint = getattr(cfg, 'endpoint', None) or DEFAULT_ENDPOINT
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
    win = TerminalWindow(driver, scale=scale, endpoint=endpoint, config=cfg)
    return win.run()


__all__ = ['TerminalWindow', 'run_window', 'fb_to_ppm']
