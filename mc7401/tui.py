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

from datetime import datetime

from .constants import SCREEN_HEIGHT, SCREEN_WIDTH
from .emulator import TerminalEmulator
from . import startup
from .config import Config
from .pen import LightPen
from .session import Session

#: Название модели терминала — показывается в заголовке.
#: Гид-символ ОС Диспак (ETX): после подключения к линии ДКС
#: его надо передать, иначе приглашения «ЭВМ-3,ТNNN» не будет.
ETX = 0o003
#: Символ конца строки, который посылается терминалом по линии ДКС.
#: Именно он инициирует сеанс: после `LF` ОС Диспак отвечает
#: приглашением «ЭВМ-3,ТNNN» (проверено на линии 4203).
LF = 0o012

MODEL = 'Электроника МС 7401'

#: Линия ДКС, к которой подключается эмулятор по умолчанию.
#: ЭВМ слушает порты 4202-4223 (`set ttyN dks,raw` в SIMH).
DEFAULT_ENDPOINT = '127.0.0.1:4202-4223'


def title_line() -> str:
    """Заголовок: модель терминала, разрешение, время."""
    now = datetime.now().strftime('%d.%m.%Y %H:%M:%S')
    return (f'  {MODEL}  {SCREEN_WIDTH}×{SCREEN_HEIGHT}  '
            f'{now}')
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


class LinkRefused(Exception):
    """Линия ДКС не открылась; текст — готовое объяснение для пользователя."""


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
        # Символ конца строки: после подключения его нужно передать,
        # иначе ОС Диспак не отвечает приглашением «ЭВМ-3,ТNNN».
        # Гид `ETX` посылаем тоже — так поступает
        # `simh/BESM6/scripts/dks_tty.py`.
        try:
            self.link.send(bytes((LF, ETX)))
        except OSError:
            pass

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
                 grid: bool = False, intro: str = '',
                 config: Optional[Config] = None) -> None:
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
        #: Накопленный текст, присланный ЭВМ (приглашения ОС Диспак).
        self._text_log: list[str] = []
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
        #: Данные получены? до них показывается начальный экран.
        self._got_data = False
        #: Последняя нажатая клавиша — строка «ПОСЛ:» подвала.
        self.last_key = ''
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
            self._text_log.extend(
                data.decode('koi8-r', errors='replace'))
            return
        self._got_data = True
        self.splash = False
        self.reader.feed(data)
        for ev in self.reader.pop():
            if ev.kind is EventKind.COMMAND:
                self.emu.execute(self.emu.parser.parse(ev.data))
            else:
                self.emu.handle_event(ev)

    def _clean_text(self) -> str:
        """Текст ОС без управляющих последовательностей, в одну строку."""
        out = ''.join(self._text_log)
        out = ''.join(ch for ch in out
                      if ch.isprintable() or ch in ' \t')
        return ' '.join(out.split())

    # ── рисование ───────────────────────────────────────────────────────────

    def layout(self) -> tuple[int, int]:
        """Столбцы и строки под изображение.

        Сверху — заголовок с моделью и временем, снизу — три строки
        панели: состояние, подсказка и строка ввода.
        """
        cols, rows = terminal_size()
        n = 9 + (1 if self.config.title else 0)
        return cols, max(3, rows - n)

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
        if not self._got_data:
            body = startup.build(
                cols, state=self._link_state(), summary=self.config.summary(),
                hint=startup.hint_text())
            body = body.replace('\n', ' \n') if False else body
        out = [HOME]
        if self.config.title:
            out.append(title_line() + '\n')
        if self.splash:
            body = self._splash_text(cols)
        out.append(body + '\n')
        for line in self._service_panel(cols):
            out.append(bar(line, cols) + '\n')
        sys.stdout.write(''.join(out))
        sys.stdout.flush()

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


    # ── подвал (как в 15ИЭ-00-013) ──────────────────────────────────

    #: Ширина колонки подвала: 4��олонки по 17 знаков и разделители
    #: « | » — разделители стоят на одних позициях во всех рядах.
    COL = 17

    def _row(self, *fields) -> str:
        return ' | '.join(f.ljust(self.COL) for f in fields).rstrip()

    def _service_panel(self, cols: int) -> list:
        """Сегмент состояния, черта и справочные ряды — как в 15ИЭ."""
        from .chars import SET_LABELS
        ses = self.session
        host = ses.mode == 'host'
        lay = ('ВЫКЛ' if not ses.layout else
               ('ФОН' if ses.layout == 'phonetic' else 'ПОЗ'))
        buf = str(len(ses.buffer)) if ses.buffer else 'ПУСТО'
        state1 = self._row(
            'РЕЖИМ=' + ('С ЭВМ' if host else 'АВТОНОМНО'),
            'НАБОР=' + SET_LABELS.get(ses.charset_set, ses.charset_set),
            'ЛИНИЯ=' + self._link_state().upper()[:self.COL],
            'РАСК=' + lay)
        state2 = self._row(
            'ЭХО=' + ('ВКЛ' if ses.echo else 'ВЫКЛ'),
            'ПЕРО=' + ('ВКЛ' if self.pen.enabled else 'ВЫКЛ'),
            'БУФЕР=' + buf,
            'СЕТКА=' + ('ВКЛ' if self.grid else 'ВЫКЛ'))
        state3 = self._row(
            'МАСШТАБ=1:' + str(self.renderer.vp.scale),
            'ТОЧЕК=' + str(self.emu.fb.count_set()),
            'ПОСЛ: ' + (self.last_key or ''),
            'ВВОД=' + (self.input_line or ''))
        keys1 = self._row('ВК=Bksp', 'ТАБ=Tab', 'ЗВН=Ctrl-G', 'ПРПС=Enter')
        keys2 = self._row('ESC=Esc', 'КУРСОР=Стрелки', 'СДВИГ=PageUp/Dn',
                          'МАСШТАБ=+/−')
        keys3 = self._row('ВПИСАТЬ=0', 'ЦЕНТР=c', 'ЦВЕТ=m', 'СЕТКА=g')
        keys4 = self._row('РЕЖИМ=L', 'ПЕРЕДАЧА=S', 'ЭХО=e', 'ОЧИСТКА=x')
        keys5 = self._row('НАБОР=z', 'ПЕРО=p', 'ВЫХОД=q', '')
        width = min(cols, self.COLS_N * self.COL + 3 * (self.COLS_N - 1))
        return [state1, state2, state3, '-' * width,
                keys1, keys2, keys3, keys4, keys5]

    COLS_N = 4

    def _splash_text(self, cols: int) -> str:
        """Заставка: рисунок из `golden/render` плюс приветственный текст.

        Рисунок задаётся конфигурацией (`[display] splash`), по умолчанию
        птица.  Закрывается при первом вводе или первом байте от ЭВМ.
        """
        rows = self.layout()[1]
        pic = startup.picture_path(self.config.splash, prefer_stream=True)
        art = _render_picture(pic, cols, rows - 8) if pic else ''
        art_lines = art.split('\n') if art else []
        text = startup.build(
            max(20, cols - 2), state=self._link_state(),
            summary=self.config.summary(), hint=startup.hint_text()).split('\n')
        # Заставка должна строго уместиться в отведённые строки, иначе
        # текст наезжает на подвал.
        room = rows - len(art_lines)
        lines = art_lines + text[:max(0, room)]
        return '\n'.join(lines[:rows])

    def _link_state(self) -> str:
        """Строка состояния линии для начального экрана и подвала."""
        return self._connect_note or self.driver.name

    #: Приветственное сообщение на начальном экране.
    WELCOME = ('Электроника МС 7401 — растровый графический терминал. '
               'Готов к работе с ЭВМ.')
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
            # ОЧИСТКА: в режиме С ЭВМ команда уходит на ЭВМ,
            # иначе чистим локально.
            out = self.session.clear_command()
            if out:
                self.driver.send_keys(out.decode('latin-1'))
            else:
                self.emu.execute(self.emu.parser.parse(b'S\x55'))
            self.message = 'экран очищен'
        elif key == 'L':          # режим терминала
            self.session.toggle_mode()
            self.message = 'режим: ' + self.session.panel()
        elif key in ('S', 'KEY_F2'):   # SEND
            self._send()
        elif key == 'e':          # ЭХО
            self.session.toggle_echo()
            self.message = ('эхо включено' if self.session.echo
                            else 'эхо выключено — пароль не печатается')
        elif key == 'z':          # набор: Н2 (КОИ-7) / КОИ-8Р
            want = 'koi8r' if self.session.charset_set == 'n2' else 'n2'
            self.session.set_charset(want)
            self.emu.state.font = want
            self.message = 'набор: ' + self.session.panel()
        elif key == 'KEY_BACKSPACE' or key == '\x7f':
            self.input_line = self.input_line[:-1]
        elif key == '\r' or key == '\n' or key == 'KEY_ENTER':
            self._submit()
        elif key == 'ESC':
            self.input_line = ''
            self.message = 'ввод сброшен'
        elif key and len(key) == 1 and key.isprintable():
            # Свободный ввод, как на 15ИЭ: в режиме С ЭВМ знак уходит в
            # линию немедленно, в АВТОНОМНО копится в буфер до ПЕРЕДАЧА.
            self.input_line += key
            out = self.session.type_key(key)
            if out:
                self.driver.send_keys(out.decode('latin-1'))
            self.splash = False
        else:
            return
        self.last_key = key if len(key) <= 12 else key[:12]
        vp.clamp(self.emu.fb, cols, rows)

    def _send(self) -> None:
        """SEND: передать накопленное на ЭВМ, буфер очистить."""
        text = self.input_line
        self.input_line = ''
        if not text and not self.session.buffer:
            self.message = 'нечего отправлять'
            return
        if text:
            self.session.type_line(text)
        out = self.session.send()
        self.session.reset_buffer()
        if out:
            self.driver.send_keys(out.decode('latin-1'))
            self.message = f'отправлено {len(out)} Б'

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
                    if not self._got_data:
                        # Часы на начальном экране идут вживую.
                        self.draw()
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
            config: Optional[Config] = None) -> int:
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

    app = TerminalApp(driver, mode=mode, grid=grid, config=cfg)
    if pen:
        app.pen.enabled = True
    app._connect_note = getattr(driver, 'endpoint', '') or ''
    if demo:
        app.status_extra = 'сценарий нажатий: ЛИН 3 ВЫХ'
    return app.run()


__all__ = ['TerminalApp', 'Driver', 'FileDriver', 'LiveDriver',
           'ScriptedDriver', 'run_tui', 'HELP']