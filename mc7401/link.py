"""
link.py — связь эмулятора МС 7401 с БЭСМ-6 по каналу ДКС.

На стороне SIMH линии telnet работают как мультиплексор
(`TMXR tty_desc` в `BESM6/besm6_tty.c`), а режим ДКС включается командой
`set ttyN dks,raw` (`tty_set_dks`).  Значит, **ЭВМ слушает порт, а
терминал к нему подключается** — как в `ie15/ie15emu/link.py`.

Протокол ДКС («Электроника-60» со стороны БЭСМ-6, `besm6_dks.c`):

* ОС Диспак опрашивает каналы КРК, находит терминал и отправляет
  поток байтов в линию; терминал отвечает символами с клавиатуры;
* канал завершается запросом `ПРП 6` и `абфосh+2 ≠ 0`;
* между сессиями SIMH печатает `DKS: line busy` или
  `DKS: no free terminal` — такой порт при переборе пропускается.

Направление данных: ЭВМ → терминал — байты потока команд графики;
терминал → ЭВМ — ввод с клавиатуры (обычные символы, ВК = 0o125
завершает строку; терминал ДКС сам реализует локальное эхо и
редактирование строки, см. комментарий в `besm6_dks.c`).

    python -m mc7401 --link 127.0.0.1:4202 --png кадр.png
    python -m mc7401 --listen 127.0.0.1:4202        # обратный режим
"""

from __future__ import annotations

import logging
import re
import socket
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from . import chars
from .constants import ETX
from .emulator import TerminalEmulator
from .reader import CommandStreamReader

log = logging.getLogger('mc7401.link')


class LinkError(RuntimeError):
    """Ошибка связи: нет порта, линия занята, обрыв."""


# ── чем SIMH отвечает на вход в уже занятую линию (tmxr/ДКС) ───────────────
BUSY_MARKERS = (
    b'connection busy', b'connection not available',
    b'line busy', b'no free terminal',
    # мост сообщает, что не смог дойти до линии (UTF-8)
    'линия недоступна'.encode('utf-8'),
)

#: Спецификации портов ДКС по умолчанию (как в `ie15`, `dispak`).
DEFAULT_PORTS = '4202-4223'

#: telnet-последовательности RFC 854, которые надо отбросить.
IAC, DONT, DO, WONT, WILL, SB, SE = 0xFF, 254, 253, 252, 251, 250, 240


def parse_port_spec(spec) -> list[int]:
    """Спецификация портов → упорядоченный список без повторов.

    «4202», «4202,4210», «4202-4223», «4202-4204,4210» — формат поля
    ``line`` в SIMH и ключа командной строки.
    """
    if isinstance(spec, int):
        specs = [str(spec)]
    elif isinstance(spec, (list, tuple)):
        specs = [str(s) for s in spec]
    else:
        specs = str(spec).split(',')
    ports: list[int] = []
    for part in specs:
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            a, _, b = part.partition('-')
            lo, hi = int(a), int(b)
            if lo > hi:
                raise LinkError(f'неверный диапазон портов: {part}')
            ports.extend(range(lo, hi + 1))
        else:
            ports.append(int(part))
    for p in ports:
        if not 0 < p < 65536:
            raise LinkError(f'порт вне диапазона: {p}')
    if not ports:
        raise LinkError('не задано ни одного порта')
    if len(ports) > 512:
        raise LinkError('слишком длинный список портов (максимум 512)')
    return list(dict.fromkeys(ports))


def busy_reply(data: bytes) -> bool:
    """Ответила ли линия, что она занята?"""
    return any(m in data.lower() for m in BUSY_MARKERS)


def spec_hint(ports: Iterable[int]) -> str:
    """«4202,4204-4206» из списка — компактнее длинных диапазонов."""
    ports = sorted(set(ports))
    if not ports:
        return 'нет'
    out: list[list[int]] = []
    run: Optional[list[int]] = None
    prev = None
    for p in ports:
        if prev is not None and p == prev + 1:
            run[1] = p
        else:
            if run:
                out.append(run)
            run = [p, p]
        prev = p
    if run:
        out.append(run)
    return ','.join(str(a) if a == b else f'{a}-{b}' for a, b in out)


def split_endpoint(spec: str) -> tuple[str, list[int]]:
    """«host:4202-4223» или «host» → (хост, порты)."""
    if ':' not in spec:
        return spec.strip(), parse_port_spec(DEFAULT_PORTS)
    host, _, ports = spec.rpartition(':')
    return host.strip(), parse_port_spec(ports)


class IacStripper:
    """Срезка telnet-IAC (RFC 854) из потока ЭВМ → терминал.

    Состояние между вызовами сохраняется: последовательность IAC может
    прийти двумя чтениями.  Сабnegoциация (`IAC SB … IAC SE`) отбрасывается
    целиком — до `IAC SE`, как бы ни была разбита по чтениям; `IAC IAC`
    внутри её тела — экранированный IAC, а не конец.
    """

    def __init__(self) -> None:
        self.state = 'data'

    def feed(self, data: bytes) -> bytes:
        out = bytearray()
        for b in data:
            if self.state == 'data':
                if b == IAC:
                    self.state = 'iac'
                else:
                    out.append(b)
            elif self.state == 'iac':
                if b in (WILL, WONT, DO, DONT):
                    self.state = 'opt'
                elif b == SB:
                    self.state = 'sb'
                else:
                    self.state = 'data'          # IAC IAC — эсквейп
            elif self.state == 'opt':
                self.state = 'data'              # код опции отброшен
            elif self.state == 'sb':
                # Тело сабnegoциации отбрасывается до `IAC SE`.
                if b == IAC:
                    self.state = 'sb_iac'
            elif self.state == 'sb_iac':
                # После IAC в теле: SE — конец, IAC IAC — эсквейп,
                # прочее — снова тело.
                self.state = 'data' if b == SE else 'sb'
        return bytes(out)


# ── канал связи ────────────────────────────────────────────────────────────

class EchoStrip:
    """Срезка эха линии ДКС: Э-60 всегда эхит отданные знаки.

    По эталону 15ИЭ (`ie15emu/session.py::_queue_echo` +
    `_strip_pending_echo`): эхо-ответ на отправленное срезается из
    потока приёма.  Иначе набранное показывается дважды (локальное эхо
    и эхо машины), а при выключенном ЭХО пароль виден в эхе машины.

    Эхо не байт-в-байт (проверено на живой линии): ПР возвращается
    ПС, ВК — тройкой «ВК ПРОБЕЛ ВК».
    """

    #: Как линия эхит отправленные управляющие знаки.
    ECHO_OF = {0x0D: b"\n", 0x08: b"\x08 \x08"}

    def __init__(self) -> None:
        self.pending = bytearray()

    def sent(self, data: bytes) -> None:
        """Запомнить, что вернёт линия в эхо на отданные байты."""
        for b in data:
            self.pending += self.ECHO_OF.get(b, bytes((b,)))

    def strip(self, data: bytes) -> bytes:
        """Убрать из `data` эхо-ответ; остаток — настоящий вывод ЭВМ.

        ПР и ПС при сверке неразличимы (линия эхит ПР как ПС).
        Сверка идёт по префиксу: совпавшие байты — эхо, первое
        несовпадение — вывод машины, он показывается целиком
        (как в 15ИЭ `_strip_pending_echo`: очередь сохраняется —
        эхо опоздавших знаков придёт позже и будет срезано).
        """
        if not self.pending:
            return data
        i, n = 0, len(data)
        pend = self.pending
        while i < n and pend:
            b = data[i]
            if b == pend[0] or (b in (0x0D, 0x0A) and pend[0] in (0x0D, 0x0A)):
                pend.pop(0)
                i += 1
            else:
                break
        return bytes(data[i:])


# ── баннер линии ────────────────────────────────────────────────────────────
#
# Строка состояния над кадром должна говорить, что за линия, и говорить
# ПРАВДУ: тип, номер терминала и режим берутся из баннера SIMH, который
# выдаёт обвязка стенда при подключении (его же разбирает эталон 15ИЭ —
# `ie15/ie15emu/session.py::_parse_banner`).  Без разбора в строке стояло
# постоянное «КВУ», и линия ДКС, объявленная баннером «Connected to DKS»,
# выглядела как клавиатурно-вычислительный узел.
#
# Тип линии: `Connected to DKS` → «ДКС», обычный tty → «КВУ».

# Баннер приходит кодами КОИ-7 в режиме `raw`: латиница лежит в
# `0x40…0x5E`, а русские знаки — в `0x60…0x7E` и выводятся заглавными.
# Поэтому «Connected to DKS» приходит на экране как «CОNNECTED TO DKS»:
# часть букв — латинские коды, часть — кириллические омоглифы, и поиск
# по сырым байтам не находит ни «Connected to DKS», ни «Encoding is RAW».
# Сначала баннер приводится к тексту **тем же** способом, каким его
# показывает знакогенератор (`chars.display_dks`), и уже по нему идут
# все образцы — с заглавными буквами, потому что линия их переводит.

#: «TTY DEVICE, LINE N» — номер терминала на линии.
BANNER_LINE_NO = re.compile(r'TTY\s+DEVICE,?\s*LINE\s+(\d+)', re.IGNORECASE)
#: Номер ЭВМ — **только** из явной формы «ЭВМ-3» или «ЭС-3».
#:
#: Раньше сюда входило и `БЭСМ`, и баннер эмулятора
#: «Connected to the БЭСМ-6 simulator TTY device, line 10» давал
#: `machine = 6` — то есть **модель** выдавалась за номер ЭВМ.  На живой
#: линии в строке состоянии стояло «ЭВМ=6», и это ни о чём не говорило.
#: Номер ЭВМ приходит из приветствия ОС («ЭВМ-3,Т017») — его разбирает
#: `OS_GREETING_MACHINE` ниже.
BANNER_MACHINE = re.compile(r'(?:ЭВМ|ЕС)\s*-?\s*(\d+)', re.IGNORECASE)
#: Режим линии: `Encoding is …`.
BANNER_MODE = (('raw', 'RAW'), ('utf-?8', 'UNICODE'),
               ('koi-?7', 'JCUKEN'), ('jcuken', 'JCUKEN'))
#: Кириллические омоглифы латинских букв: знакогенератор показывает на
#: месте латинской `N` кириллическую «Н», и наоборот — различить их по
#: одному байту нельзя.
_HOMOGLYPH_OF = {'A': 'А', 'B': 'В', 'C': 'С', 'E': 'Е', 'H': 'Н',
                 'K': 'К', 'M': 'М', 'O': 'О', 'P': 'Р', 'T': 'Т',
                 'X': 'Х', 'Y': 'У', 'd': 'Д'}


def _lookalikes(ch: str) -> str:
    """Какие знаки терминала могут стоять на месте латинской буквы.

    Линия ДКС в режиме `raw` шлёт латиницу **как есть**, а знакогенератор
    читает коды `0x60…0x7E` как кириллицу (`chars.display_dks`): строчная
    `c` приходит кодом 0x63 и показывается как «Ц», `o` — как «О», `n` —
    как «Н».  Заглавная `C` остаётся латинской (код 0x43).  Поэтому на
    месте латинской буквы терминал может показать и её саму (в любом
    регистре), и кириллическую подмену из строки `chars.RUS7`.
    """
    out = set()
    upper = ch.upper()
    if len(upper) == 1 and upper.isascii() and upper.isalpha():
        out.update((ch, upper, upper.lower()))
        look = _HOMOGLYPH_OF.get(upper)
        if look:
            out.add(look)
        code = ord(ch)
        if 0x60 <= code < 0x7E:            # строка кириллицы знакогенератора
            out.add(chars.RUS7[code - 0x60])
    return ''.join(sorted(out))


def _tolerant(pattern: str) -> str:
    """Образец, допускающий кириллические подмены вместо латинских.

    Свёртка «кириллица → латиница» для этого не годится: `Н` — это и
    латинская `N`, и кириллическая `H`, и по одному знаку их не различить.
    Поэтому каждая буква заменяется классом «эта буква, её регистры и все
    знаки, которые терминал рисует на её месте», и образец принимает
    любое из прочтений.  Escape-последовательности (`\\s`) переносятся
    как есть.
    """
    out = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == '\\' and i + 1 < len(pattern):
            out.append(pattern[i:i + 2])
            i += 2
            continue
        if ch.isascii() and ch.isalpha():
            out.append('[' + _lookalikes(ch) + ']')
        else:
            out.append(ch)
        i += 1
    return ''.join(out)


#: Номер ЭВМ в приветствии ОС Диспак: «ЭВМ-3,Т017» → 3.
#:
#: Линия ДКС шлёт приветствие кодами КОИ-7, и кириллица приходит
#: **латинскими омоглифами**: на месте «ЭВМ» приходят байты `7C 42 4D`,
#: то есть `ЭBM` — «Э» кириллическая (0x7C), а «В» и «М» латинские.
#: Образец терпит омоглифы (`_tolerant`), иначе номер ЭВМ с живой линии
#: не читался бы вовсе: `ЭBM` не совпадает с `ЭВМ`.
#: В образце кириллица стоит там, где омоглифа нет, и **латиницей** —
#: где он возможен: `_tolerant` расширяет только ASCII-буквы, поэтому
#: «ВМ» и «Т» записаны как `BM` и `T`.  «Э» и «ЕС» — без омоглифов.
OS_GREETING_MACHINE = re.compile(
    _tolerant(r'ЭBM\s*-?\s*(\d+)\s*[,;]') + '|'
    + _tolerant(r'ЕС\s*-?\s*(\d+)\s*[,;]'), re.IGNORECASE)
#: Номер терминала там же: «ЭВМ-3,Т017» → 17.  Кириллическая «Т» по
#: линии приходит латинской `T` (0x54) — тоже омоглиф.
OS_GREETING_TERMINAL = re.compile(
    _tolerant(r'[,;]\s*T\s*-?\s*(\d+)'), re.IGNORECASE)

#: Маркер линии ДКС в баннере.  Терпим к омоглифам: линия шлёт приветствие
#: кодами КОИ-7 и переводит латиницу в заглавные, так что на экране это
#: «CОNNЕCTED TO DKS».
BANNER_DKS = re.compile(_tolerant(r'Connected\s+to\s+DKS'), re.IGNORECASE)


@dataclass
class LineInfo:
    """Что баннер линии говорит о соединении."""

    #: «ДКС» — линия объявлена как ДКС, «КВУ» — обычный tty, `''` — баннера нет.
    kind: str = ''
    #: Номер ЭВМ из приветствия ДКС («ЭВМ-3» → 3), 0 — неизвестно.
    machine: int = 0
    #: Номер терминала на линии (`TTY DEVICE, LINE N`), 0 — неизвестно.
    terminal: int = 0
    #: Режим линии из `Encoding is …`: `RAW` / `UNICODE` / `JCUKEN`.
    wire: str = ''
    #: Баннер получили? без него строка состояния показывает
    #: «ПРОВЕРЬ ЛИНИЮ!» — линия не «отошла».
    seen: bool = False
    #: Сам баннер — для показа в подвале по запросу.
    raw: str = ''

    @property
    def label(self) -> str:
        return self.kind or 'НЕТ'


def banner_text(data: bytes) -> str:
    """Баннер как текст — теми же знаками, какими его показывает терминал.

    Линия ДКС в режиме `raw` шлёт коды КОИ-7 и переводит латиницу в
    заглавные; часть букв приходит кириллическими кодами — омоглифами
    знакогенератора (`C`/`Н`/`Е`/`Т`/`Д`).  Поэтому «Connected to DKS»
    приходит как «CОNNЕCTED TO DKS» и по сырым байтам не находится
    никогда.  Сначала коды переводятся в знаки (`chars.display_dks`).
    """
    out = []
    for b in data:
        if b in (0xFF, 0xFE, 0xFB, 0xFC, 0xFD, 0xFA, 0xF1, 0xF0):
            continue                               # telnet-служебные коды
        out.append(chars.display_dks(b) if b else '')
    return ''.join(out)


#: Больше этого приветствие не растёт: за такой длиной это уже не
#: заголовок соединения, а вывод ОС, и его надо показать терминалу.
BANNER_MAX = 2048

#: Начала служебных строк приветствия (по эталону 15ИЭ `BANNER_HEADS`).
#: По ним баннер отличается от вывода ОС: если первая же строка не
#: похожа на приветствие, данные идут в терминал как есть.
BANNER_HEADS = ('connected', 'encoding', 'wru', 'prompt', 'from',
                'sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat',
                'tty device', 'break to')
#: Те же начала, но терпимые к омоглифам: образцы, а не строки.
_BANNER_HEADS_RE = tuple(re.compile(_tolerant(h), re.IGNORECASE)
                         for h in BANNER_HEADS)


def is_banner_line(text: str) -> bool:
    """Похожа ли строка на строку приветствия линии."""
    return any(r.match(text.strip()) for r in _BANNER_HEADS_RE)


def banner_tail(buf: bytes) -> tuple[bytes, str]:
    """Разделить накопленное на приветствие и хвост: `(хвост, судьба)`.

    Судьба: ``'wait'`` — приветствие ещё идёт, ``'end'`` — кончилось,
    ``'text'`` — это не приветствие вовсе, и всё накопленное идёт в
    терминал.

    Конец приветствия — строка с маркером ДКС либо пустая строка, за
    которой идёт **не** служебная строка: на стенде приветствие разделено
    пустыми строками внутри («Connected to the … line 3», пусто,
    «Encoding is RAW»), и пустая строка сама по себе ни о чём не
    говорит.  Первая не-служебная строка означает, что приветствия не
    было вовсе.

    Строки читаются как знаки терминала (`banner_text`), а образцы терпимы
    к кириллическим подменам латиницы (`_tolerant`).
    """
    seen = False
    lines: list[tuple[int, int]] = []          # (начало, конец) строк
    i = 0
    while True:
        j = buf.find(b'\n', i)
        lines.append((i, len(buf) if j < 0 else j + 1))
        if j < 0:
            break
        i = j + 1

    for n, (start, stop) in enumerate(lines):
        text = banner_text(buf[start:stop]).strip()
        if not text:
            continue
        if BANNER_DKS.search(text):
            return buf[stop:], 'end'           # маркер ДКС — конец
        if not is_banner_line(text):
            return buf, 'text'                 # это вывод ОС
        seen = True
        # Пустая строка перед следующей служебной — разделитель внутри
        # приветствия; перед выводом ОС — конец приветствия.
        for m in range(n + 1, len(lines)):
            nxt = banner_text(buf[lines[m][0]:lines[m][1]]).strip()
            if not nxt:
                continue
            if not is_banner_line(nxt):
                return buf[lines[m][0]:], 'end'
            break
    return buf, 'wait'


def parse_greeting(data: bytes) -> LineInfo:
    """Разобрать баннер линии в сведения для строки состояния.

    Баннер читается двумя способами — как UTF-8 (если пришёл такой) и
    как КОИ-7 (`banner_text`); для английских маркеров второе
    дополнительно сворачивается омоглифами.  Побеждает первое
    нашедшееся: строка «Connected to the БЭСМ-6 simulator» на обычной
    линии приходит UTF-8, а строка «Connected to DKS» от ДКС — кодами.
    """
    info = LineInfo()
    if not data:
        return info
    info.raw = ' '.join(data.decode('utf-8', errors='replace').split())

    texts = []
    try:
        texts.append(data.decode('utf-8'))
    except UnicodeDecodeError:
        pass
    texts.append(banner_text(data))

    for text in texts:
        if BANNER_DKS.search(text.upper()):
            info.kind = 'ДКС'
            break
    else:
        # Баннер пришёл, но ДКС не объявлена: обычный tty телетайпа.
        info.kind = 'КВУ' if _is_banner(data) else ''

    # Номер ЭВМ сперва ищем в приветствии ОС («ЭВМ-3,Т017») — там он
    # настоящий; из баннера линии берём только явную форму «ЭВМ-N», а
    # «БЭСМ-6 simulator» в счёт не идёт: это модель, а не номер.
    for pattern, attr in ((OS_GREETING_MACHINE, 'machine'),
                          (BANNER_MACHINE, 'machine'),
                          (OS_GREETING_TERMINAL, 'terminal'),
                          (BANNER_LINE_NO, 'terminal')):
        for text in texts:
            m = pattern.search(text)
            if m:
                setattr(info, attr, int(m.group(1)))
                info.seen = True
                break
    for text in texts:
        for pat, label in BANNER_MODE:
            if re.search(r'ENCODING\s+IS\s+' + pat, text.upper(),
                         re.IGNORECASE):
                info.wire = label
                info.seen = True
                return info
    return info


def _is_banner(buf: bytes) -> bool:
    """Начало служебных строк баннера (по эталону 15ИЭ `BANNER_HEADS`)."""
    head = buf.lstrip().lower()
    return any(head.startswith(w) for w in
               (b'connected', b'encoding', b'wru', b'prompt', b'from',
                b'sun', b'mon', b'tue', b'wed', b'thu', b'fri', b'sat'))


class DKSLink:
    """Линия ДКС: TCP-соединение с линией SIMH.

    ЭВМ слушает порт, терминал подключается; если порт занят или линия
    не создана — переходим к следующему из списка.
    """

    def __init__(self, host: str = '127.0.0.1',
                 ports=DEFAULT_PORTS,
                 timeout: float = 10.0,
                 strip_iac: bool = True) -> None:
        self.host = host
        self.ports = parse_port_spec(ports)
        self.timeout = timeout
        self.strip_iac = strip_iac
        self.sock: Optional[socket.socket] = None
        self.port: Optional[int] = None
        #: Приветствие линии (может приходить несколькими пакетами).
        self.greeting = b''
        #: Что о линии говорит баннер: тип, номер терминала, режим.
        self.info = LineInfo()
        #: Баннер ещё можно дорасти: маркер «Connected to DKS» на стенде
        #: приходит последней строкой и в первом пакете отсутствует.
        self._banner_open = True
        self._iac = IacStripper()

    def connect(self) -> None:
        """Подключиться к первой свободной линии ДКС."""
        refused: list[int] = []
        for port in self.ports:
            try:
                s = socket.create_connection((self.host, port), self.timeout)
            except OSError as exc:
                refused.append(port)
                log.debug('порт %s:%d недоступен: %s', self.host, port, exc)
                continue
            s.settimeout(self.timeout)
            # Линия может ответить отказом сразу после accept().
            try:
                s.settimeout(min(self.timeout, 2.0))
                hello = s.recv(256)
            except OSError:
                hello = b''
            if busy_reply(hello):
                log.info('линия %s:%d занята', self.host, port)
                s.close()
                continue
            self.sock, self.port = s, port
            s.settimeout(self.timeout)
            # Приветствие, пришедшее сразу после accept(), нельзя
            # выбрасывать: на стенде это «Connected to the БЭСМ-6
            # simulator TTY device, line N» — первое, что видит
            # оператор, и по нему видно, какая линия досталась.
            self.greeting = self._iac.feed(hello) if hello else b''
            self.info = parse_greeting(self.greeting)
            log.info('линия ДКС %s:%d установлена', self.host, port)
            return
        raise LinkError(f'нет свободной линии ДКС на {self.host}:'
                        f'{spec_hint(refused)}')

    def send(self, data: bytes) -> None:
        if self.sock is None:
            raise LinkError('соединение не установлено')
        self.sock.sendall(data)

    def recv(self, size: int = 4096, timeout: Optional[float] = None) -> bytes:
        """Прочитать данные; ``b''`` — соединение закрыто.

        `timeout` ограничивает ожидание: без него `poll` терминала
        блокировался бы до таймаута соединения (до 10 с), и всё это время
        нажатия клавиш не разбирались.
        """
        if self.sock is None:
            raise LinkError('соединение не установлено')
        if timeout is not None:
            self.sock.settimeout(timeout)
        data = self.sock.recv(size)
        if not data:
            return b''
        out = self._iac.feed(data) if self.strip_iac else data
        return self._take_banner(out)

    def _take_banner(self, data: bytes) -> bytes:
        """Отделить приветствие линии от текста терминала.

        Приветствие — служебный заголовок соединения, а не вывод
        терминала: на кадре ему не место, иначе верхние ряды занимает
        «Connected to DKS» вместо ответа ОС Диспак.  Оно уходит целиком в
        `greeting`, откуда его читает `parse_greeting` для строки
        состояния, а терминал получает только то, что пришло после.

        Баннер кончается пустой строкой после непустой либо строкой
        «Connected to DKS» — на стенде за ней пустой строки не бывает.
        Если приветствие так и не кончилось (это уже болтливость линии,
        а не заголовок), данные отдаются терминалу как есть, чтобы не
        потерять вывод ОС.
        """
        if not self._banner_open:
            return data
        self.greeting += data
        tail, fate = banner_tail(self.greeting)
        info = parse_greeting(self.greeting)
        if info.kind or info.terminal or info.wire:
            self.info = info
        if fate == 'wait' and len(self.greeting) < BANNER_MAX:
            return b''
        self._banner_open = False
        self.greeting = b''            # разобрано, больше не нужно
        return tail if fate == 'end' else data

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def __enter__(self) -> 'DKSLink':
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ── сессия терминала ───────────────────────────────────────────────────────

class TerminalSession:
    """Поток ДКС → кадры терминала.

    Байты из линии скармливаются эмулятору по мере прихода; на каждом
    маркере конца кадра (`АР2 'F‾' ВК`) вызывается `on_frame`, так что
    анимация отображается в темпе прихода данных.
    """

    def __init__(self, link: DKSLink,
                 on_frame: Optional[Callable[[object], None]] = None,
                 on_key: Optional[Callable[[bytes], None]] = None,
                 eol: int = 0o015) -> None:
        self.link = link
        self.on_frame = on_frame
        self.on_key = on_key
        self.emu = TerminalEmulator()
        self.reader = CommandStreamReader()
        self.closed = False
        #: Символ конца строки на линии ДКС — из настройки `[line] eol`,
        #: по умолчанию ПР (0o015): по эталону 15ИЭ (`ie15emu`) окончание
        #: строки на ДКС — ПР, а ETX для линии — просто знак.
        self.eol = eol

    def send_keys(self, text: str, *, eol: Optional[int] = None,
                  set_name: Optional[str] = None) -> None:
        """Отправить ввод с клавиатуры (ДКС сам делает эхо).

        Текст кодируется по правилам 15ИЭ (`chars.encode_input`) в наборе
        `set_name` (по умолчанию — набор ввода сеанса, Н2/КОИ-7): русские
        буквы идут в строку `0x60…0x7E`, строчная латинная — заглавной.
        В конец добавляется символ конца строки (`self.eol`,
        настраивается `[line] eol`); `eol=0` — не добавлять.

        Набранное сеансом (`session.type_key`/`send`) сюда **не** берут:
        оно уже покодировано, и повторное кодирование искажает знаки —
        для отправки готовых байт есть `send_bytes`.
        """
        code = self.eol if eol is None else eol
        data = chars.encode_input(text, set_name=set_name or chars.INPUT_SET)
        if code and not data.endswith(bytes((code,))):
            data += bytes((code,))
        self.link.send(data)

    def send_bytes(self, data: bytes, *, eol: Optional[int] = None) -> None:
        """Отправить **уже покодированные** байты ввода.

        Сеанс кодирует знаки сам (`chars.encode_input` по набору ввода),
        и линия должна получить ровно их.  Раньше байты проходили через
        `send_keys` строкой latin-1 и кодировались второй раз: русская
        «с» (0x73 в Н2) доходила до ЭВМ как `0xD3`, а там уже как `S` —
        команда ОС Диспак уходила в никуда.
        """
        if not data:
            return
        code = self.eol if eol is None else eol
        if code and not data.endswith(bytes((code,))):
            data += bytes((code,))
        self.link.send(data)

    def feed(self, data: bytes) -> None:
        self.reader.feed(data)
        self._drain()

    def _drain(self) -> None:
        for ev in self.reader.pop():
            if ev.kind.name == 'FRAME_END':
                # Счётчик кадров ведёт сам эмулятор (`handle_event`).
                if self.on_frame is not None:
                    self.on_frame(self.emu.fb)
            self.emu.handle_event(ev)

    def run(self, on_data: Optional[Callable[[int], None]] = None,
            poll: float = 0.2) -> None:
        """Читать линию до обрыва."""
        while not self.closed:
            try:
                data = self.link.recv()
            except socket.timeout:
                if on_data is not None:
                    on_data(0)
                continue
            except OSError as exc:
                log.info('линия оборвана: %s', exc)
                break
            if not data:
                log.info('ЭВМ закрыла линию')
                break
            self.feed(data)
            if on_data is not None:
                on_data(len(data))
        self.reader.close()
        self._drain()
        if self.on_frame is not None:
            self.on_frame(self.emu.fb)


# ── режимы командной строки ────────────────────────────────────────────────

def _renderer(backend: str, output: str, scale: int):
    """Создать сохранение кадра по имени backend'а.

    `save(fb)` сохраняет **переданный** кадр — тот, что нарисовала
    сессия, а не пустой кадр нового эмулятора.
    """
    from .backends import PNGBackend, SVGBackend

    def save(fb) -> None:
        cls = SVGBackend if backend == 'svg' else PNGBackend
        cls(fb, **({} if backend == 'svg' else {'scale': scale})).save(output)
        log.info('кадр сохранён: %s', output)
    return save


def run_link(spec: str, *, backend: str = 'png', output: str = 'mc7401.png',
             scale: int = 2, timeout: float = 0.0,
             verbose: int = 0) -> int:
    """Подключиться к ЭВМ и отображать поступающие кадры."""
    host, ports = split_endpoint(spec)
    link = DKSLink(host, ports, timeout=timeout or 10.0)
    with link:
        session = TerminalSession(
            link, on_frame=_renderer(backend, output, scale))
        log.info('ожидание данных от %s:%s (Ctrl-C — выход)',
                 host, spec_hint(link.ports))
        try:
            session.run()
        except KeyboardInterrupt:                    # pragma: no cover
            log.info('прервано пользователем')
    return 0


def _accepted_link(conn: socket.socket, addr, port: int,
                   timeout: float) -> DKSLink:
    """Собрать линию поверх уже принятого соединения (обратный режим).

    `accept()` уже вернул соединение: подключаться заново нельзя —
    иначе принятый сокет был бы потерян.  Объект достраивается теми же
    атрибутами, которые задаёт `__init__` (приветствие, срезка IAC).
    """
    link = DKSLink(addr[0], [port], timeout=timeout)
    link.sock, link.port = conn, port
    return link


def run_listener(spec: str, *, backend: str = 'png',
                 output: str = 'mc7401.png', scale: int = 2,
                 timeout: float = 0.0, verbose: int = 0) -> int:
    """Слушать порт и обслуживать подключения ЭВМ (обратный режим).

    Нужен, когда связь инициирует машина (например, `nc -l` на стороне
    стенда или проброс порта внутрь сети).
    """
    host, ports = split_endpoint(spec)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, ports[0]))
    srv.listen(4)
    log.info('ожидание подключения ЭВМ на %s:%d', host, ports[0])
    served = 0
    try:
        while True:
            conn, addr = srv.accept()
            served += 1
            log.info('подключение: %s:%d', *addr)
            link = _accepted_link(conn, addr, ports[0], timeout or 10.0)
            try:
                session = TerminalSession(
                    link, on_frame=_renderer(backend, output, scale))
                try:
                    session.run()
                except KeyboardInterrupt:            # pragma: no cover
                    log.info('прервано пользователем')
            finally:
                link.close()
    except KeyboardInterrupt:                        # pragma: no cover
        log.info('остановлено, обслужено подключений: %d', served)
    finally:
        srv.close()
    return 0


def wait_for_link(host: str, ports, *, timeout: float = 30.0,
                  delay: float = 0.5) -> int:
    """Дождаться готовности линии ДКС (ЭВМ может стартовать долго)."""
    ports = parse_port_spec(ports)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with DKSLink(host, ports, timeout=delay) as link:
                return link.port
        except (LinkError, OSError):
            time.sleep(delay)
    raise LinkError(f'линия ДКС {host}:{spec_hint(ports)} не поднялась '
                    f'за {timeout:g} с')


__all__ = [
    'LinkError', 'DKSLink', 'TerminalSession', 'IacStripper',
    'parse_port_spec', 'spec_hint', 'split_endpoint', 'busy_reply',
    'run_link', 'run_listener', 'wait_for_link',
    'BUSY_MARKERS', 'DEFAULT_PORTS',
]