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
import os
import re
import sys
import time
import zlib
from pathlib import Path
from typing import Callable, Optional

from .emulator import TerminalEmulator
from . import panel
from . import startup
from .config import Config
from .pen import LightPen
from .link import EchoStrip, LinkError
from .session import MODE_HOST, Session

# ── коды линии ДКС ──────────────────────────────────────────────────────────
# По эталону 15ИЭ (`ie15emu/charset.py::to_line`, ветка dks) и живой
# машине: `dks_line_char` завершает строку по **ПР (CR)**, а ETX для
# линии — просто знак (команда с ETX не выполняется).

#: Возврат каретки (ПР): им ОС Диспак завершает строку ввода; после
#: пустой строки (ПРПС) ОС отвечает приглашением «ЭВМ-3,ТNNN».
CR = 0o015
#: Перевод строки (ПС); пара ПР ПС сворачивается в один ПР.
LF = 0o012
#: Гид-символ ОС Диспак (ETX) — для линии ДКС просто знак.
ETX = 0o003

#: Приветствие, которое выдаёт линия ДКС при подключении терминала
#: (её выдаёт обвязка стенда — самой ОС Диспак это не свойственно):
#: «Connected to the БЭСМ-6 simulator TTY device, line N».
GREETING_EN = 'Connected to the'

#: Конец строки, который терминал передаёт на ЭВМ.  Переопределяется
#: настройкой `[line] eol`.
DEFAULT_EOL = CR

#: Линия ДКС, к которой подключается эмулятор по умолчанию.
#: ЭВМ слушает порты 4202-4223 (`set ttyN dks,raw` в SIMH).
DEFAULT_ENDPOINT = '127.0.0.1:4202-4223'


from .reader import CommandStreamReader, EventKind
from .console import GLYPH_SCALE, set_glyph_scale
from .panel import Diagnostics, Status
from .screen import ScreenRenderer, Viewport
from .constants import VK
from .term import (ALT_SCREEN_OFF, ALT_SCREEN_ON, BOLD, CLEAR, ERASE_EOL,
                   HIDE_CURSOR, HOME, KEY_CLEAR, KEY_CMDSET, KEY_ECHO,
                   KEY_MODE, KEY_SEND, RESET, SESSION_KEYS, SHOW_CURSOR,
                   RawTerminal, at, bar, read_key, terminal_size)

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


def _render_picture(path, cols: int, rows: int) -> str:
    """Показать эталонный рисунок как ASCII-арт.

    Берётся поток команд (`golden/streams/*.bin`), он же (`*.png`) —
    если потока рядом нет, PNG разбирается построчно.
    """
    from pathlib import Path
    p = Path(path)
    if p.suffix.lower() in ('.bin', ''):
        return _render_stream(p, cols, rows)
    if p.suffix.lower() == '.svg':
        return _render_svg(p, cols, rows)
    return _render_png(p, cols, rows)


def _render_stream(bin_path, cols: int, rows: int) -> str:
    """Отрисовать поток команд и вывести кадр символами.

    Рисунок берётся из самого эталонного потока, а не из картинки:
    свой же PNG разбирать не нужно и формат не важен.
    """
    from .emulator import TerminalEmulator
    from .framebuffer import Framebuffer
    from .reader import CommandStreamReader, EventKind
    emu = TerminalEmulator()
    reader = CommandStreamReader()
    reader.feed(Path(bin_path).read_bytes())
    reader.close()
    best, best_n = emu.fb.copy(), 0
    for ev in reader.pop():
        # Программа очищает экран в конце: берём самую наполненную
        # картину — ту, что видел оператор.
        if ev.kind is EventKind.VUK_BLOCK:
            n = emu.fb.count_set()
            if n > best_n:
                best, best_n = emu.fb.copy(), n
        emu.handle_event(ev)
    fb = best if best_n else emu.fb
    sx = max(1, fb.width // max(1, cols))
    sy = max(1, fb.height // max(1, rows * 2))
    lines = []
    for r in range(rows):
        y = r * sy * 2
        line = []
        for c in range(cols):
            x = c * sx
            hit = any(fb.get(xx, yy)
                      for yy in (fb.height - 1 - y, fb.height - 2 - y)
                      for xx in (x, x + sx - 1))
            line.append('#' if hit else ' ')
        lines.append(''.join(line))
    return '\n'.join(lines)


def _render_png(path, cols: int, rows: int) -> str:
    """ASCII-арт из PNG: строки разворачиваются фильтром PNG не нужно —
    берём кадр, если PNG получен из нашего же эталонного потока."""
    import zlib
    data = Path(path).read_bytes()
    pos, idat, w, h, depth, ctype = 8, b'', 0, 0, 8, 0
    while pos < len(data):
        ln = int.from_bytes(data[pos:pos + 4], 'big')
        typ = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        if typ == b'IHDR':
            w = int.from_bytes(body[0:4], 'big')
            h = int.from_bytes(body[4:8], 'big')
            depth, ctype = body[8], body[9]
        elif typ == b'IDAT':
            idat += body
        pos += 12 + ln
    if not w or depth != 8:
        return ''
    bpp = {0: 1, 2: 3, 4: 2, 6: 4}.get(ctype, 1)
    try:
        raw = zlib.decompress(idat)
    except zlib.error:
        return ''
    stride = w * bpp
    prev = bytearray(stride)
    rows_out = []
    for y in range(h):
        off = y * (stride + 1)
        if off >= len(raw):
            break
        ft = raw[off]
        line = bytearray(raw[off + 1:off + 1 + stride])
        # разворот фильтров PNG (RFC 2083)
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            x = line[i]
            if ft == 1:
                x += a
            elif ft == 2:
                x += b
            elif ft == 3:
                x += (a + b) // 2
            elif ft == 4:
                pp = a + b - c
                pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
                x += a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
            line[i] = x & 0xFF
        prev = line
        rows_out.append(line)
    if not rows_out:
        return ''
    sy = max(1, len(rows_out) // max(1, rows))
    sx = max(1, w // max(1, cols))
    out = []
    for r in range(rows):
        y = r * sy
        if y >= len(rows_out):
            break
        line = rows_out[y]
        cells = []
        for c in range(cols):
            x = c * sx * bpp
            lum = line[x] if x < stride else 0
            cells.append('#' if lum > 40 else ' ')
        out.append(''.join(cells))
    return '\n'.join(out)


def _render_svg(path, cols: int, rows: int) -> str:
    import re
    text = Path(path).read_text(encoding='utf-8')
    rects = re.findall(r'<rect[^>]*x="([\d.]+)"[^>]*y="([\d.]+)"', text)
    if not rects:
        return ''
    grid = [[' '] * cols for _ in range(rows)]
    for xs, ys in rects:
        c = int(float(xs) * cols / 560)
        r = int(float(ys) * rows / 400)
        if 0 <= r < rows and 0 <= c < cols:
            grid[r][c] = '#'
    return '\n'.join(''.join(r) for r in grid)


def _utf8_width(first: int) -> int:
    """Длина UTF-8-последовательности по первому байту (0 — не UTF-8)."""
    if 0xC2 <= first <= 0xDF:
        return 2
    if 0xE0 <= first <= 0xEF:
        return 3
    if 0xF0 <= first <= 0xF4:
        return 4
    return 0


class LinkRefused(Exception):
    """Линия ДКС не открылась; текст — готовое объяснение для пользователя."""


# ── драйверы ────────────────────────────────────────────────────────────────

class Driver:
    """Источник данных терминала и приёмник нажатий."""

    name = 'драйвер'
    #: Приветствие линии, прочитанное при подключении (если было).
    greeting: bytes = b''

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

    #: Задержка перед первым запросом приглашения ОС: Э-60
    #: регистрируется ОС не сразу и ранние строки проглатывает
    #: (эталон 15ИЭ: `agent.py` — `drain()`, затем `key("KEY_ENTER")`,
    #: «Э-60 регистрируется ОС не сразу» — `session.py`).
    PROMPT_DELAY = 1.5
    #: Повторы запроса приглашения, если ответа нет.
    PROMPT_TRIES = 5
    #: Пауза между повторами, с.
    PROMPT_RETRY = 3.0

    def __init__(self, endpoint: str) -> None:
        from .link import DKSLink, LinkError, spec_hint, split_endpoint
        host, ports = split_endpoint(endpoint)
        self.endpoint = endpoint
        self.host, self.ports = host, ports
        self.link = DKSLink(host, ports)
        try:
            self.link.connect()
        except LinkError as exc:
            raise LinkRefused(self._diagnose(exc)) from exc
        self.name = f'ДКС {host}:{self.link.port}'
        # Приветствие линии показываем в окне: по нему видно, какая
        # линия досталась и в каком режиме она работает.
        self.greeting = self.link.greeting
        #: Эхо линии: Э-60 всегда эхит отданные знаки — срезаем свой
        #: эхо-ответ из потока (иначе набранное показывается дважды,
        #: а пароль виден в эхе машины).
        self._echo = EchoStrip()
        #: Запрос приглашения ОС (ПРПС): повторяется, пока машина
        #: не ответит.
        self._prompt_at = time.monotonic() + self.PROMPT_DELAY
        self._prompt_tries = 0
        self._prompted = False

    def _diagnose(self, exc: Exception) -> str:
        """Человекочитаемое объяснение, почему линия не открылась."""
        from .link import DEFAULT_PORTS, spec_hint
        lines = [
            f'Не удалось подключиться к линии ДКС: {exc}',
            '',
            f'  запрошено     : {self.host}:{spec_hint(self.ports)}',
            f'  ЭВМ слушает   : порты {spec_hint(self.ports)}',
        ]
        if len(self.ports) == 1:
            lines += [
                '',
                'Причины по порядку:',
                f'  1. линия {self.ports[0]} уже занята другим терминалом '
                '(ДКС допускает один терминал на линию);',
                '  2. линия не создана: ОС Диспак не зарегистрировала канал '
                f'(КРК {self.ports[0] - 4200 + 2});',
                '  3. эмулятор БЭСМ-6 не запущен либо линия не в режиме ДКС '
                '(`set ttyN dks,raw`).',
                '',
                f'Попробовать другие линии: --link {self.host}:'
                f'{spec_hint(range(self.ports[0] + 1, self.ports[0] + 4))}',
                f'  или весь диапазон: --link {self.host}:{DEFAULT_PORTS}',
            ]
        else:
            lines += [
                '',
                'Все указанные порты заняты или не слушаются. Проверьте,',
                'что линии в режиме ДКС и свободны.',
            ]
        return '\n'.join(lines)

    def poll(self, timeout: float) -> bytes:
        try:
            data = self.link.recv(timeout=timeout)
        except (TimeoutError, OSError, LinkError):
            # `LinkError` ловится здесь же, а не выше: обход окна
            # спрашивает линию уже после того, как она закрыта выходом,
            # и без этой строки Tk печатал трассировку на каждый оборот
            # (`Exception in Tkinter callback`).
            data = b''
        data = self._echo.strip(data)
        # Ответом считается любой приход данных после нашего запроса
        # (баннер линии приходит до него и в счёт не идёт).
        if data and self._prompt_tries:
            self._prompted = True
        if not self._prompted:
            # Запрос приглашения живёт здесь же: recv на тихой линии
            # отбрасывается таймаутом, и без этого до него не дошло бы.
            self._request_prompt()
        return data

    def _request_prompt(self) -> None:
        """Запросить приглашение ОС — ПРПС (пустая строка), как в 15ИЭ.

        ОС Диспак отвечает на ПРПС приглашением «ЭВМ-3,ТNNN»; так же
        поступает эталон (`ie15emu/agent.py`: `key("KEY_ENTER")`).
        Э-60 регистрируется ОС не сразу и ранние строки проглатывает
        (`ie15emu/session.py`), поэтому запрос повторяется с паузой,
        пока машина не ответит.  Если ответа так и нет — оператор
        жмёт ПРПС (Enter) сам.
        """
        now = time.monotonic()
        if self._prompt_tries >= self.PROMPT_TRIES or now < self._prompt_at:
            return
        self._prompt_tries += 1
        self._prompt_at = now + self.PROMPT_RETRY
        self.send_keys('\r')                 # ПРПС = ПР = конец строки

    def send_keys(self, text: str) -> None:
        data = text.encode('koi8-r', errors='replace')
        self._echo.sent(data)
        self.link.send(data)

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
                 grid: bool = False, intro: str = '',
                 config: Optional[Config] = None,
                 graphics: str = 'auto') -> None:
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
        #: Байты, присланные ЭВМ (приглашения ОС Диспак, ответы программ).
        #: Хранятся кодами: текст разбирается по набору знаков при выводе.
        self._raw_text: list[int] = []
        #: Графика в окне терминала (протокол kitty); `None`, если нет.
        self.gfx = None
        #: Способ показа графики: auto | kitty | blocks | mono.
        self.graphics = 'auto'
        #: Режимы терминала по образцу 15ИЭ-00-013: АВТОНОМНО / С ЭВМ,
        #: SEND, ЭХО, ОЧИСТКА, переключение наборов.
        self.session = Session()
        #: Настройки из файла конфигурации.
        self.config = config or Config.default()
        self.session.mode = self.config.mode
        self.session.set_charset(self.config.charset)
        self.session.echo = self.config.echo
        self.pen.enabled = self.config.pen
        self.grid = self.config.grid or grid
        self.graphics = self.config.graphics or graphics
        self.eol = getattr(self.config, 'eol', DEFAULT_EOL)
        #: Размер знака текстового слоя кадра — `[terminal] glyph`.
        set_glyph_scale(getattr(self.config, 'glyph', GLYPH_SCALE))
        #: Диагностика — журнал сообщений, последнее идёт под подвалом.
        self.diag = Diagnostics()
        #: Ряды-легенды подвала (`[terminal] help`, как `--no-help` в 15ИЭ).
        self.show_help = getattr(self.config, 'help', True)
        self._splash_fb_cache = None
        #: ASCII-арт заставки — по ключу (картинка, высота); иначе он
        #: разбирался заново из потока команд на каждом кадре.
        self._splash_key = None
        self._splash_art: list = []
        #: Подпись выгруженной картинки (кэш протокола графики).
        self._gfx_key = None
        #: Самый наполненный кадр (программа чистит экран в конце).
        self._best_fb = None
        self._best_n = 0
        #: История очищенных экранов — на неё возвращает `ЭКРАН↑`.
        self._history: list = []
        #: Нажат ли `Ctrl`: служебные клавиши эмулятора — только с ним.
        self.ctrl = False
        #: Для какого кадра и при какой геометрии окна подгонка уже
        #: выполнена: `(кадр, (столбцы, ряды))`.  Смена любой части означает,
        #: что параметры окна изменились и кадр надо вписать заново.
        self._fitted_for = None
        #: Сколько рядов окна над кадром (заголовок и текст ЭВМ): ряды
        #: мыши приходят от верха окна, а перо считает их от кадра.
        self._body_offset = 0
        #: Протокол графики запрошен, но терминал не ответил.
        self._gfx_failed = False
        # Приветствие линии на экран не выводится: это служебный
        # заголовок соединения, а не ответ ОС Диспак.  Оно целиком уходит
        # в `DKSLink.greeting`, откуда его читает разбор для строки
        # состояния (`ДКС`, `ЭВМ=`, `ТЕРМ=`).
        #: Данные получены? до них показывается начальный экран.
        self._got_data = False
        #: Последняя нажатая клавиша — строка «ПОСЛ:» подвала.
        self.last_key = ''
        #: Эхо набираемого: то, что ввёл оператор, видно на экране
        #: (клавиша ЭХО снимает вывод — чтобы не печатать пароли).
        self.echo_buf = ''
        #: Заставка закрыта ли? по умолчанию показывается.
        self.splash = True
        self._connect_note = ''
        self.running = True
        self._grid_fb = None

    # ── приём данных ────────────────────────────────────────────────────────

    #: Буквы команд терминала в начале записи.  Запись, начинающаяся с
    #: одной из них, — операция графики; всё остальное — текст.
    COMMAND_LETTERS = b'J01245789]}>*3+KS/'

    @classmethod
    def is_command_stream(cls, data: bytes) -> bool:
        """Похоже ли это на поток команд терминала, а не на текст ОС.

        Признаки: маркер кадра `АР2` или начало блока ВУК `'/' ВК`,
        либо запись, начинающаяся с буквы команды.  Сообщения ОС Диспак
        («ЭВМ-3», приглашения) под это не подходят и идут как текст.
        """
        from .constants import AR2, VK
        if bytes((AR2,)) + b'F' in data:
            return True
        if b'/' + bytes((VK,)) in data:
            return True
        for rec in data.split(bytes((VK,))):
            if rec[:1] in (bytes((c,)) for c in cls.COMMAND_LETTERS):
                return True
        return False

    def feed(self, data: bytes) -> None:
        """Принять данные: графику — в кадр, текст — в строку сообщений."""
        if not data:
            return
        if not self.is_command_stream(data):
            # Текст от ЭВМ: приглашения ОС Диспак и ответы программ.
            # Он идёт в то же окно, что и графика, и снимает заставку.
            self._raw_text.extend(data)
            self.splash = False
            return
        self._got_data = True
        self.splash = False
        self.reader.feed(data)
        for ev in self.reader.pop():
            if ev.kind is EventKind.COMMAND:
                self.emu.execute(self.emu.parser.parse(ev.data))
            else:
                # Программа (`graf.pas`) чистит экран перед выходом, и
                # после её работы кадр пуст.  Поэтому запоминаем самый
                # наполненный кадр — тот, что видел оператор, — и
                # показываем его, а не пустой результат.
                self.emu.handle_event(ev)
                self._remember_frame()

    def _clean_text(self) -> str:
        """Текст ОС без управляющих последовательностей, в одну строку."""
        return ' '.join(' '.join(self._text_lines()).split())

    # ── рисование ───────────────────────────────────────────────────────────

    def layout(self) -> tuple[int, int]:
        """Столбцы и строки под изображение.

        Сверху — необязательная строка заголовка, снизу — подвал.  Кадр
        выводится позиционированием курсора, поэтому важно, чтобы рядов
        не оказалось больше, чем строк в окне: лишний ряд уводит курсор
        на новую строку и прокручивает экран.
        """
        cols, trows = terminal_size()
        n = self.panel_rows(cols) + (1 if self.config.title else 0)
        return cols, max(1, trows - n)

    #: Минимальное число рядов подвала — когда легенда убрана.
    PANEL_ROWS = 5

    def panel_rows(self, cols: int) -> int:
        """Сколько рядов займёт подвал в окне шириной `cols`.

        Высота подвала зависит от ширины окна: ячейки раскладываются по
        плану `panel`, и в узком окне их в ряд помещается меньше.  Раньше
        здесь стояла константа «9 рядов» — в узком окне подвал уезжал за
        нижнюю кромку, а в широком оставлял пустоту.
        """
        rows = panel.footer(self.status(), width=cols, help_=self.show_help)
        return len(rows) + (1 if self.diag.last else 0)

    def draw(self) -> None:
        """Вывести кадр целиком: каждый ряд — на своё место.

        Раньше кадр печатался переводами строки, и последняя строка
        прокручивала окно: следующий кадр ложился со сдвигом, и на экране
        оставались хвосты прежнего — строки выглядели удвоенными.  Здесь
        ряды ставятся курсором, а остаток строки стирается, поэтому
        картинка всегда ровно на своём месте.
        """
        cols, rows = self.layout()
        out = [HIDE_CURSOR]
        row = 1
        if self.config.title:
            # Строка состояния линии — как на 15ИЭ: тип линии, ЭВМ,
            # терминал, канал, режим, время.  Без баннера линии в ней
            # горит «ПРОВЕРЬ ЛИНИЮ!».
            out.append(at(row) + ERASE_EOL + self._status_line(cols))
            row += 1
        # Графика протоколом терминала занимает свои строки, текст идёт
        # под ней — то и другое в одном окне.
        gfx_h = self._paint_gfx(row - 1, cols, rows)
        if gfx_h:
            # Кадр протоколом kitty идёт первым же рядом под заголовком.
            self._body_offset = 0
            for blank in range(gfx_h):
                out.append(at(row) + ERASE_EOL)
                row += 1
            lines = self._text_window(cols, rows - gfx_h)
        else:
            lines = self._body_lines(cols, rows)
        for line in lines:
            out.append(at(row) + ERASE_EOL + line)
            row += 1
        for line in self._service_panel(cols):
            out.append(at(row) + ERASE_EOL + bar(line, cols))
            row += 1
        sys.stdout.write(''.join(out))
        sys.stdout.flush()

    def _remember_frame(self) -> None:
        """Запомнить самый наполненный кадр.

        `graf.pas` по завершении очищает экран, поэтому «текущий» кадр в
        конце пуст; без этой памяти окно показывало бы пустоту вместо
        рисунка.
        """
        n = self.emu.fb.count_set()
        if n > self._best_n:
            self._best_fb = self.emu.fb.copy()
            self._best_n = n

    def _visible_fb(self):
        """Кадр для показа: текущий, а если тот пуст — лучший из бывших."""
        if self.emu.fb.count_set():
            return self.emu.fb
        return self._best_fb

    def _text_window(self, cols: int, rows: int) -> list:
        """Строки текста ЭВМ под картинкой, ровно `rows` штук."""
        text = self._text_lines()
        lines = text[-rows:] if rows > 0 else []
        while len(lines) < rows:
            lines.append('')
        return lines[:max(0, rows)]

    def _body_lines(self, cols: int, rows: int) -> list:
        """Содержимое окна: текст ЭВМ, заставка и кадр — вместе.

        Текст, присланный ЭВМ (приглашения и ответы ОС Диспак), идёт
        сверху; под ним — рисунок, если кадр есть.  Окно одно: и надпись,
        и графика видны одновременно, как на настоящем терминале.
        """
        from . import chars
        text = self._text_lines()
        has_fb = self._visible_fb() is not None
        # Текста берём столько, сколько помещается, кадру оставляем
        # хотя бы треть окна — иначе при длинном ответе ЭВМ рисунок
        # совсем пропадёт.
        take = min(len(text), max(0, rows - max(3, rows // 3))) if has_fb \
            else len(text)
        lines = text[len(text) - take:] if take else []
        rest = max(0, rows - len(lines))
        if rest == 0:
            return lines[:rows]
        if self.splash and not self._got_data:
            pic = self._splash_lines(cols, rest)
            lines.extend(pic)
            rest -= len(pic)
        fb = self._visible_fb() if has_fb else None
        if fb is not None and rest > 0:
            vp = self.renderer.vp
            # Вписываем кадр в окно, когда он сменился или изменились
            # параметры окна (размер терминала, число рядов под подвалом):
            # первый кадр приходит уже после запуска, и без этого виден
            # только угол (масштаб остаётся от пустого экрана).  Кадр
            # встаёт в верхний левый угол — как на 15ИЭ, а не по центру.
            geom = (cols, rest)
            if self._fitted_for != (fb, geom):
                vp.fit(fb, cols, rest)
                self._fitted_for = (fb, geom)
            else:
                vp.clamp(fb, cols, rest)
            if self.pen.enabled:
                self.pen.stroke(fb)
            self.renderer.fb = fb
            body = self.renderer.render(cols, rest)
            if self.grid:
                body = self._overlay_grid(body, cols, rest)
            if self.pen.enabled:
                body = self._overlay_pen(body, cols, rest)
            # Кадр начинается после текстовых строк — запоминаем, сколько
            # их, чтобы пересчитывать ряды мыши от начала кадра.
            self._body_offset = len(lines)
            lines.extend(body.split('\n'))
        while len(lines) < rows:
            lines.append('')
        return lines[:rows]

    # ── графика в окне терминала ────────────────────────────────────────────

    def _enable_gfx(self) -> bool:
        """Заказать протокол графики терминала.

        В режиме `auto` запрос отправляется **всегда**, без опроса
        переменных окружения: поддержка протокола через окружение
        определяется ненадёжно (нет ни `KITTY_WINDOW_ID`, ни
        `TERM_PROGRAM` у ряда терминалов), а проверка по ответу терминала
        точная.  Если ответа не будет, `_probe_gfx` вернёт `False` и
        показ останется псевдографикой — с явным сообщением.
        """
        if self.graphics in ('blocks', 'mono'):
            return False
        from .backends.kitty import KittyGraphics
        self.gfx = KittyGraphics(sys.stdout)
        return True

    def _probe_gfx(self, timeout: float = 0.5) -> bool:
        """Спросить у терминала размер знакоместа и включить графику.

        Ответ приходит на вход — тем же каналом, что и нажатия, поэтому
        он читается до входа в основной цикл.
        """
        if self.gfx is None:
            return False
        sys.stdout.write(self.gfx.request_cell().decode('ascii'))
        sys.stdout.flush()
        # Ответ читается напрямую: APC начинается с `ESC`, и `read_key`
        # разобрал бы его как нажатия клавиш.
        import select
        buf = b''
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            left = max(0.0, deadline - time.monotonic())
            try:
                ready, _, _ = select.select([sys.stdin], [], [], left)
            except Exception:
                break
            if not ready:
                break
            chunk = os.read(sys.stdin.fileno(), 256)
            if not chunk:
                break
            buf += chunk
            if b'\x1b\\' in buf:
                break
        self.gfx.apply_response(buf.decode('latin-1'))
        # Нажатия, попавшие в окно пробы, не должны теряться: всё, что не
        # ответ протокола графики, возвращается в буфер ввода.
        import re as _re
        spans = _re.findall(rb'\x1b_G.*?\x1b\\', buf, _re.DOTALL)
        rest = buf
        for span in spans:
            rest = rest.replace(span, b'')
        if rest:
            from .term import push_input
            push_input(rest)
        if not self.gfx.active:
            self.gfx = None
            # Молча переходить на псевдографику нельзя: оператор будет
            # думать, что так выглядит настоящий терминал.
            self.message = ('протокол графики не поддержан — показ '
                            'псевдографикой')
            if self.graphics == 'kitty':
                self._gfx_failed = True
        else:
            self.renderer.mode = 'color'
        return bool(self.gfx and self.gfx.active)

    def _paint_gfx(self, row: int, cols: int, rows: int) -> int:
        """Показать кадр протоколом графики терминала.

        Возвращает число занятых знакомест по вертикали; под ними остаются
        строки для текста — графика и текст видны в одном окне.  Картинка
        выгружается только при изменении кадра или геометрии: кодирование
        PNG и передача APC — самое дорогое в отрисовке, и без кэша набор
        клавиш был очень медленным.
        """
        if self.gfx is None or not self.gfx.active:
            return 0
        fb = self._display_fb()
        if fb is None or not fb.count_set():
            return 0
        cw, ch = self.gfx.cell
        need_cols = (fb.width + cw - 1) // cw
        need_rows = (fb.height + ch - 1) // ch
        if need_cols > cols:
            # Кадр шире окна: вписываем по ширине, высота считается от
            # пропорции, иначе картинка получится сплющенной.
            need_rows = max(1, int(fb.height * cols * cw / fb.width / ch))
            need_cols = cols
        height = min(need_rows, rows)
        if height < 1:
            return 0
        key = (zlib.crc32(bytes(fb._bits)), row, cols, rows,
               need_cols, height)
        if key != self._gfx_key:
            self.gfx.blit(fb, row=row, col=0, cols=need_cols, rows=height,
                          force=True)
            self._gfx_key = key
        return height

    def _display_fb(self):
        """Кадр для показа: заставка или то, что нарисовала программа."""
        if self.splash and not self._got_data:
            return self._splash_frame()
        return self._visible_fb()

    def _splash_frame(self):
        """Кадр заставки из эталонного потока (берётся один раз)."""
        if self._splash_fb_cache is not None:
            return self._splash_fb_cache or None
        if (self.config.splash or 'none').lower() == 'none':
            self._splash_fb_cache = False
            return None
        from .backends.kitty import KittyGraphics
        pic = startup.picture_path(self.config.splash, prefer_stream=True)
        if not pic or not str(pic).lower().endswith('.bin'):
            self._splash_fb_cache = False
            return None
        fb = KittyGraphics.frame_from_stream(Path(pic))
        self._splash_fb_cache = fb if fb is not None else False
        return self._splash_fb_cache or None

    def _text_lines(self) -> list:
        """Строки, присланные ЭВМ, разобранные по переводу строки.

        Байты приходят кодами знаков терминала, а не готовым текстом:
        каждый код переводится через таблицу набора (`chars.display_char`),
        иначе вместо «ЭВМ-3,Т017» будет нечитаемая мешанина.
        """
        from . import chars
        out: list = []
        cur: list = []
        for piece in self._decode_bytes(self._raw_text):
            if isinstance(piece, str):
                cur.append(piece)
                continue
            code = piece
            if code in (LF, CR, VK):
                if cur:
                    out.append(''.join(cur))
                cur = []
                continue
            if code < 32 or code == 0x7F:
                continue                        # управляющие не показываем
            cur.append(chars.display_dks(code))
        if cur:
            out.append(''.join(cur))
        out = [ln.rstrip() for ln in out if ln.strip()]
        # Эхо оператора: набранное видно на экране, пустые строки
        # сохраняются — это переводы строки.
        out += [ln.rstrip() for ln in self.echo_buf.splitlines()]
        return out

    def _decode_bytes(self, data) -> list:
        """Разделить поток на куски: готовый текст и коды знаков.

        ОС Диспак присылает по линии коды знаков терминала (0x60…0x7F), а
        обвязка стенда — обычный UTF-8.  Различить их можно только по
        самим байтам: многобайтная последовательность UTF-8 не может быть
        кодом знака семибитного терминала.  Поэтому UTF-8 собирается в
        строку, а всё остальное трактуется как код.
        """
        out: list = []
        i, n = 0, len(data)
        while i < n:
            b = data[i]
            if b == 0x0D:
                out.append(CR)
                i += 1
                continue
            if b == 0x0A:
                out.append(LF)
                i += 1
                continue
            if b < 0x80:
                out.append(b)
                i += 1
                continue
            width = _utf8_width(b)
            if width and i + width <= n:
                chunk = bytes(data[i:i + width])
                try:
                    out.append(chunk.decode('utf-8'))
                except UnicodeDecodeError:
                    out.append(b)
                    i += 1
                else:
                    i += width
                continue
            # Не UTF-8: это код семибитного знака терминала.
            out.append(b)
            i += 1
        return out

    def _title(self) -> str:
        st = self.renderer.status(self.layout()[0], self.layout()[1])
        mode = ('С ЭВМ' if self.session.mode == 'host'
                else 'АВТОНОМНО')
        parts = [f'МС 7401 — {self.driver.name}',
                 'РЕЖИМ=' + mode, self.session.panel(),
                 st, self.status_extra]
        return ' | '.join(p for p in parts if p)

    def _hint(self) -> str:
        """Строка подсказок; если ЭВМ прислала текст — он важнее."""
        text = self._clean_text()
        if text:
            return f'ЭВМ: {text[-200:]}'
        if self.message:
            return self.message
        return ('РЕЖИМ=L  ПЕРЕДАЧА=S  ЭХО=e  ОЧИСТКА=x  НАБОР=z  '
                'ПЕРО=p  МАСШТАБ=+/-  ВПИСАТЬ=0  ЦЕНТР=c  '
                'ЦВЕТ=m  СЕТКА=g  ВЫХОД=q')

    def _input_row(self) -> str:
        return f' ВВОД: {self.input_line}▏'

    def _overlay_pen(self, body: str, cols: int, rows: int) -> str:
        """Показать положение пера обратным видео — как курсор на экране."""
        lines = body.split('\n')
        vp = self.renderer.vp
        col = (self.pen.x - vp.x) // vp.scale
        # `pen.y` — координата кадра «Y вверх», `vp.y` — смещение сверху:
        # Y переворачивается, как в `screen.py`, иначе перо зеркалилось
        # по вертикали.
        fb = self.renderer.fb
        height = fb.height if fb is not None else self.emu.fb.height
        row = (height - 1 - self.pen.y - vp.y) // (vp.scale * 2)
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


    # ── подвал (как в 15ИЭ-00-013) ──────────────────────────────────

    def status(self) -> Status:
        """Состояние для строки состояния и подвала.

        Тот же `Status`, что у графического окна: набор ячеек один,
        различается только вывод (здесь — escape-последовательности).
        """
        ses = self.session
        shown = self._visible_fb()
        line, channel, banner, kind, machine, terminal, wire = \
            self._line_info()
        return Status(
            mode=ses.mode,
            charset=ses.charset_set,
            line=line,
            link_kind=kind,
            banner=banner,
            machine=machine,
            terminal=terminal,
            wire=wire,
            channel=channel,
            echo=ses.echo,
            pen=self.pen.enabled,
            pen_tool=self.pen.tool,
            buffer=len(ses.buffer),
            grid=self.grid,
            scale=self.renderer.vp.scale,
            points=shown.count_set() if shown else 0,
            last_key=self.last_key,
            input_line=self.input_line,
            cursor='0,0',
            graphics=('протокол' if self.gfx and self.gfx.active
                      else 'псевдографика'),
        )

    def _line_info(self):
        """Строка линии, канал, баннер, тип линии, ЭВМ, терминал, режим."""
        link = getattr(self.driver, 'link', None)
        info = getattr(link, 'info', None)
        kind = info.kind if info else ''
        machine = info.machine if info else 0
        terminal = info.terminal if info else 0
        wire = info.wire if info else ''
        banner = bool(info.seen) if info else False
        greeting = getattr(self.driver, 'greeting', b'')
        banner = banner or bool(greeting)
        port = getattr(link, 'port', None)
        return (self._link_state(), str(port) if port else '', banner,
                kind, machine, terminal, wire)

    def _status_line(self, cols: int) -> str:
        """Верхняя строка состояния линии — над кадром, вне его.

        Ячейки из `panel`, как в подвале и как на 15ИЭ; при нехватке
        места строка обрезается по ширине окна.
        """
        cells = panel.status_line(self.status())
        text, _ = panel.render_spans(cells)
        return text[:cols] if len(text) > cols else text

    def _service_panel(self, cols: int) -> list:
        """Подвал: состояния, черта и легенда — оформление `panel`."""
        pl = panel.plan(cols)
        st = self.status()
        rows = panel.footer(st, width=cols, help_=self.show_help)
        lines = [panel.render_ansi(row, pl.width) for row in rows]
        # Последнее диагностическое сообщение — под подвалом: в окне оно
        # идёт полосой по кадру (`strip.DiagStrip`).
        if self.diag.last:
            lines.append(panel.render_ansi(
                [panel.mk((self.diag.last_stamp + ' ', 'diag'),
                          (self.diag.last, 'diag'))], pl.width))
        return lines

    def _splash_lines(self, cols: int, rows: int) -> list:
        """Заставка: рисунок из эталонного потока и приветственный текст.

        Рисунок задаётся конфигурацией (`[display] splash`), по умолчанию
        птица.  Закрывается при первом вводе или первом байте от ЭВМ.
        Возвращает ровно `rows` строк: заставка не должна залезать на
        подвал.

        Рисунок **кэшируется**: он разбирается из потока команд заново на
        каждый кадр, а это десятки тысяч команд и полсекунды работы —
        при открытом окне ввод с клавиатуры заметно отставал.  Ключ —
        картинка и геометрия окна; при перерисовке окна ключ меняется и
        рисунок строится заново.
        """
        pic = startup.picture_path(self.config.splash, prefer_stream=True)
        art_h = max(0, min(rows - 8, int(rows * 0.6)))
        key = (str(pic) if pic else '', art_h)
        if key != self._splash_key:
            art = (_render_picture(pic, cols, art_h).split('\n')
                   if pic and art_h else [])
            self._splash_art = art[:art_h] + [''] * max(0, art_h - len(art))
            self._splash_key = key
        art_lines = list(self._splash_art)
        text = startup.build(
            max(20, cols - 2), state=self._link_state(),
            summary=self.config.summary(), hint=startup.hint_text()).split('\n')
        return (art_lines + text)[:rows]

    def _splash_text(self, cols: int) -> str:
        """Заставка одной строкой (для проверок и снимков экрана)."""
        return '\n'.join(self._splash_lines(cols, self.layout()[1]))

    def _link_state(self) -> str:
        """Строка состояния линии для начального экрана и подвала."""
        return self._connect_note or self.driver.name

    #: Приветственное сообщение на начальном экране.
    WELCOME = ('Электроника МС 7401 — растровый графический терминал. '
               'Готов к работе с ЭВМ.')
    # ── ввод ────────────────────────────────────────────────────────────────

    def handle_key(self, key: str) -> None:
        """Нажатие клавиши.

        Раскладка как на 15ИЭ: служебные действия терминала висят на
        **функциональных** клавишах (F5…F10), а буквы свободны — иначе
        текст не набрать.  Поэтому `x`, `e`, `z`, `L`, `S` уходят на
        ЭВМ как обычные знаки.
        """
        vp = self.renderer.vp
        cols, rows = self.layout()
        step = max(8, vp.scale * 8)

        # ── канонические формы нажатий ──
        # `term.read_key` отдаёт имена (`'\r'`, `'KEY_BACKSPACE'`), здесь
        # принимаются и каретные обозначения — формы не должны расходиться.
        if key in ('^M', '^J'):
            key = '\r'                     # ВК/ПС в каретной нотации
        elif key in ('^H', '\x08', '\x7f'):
            key = 'KEY_BACKSPACE'          # Backspace, а не переключатель Ctrl
        elif key == '^]':
            # УПР: `Ctrl` держится между нажатиями, как на настоящей
            # клавиатуре.  Своя комбинация: Backspace удаляет знак.
            self.ctrl = not self.ctrl
            self.message = ('Ctrl удерживается' if self.ctrl
                            else 'Ctrl отпущен')
            self.last_key = 'УПР'
            return

        if key in ('^C', 'EOF'):
            self.running = False
            return

        # ── служебные клавиши терминала (клавиатура 15ВВВ-97-006) ──
        if key == KEY_MODE:                      # F9 — СЕТЬ
            self.session.toggle_mode()
            self.message = 'сеть: ' + self.session.panel()
        elif key == KEY_SEND:                    # F10 — ПЕРЕДАЧА
            self._send()
        elif key == KEY_CLEAR:                   # F5 — ОЧИСТКА
            self._clear_screen()
        elif key == KEY_ECHO:                    # F6 — ЭХО
            self.session.toggle_echo()
            self.message = ('эхо включено' if self.session.echo
                            else 'эхо выключено — пароль не печатается')
        elif key == KEY_CMDSET:                  # F8 — НАБОР
            want = 'koi8r' if self.session.charset_set == 'n2' else 'n2'
            self.session.set_charset(want)
            self.emu.state.font = want
            self.message = 'набор: ' + self.session.panel()

        # ── экран и ввод ──
        # Стрелка указывает, куда движется кадр: `vp.x`/`vp.y` — смещение
        # видимой области слева/сверху, поэтому кадр вправо — это `x` вниз,
        # кадр вверх — это `y` вверх.  Раньше ←/→ шли в обратную сторону
        # относительно ↑/↓.
        elif key == 'KEY_UP':                    # кадр вверх
            vp.y = min(self.emu.fb.height, vp.y + step)
        elif key == 'KEY_DOWN':                  # кадр вниз
            vp.y = max(0, vp.y - step)
        elif key == 'KEY_LEFT':                  # кадр влево
            vp.x = min(self.emu.fb.width, vp.x + step)
        elif key == 'KEY_RIGHT':                 # кадр вправо
            vp.x = max(0, vp.x - step)
        elif key == 'KEY_PAGEUP':                # ЭКРАН↑
            self._scroll_screen(-1)
        elif key == 'KEY_PAGEDOWN':              # ЭКРАН↓
            self._scroll_screen(1)
        elif key == 'KEY_BACKSPACE' or key == '\x7f':
            self.input_line = self.input_line[:-1]
            if self.session.echo and self.echo_buf \
                    and not self.echo_buf.endswith('\n'):
                self.echo_buf = self.echo_buf[:-1]
        elif key in ('\r', '\n', 'KEY_ENTER'):       # ПРПС
            self._submit()
        elif key == 'ESC':
            self.input_line = ''
            self.message = 'ввод сброшен'

        # ── служебные клавиши эмулятора: только с `Ctrl` ──
        # Проверка идёт **до** ввода текста, иначе буква ушла бы в
        # строку: на настоящем терминале этих клавиш нет, и держать
        # их на буквах без `Ctrl` нельзя.
        elif key in self.EMULATOR_KEYS and self.ctrl:
            self._emulator_key(key, cols, rows)

        # ── мышь: световое перо ──
        elif key and key.startswith('\x1b[<') and self.pen.enabled:
            self.pen.feed_mouse(self._mouse_for_frame(key), cols=cols,
                                rows=rows, scale=vp.scale, vx=vp.x, vy=vp.y)
            for ev in self.pen.take_events():
                self.driver.send_keys(ev.encode().decode('ascii'))

        # ── сочетания Ctrl-буква ──
        # Настоящие сочетания приходят управляющим кодом (`'^Q'` — это
        # Ctrl-q): действие выполняется сразу, без удержания `Ctrl`.
        elif len(key) == 2 and key.startswith('^') and key[1].isalpha():
            self._emulator_key(key[1].lower(), cols, rows)

        # ── ввод текста ──
        elif key and len(key) == 1 and key.isprintable():
            # Свободный ввод, как на 15ИЭ: в режиме С ЭВМ знак уходит в
            # линию немедленно, в АВТОНОМНО копится в буфер до ПЕРЕДАЧА.
            self.input_line += key
            if self.session.echo:
                self.echo_buf += key      # ЭХО: набранное видно на экране
            out = self.session.type_key(key)
            if out:
                self._send_raw(out)
            self.splash = False
        else:
            return
        self.last_key = self._last_label(key)

    def _last_label(self, key: str) -> str:
        """«ПОСЛ:» — код выданного знака и сам знак (`≡144 'd'`).

        Код — восьмеричный КОИ-8Р (`≡NNN`, как в описании терминала);
        для служебных клавиш показывается их имя.
        """
        if len(key) == 1 and key.isprintable():
            code = key.encode('koi8-r', errors='replace')[0]
            return f'≡{code:o} {key!r}'
        return SESSION_KEYS.get(key, key)[:12]

    #: SGR-последовательность мыши: `\x1b[<b;x;yM` / `\x1b[<b;x;ym`.
    _MOUSE_RE = re.compile(r'^(\x1b\[<)(\d+);(\d+);(\d+)([Mm])$')

    def _mouse_for_frame(self, key: str) -> str:
        """Пересчитать ряд мыши: от верха окна → от начала кадра.

        `feed_mouse` считает, что кадр начинается с первого ряда, а в
        окне он ниже заголовка и текстовых строк ЭВМ.  Без поправки перо
        попадало не туда, куда указал пользователь.
        """
        m = self._MOUSE_RE.match(key)
        if not m:
            return key
        head, b, col, row, tail = m.groups()
        row = max(1, int(row) - self._frame_row_offset())
        return f'{head}{b};{col};{row}{tail}'

    def _frame_row_offset(self) -> int:
        """Сколько рядов окна над кадром: заголовок и текст ЭВМ."""
        return (1 if self.config.title else 0) + self._body_offset

    #: Служебные клавиши эмулятора (окно просмотра, перо, сетка).
    #: Это не клавиши терминала: на настоящем терминале их нет.
    EMULATOR_KEYS = '+-0cmgprq'

    def _emulator_key(self, key: str, cols: int, rows: int) -> None:
        """Действие эмулятора над окном просмотра (с `Ctrl`)."""
        vp = self.renderer.vp
        if key == 'q':
            self.running = False
            return
        if key == '+':
            vp.scale = max(1, vp.scale - 1)
        elif key == '-':
            vp.scale = min(16, vp.scale + 1)
        elif key == '0':
            fb = self._visible_fb()
            if fb is not None:
                vp.fit(fb, cols, rows)
        elif key == 'c':
            fb = self._visible_fb()
            if fb is not None:
                vp.center(fb, cols, rows)
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
            sys.stdout.write(LightPen.disable_sequence() if not self.pen.enabled
                              else LightPen.enable_sequence())
        elif key == 'r':
            self.message = 'перерисовано'

    def _scroll_screen(self, direction: int) -> None:
        """`ЭКРАН↑` / `ЭКРАН↓` — листание окна просмотра, как на 15ИЭ."""
        vp = self.renderer.vp
        step = max(8, vp.scale * 8) * 4
        if direction < 0:
            vp.y = min(self.emu.fb.height, vp.y + step)
        else:
            vp.y = max(0, vp.y - step)

    def _clear_screen(self) -> None:
        """`ОЧИСТКА` (F5): экран — в историю, кадр — в начало.

        Прежний кадр сохраняется: к нему возвращает `ЭКРАН↑`.
        """
        shown = self._visible_fb()
        if shown is not None:
            self._history.append(shown)
        out = self.session.clear_command()
        if out:
            self._send_raw(out)
        else:
            self.emu.execute(self.emu.parser.parse(b'S\x55'))
        self._best_fb = None
        self._best_n = 0
        self.message = 'экран очищен'


    def _send(self) -> None:
        """SEND: передать накопленное на ЭВМ, буфер очистить."""
        text = self.input_line
        self.input_line = ''
        if not text and not self.session.buffer:
            self.message = 'нечего отправлять'
            return
        if self.session.mode == MODE_HOST:
            # В режиме С ЭВМ знаки ушли по мере набора — осталось
            # передать только конец строки.
            self.session.reset_buffer()
            self._send_raw(bytes((self.eol,)))
            self.message = 'строка передана'
            return
        # В АВТОНОМНО знаки уже лежат в буфере по мере набора, добирать
        # их из строки ввода нельзя — иначе строка уйдёт дважды.
        out = self.session.send(line_end=self.eol)
        self.session.reset_buffer()
        if out:
            self._send_raw(out)
            self.message = f'отправлено {len(out)} Б'

    def _submit(self) -> None:
        """`ПРПС` (Enter): конец строки.

        В режиме С ЭВМ знаки уже ушли по одному, поэтому передаётся только
        завершитель.  В АВТОНОМНО строка дописывается в буфер и уходит по
        ПЕРЕДАЧА (SEND) — как на 15ИЭ.
        """
        text = self.input_line
        self.input_line = ''
        if self.session.echo:
            self.echo_buf += '\n'         # ПЕРЕВОД СТРОКИ: строка на экране
        if not text and self.session.mode == MODE_HOST:
            # Пустая строка — тоже команда (например, пустой ввод в ОС).
            self._send_raw(bytes((self.eol,)))
            self.message = 'пустая строка передана'
            return
        if not text:
            return
        if self.session.mode == MODE_HOST:
            self.session.reset_buffer()
            self._send_raw(bytes((self.eol,)))
            self.message = f'передана строка ({len(text)} знаков)'
            return
        # АВТОНОМНО: знаки уже в буфере, повторно их добавлять нельзя.
        self.message = ('строка в буфере — передать клавишей S '
                        '(ПЕРЕДАЧА)')

    def _send_raw(self, data: bytes) -> None:
        """Отдать байты линии.

        `send_keys` ждёт строку latin-1: каждый байт проходит насквозь
        (latin-1 → koi8-r), поэтому кодировку набираемого набора знаков
        менять здесь нельзя.
        """
        if data:
            self.driver.send_keys(data.decode('latin-1'))

    # ── главный цикл ────────────────────────────────────────────────────────

    def run(self) -> int:
        cols, rows = self.layout()
        self.renderer.vp.fit(self.emu.fb, cols, rows)
        self._enable_gfx()
        sys.stdout.write(ALT_SCREEN_ON + CLEAR + HIDE_CURSOR
                         + LightPen.enable_sequence())
        # Экран перерисовывается только по изменению: полная отрисовка на
        # каждом обороте делала приём клавиш очень медленным.
        dirty = True
        clock_at = 0.0
        try:
            with RawTerminal():
                self._probe_gfx()
                while self.running:
                    if dirty:
                        self.draw()
                        dirty = False
                    data = self.driver.poll(0.05)
                    if data:
                        self.feed(data)
                        dirty = True
                        continue
                    key = read_key(timeout=0.08)
                    if key:
                        self.handle_key(key)
                        dirty = True
                    elif not self._got_data:
                        # Часы на начальном экране идут вживую — раз в
                        # секунду, а не на каждом обороте.
                        now = time.monotonic()
                        if now - clock_at >= 1.0:
                            clock_at = now
                            dirty = True
        except KeyboardInterrupt:
            pass
        finally:
            if self.gfx is not None:
                self.gfx.clear()
            sys.stdout.write(LightPen.disable_sequence() + SHOW_CURSOR
                             + ALT_SCREEN_OFF + RESET + '\n')
            sys.stdout.flush()
            self.driver.close()
        return 0


# ── запуск из командной строки ──────────────────────────────────────────────

def builtin_stream() -> bytes:
    """Пробный поток команд, собираемый прямо в коде.

    Нужен, чтобы `mc7401` сразу после установки показывал осмысленный
    кадр: эталонные потоки лежат в каталоге исходников, а в
    `site-packages` их нет.  Формат записи тот же, что у `graf.pas`:
    команды разделены `ВК`, кадр обрамляется маркерами ВУК.
    """
    from .constants import AR2, S0, S1, VK

    def rec(text) -> bytes:
        """Одна запись потока: данные + разделитель `ВК`."""
        if isinstance(text, bytes):
            return text + bytes((VK,))
        return text.encode('koi8-r', errors='replace') + bytes((VK,))

    def blk(data: bytes) -> bytes:
        """Обрамить данные блоком ВУК."""
        return (b'/' + bytes((VK,)) + b'z' + b'0' * 19 + data
                + bytes((AR2,)) + b'\xf5  '          # АР2 'У  '
                + bytes((AR2,)) + b'F~' + bytes((VK,)))

    out = bytearray()
    out += blk(rec('J1') + rec('S'))
    out += rec('] 8 1 551 398')                        # рамка
    for n in range(1, 8):                              # образцы типов линий
        y = 40 + n * 24
        out += rec(f'4 40 {y}') + rec(f'7 {n} 400 {y}')
    out += rec('4 60 180') + rec('> 2 60')             # окружность
    out += rec('4 300 180') + rec('> 1 60')
    out += rec('] 420 120 520 240')                    # прямоугольник
    out += rec(f'4 60 300') + rec('9\x01') + rec(b';\x00 ' + b'\x70'
           + 'ПРОБА ЭМУЛЯТОРА'.encode('koi8-r'))
    out += blk(rec('4 60 320') + rec('9\x01')
               + rec(b';\x00 ' + b'\x70'
                     + 'ДИАЛОГОВЫЙ РЕЖИМ'.encode('koi8-r')))
    return bytes(out)


class BuiltinDriver(Driver):
    """Пробный поток из :func:`builtin_stream` — работает всегда."""

    name = 'проба'

    def __init__(self) -> None:
        self.inner = FileDriver.__new__(FileDriver)
        self.inner.path = Path('<встроенный>')
        self.inner.data = builtin_stream()
        self.inner.pos = 0

    def poll(self, timeout: float) -> bytes:
        return self.inner.poll(timeout)

    def send_keys(self, text: str) -> None:
        log.info('пробный поток: ввод %r не требуется', text)


def _default_stream() -> Optional[Path]:
    """Найти эталонный поток для запуска без аргументов.

    Установленный пакет лежит в `site-packages`, где каталога `golden/`
    нет, поэтому ищем в нескольких местах: рядом с исходниками, в текущем
    каталоге и в каталоге пользователя.
    """
    names = ('ГРА', 'СЕТ', 'ЧАС', 'ЛИН')
    roots = [Path(__file__).resolve().parent.parent, Path.cwd(),
             Path.home() / '.local' / 'share' / 'mc7401']
    for root in roots:
        for name in names:
            for cand in (root / 'golden' / 'streams' / f'{name}.bin',
                         root / 'streams' / f'{name}.bin',
                         root / f'{name}.bin'):
                if cand.exists():
                    return cand
    return None


def _no_stream_hint() -> str:
    return (
        'Не найдено ни одного эталонного потока команд.\n'
        'Укажите файл или подключитесь к ЭВМ:\n'
        '  mc7401 golden/streams/ГРА.bin      — файл потока\n'
        '  mc7401 --link 127.0.0.1:4202       — линия ДКС БЭСМ-6\n'
        'Потоки создаются командой:\n'
        '  python3 tools/gen_golden.py         — из golden/graf.pas')


def run_tui(path: Optional[Path] = None, *, endpoint: Optional[str] = None,
            demo: bool = False, mode: Optional[str] = None,
            grid: bool = False, pen: bool = False,
            config: Optional[Config] = None,
            graphics: Optional[str] = None) -> int:
    """Запустить фронтенд.

    Без файла и без `--link` открывается интерактивная сессия с
    БЭСМ-6 по каналу ДКС (порты 4202-4223).  Если ни одна линия не
    доступна, печатается объяснение и возвращается код 2 — молча
    подменять её эталонной картинкой нельзя, это вводит в заблуждение.
    """
    cfg = config or Config.default()
    mode = mode or cfg.display_mode
    if endpoint is None and path is None and not demo:
        endpoint = cfg.endpoint

    if path is not None:
        # Явно указан файл — обычный разбор без диалога с ЭВМ.
        driver: Driver = FileDriver(path)
    elif demo:
        p = _default_stream()
        driver = ScriptedDriver(p) if p else BuiltinDriver()
    else:
        # По умолчанию — интерактивная сессия с БЭСМ-6 по каналу ДКС.
        try:
            driver = LiveDriver(endpoint or DEFAULT_ENDPOINT)
        except LinkRefused as exc:
            print(str(exc), file=sys.stderr)
            return 2

    app = TerminalApp(driver, mode=mode, grid=grid, config=cfg,
                       graphics=graphics or 'auto')
    if pen:
        app.pen.enabled = True
    app._connect_note = getattr(driver, 'endpoint', '') or ''
    if demo:
        app.status_extra = 'сценарий нажатий: ЛИН 3 ВЫХ'
    return app.run()


__all__ = ['TerminalApp', 'Driver', 'FileDriver', 'LiveDriver',
           'ScriptedDriver', 'run_tui', 'HELP']