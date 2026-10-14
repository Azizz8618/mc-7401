"""
tests/test_link_session.py — линия ДКС и сеанс терминала.

Проверяются дефекты связи и сеанса (`mc7401/link.py`, `mc7401/session.py`):

* принятое в `run_listener` соединение не теряется и не переподключается;
* кадр для `--png`/`--svg` сохраняется из сессии, а не пустой;
* счётчик кадров не удваивается;
* тело telnet-сабnegoциации не протекает в поток команд;
* ввод кодируется по правилам 15ИЭ (семибитная КОИ-7), а не КОИ-8Р;
* накопленное в АВТОНОМНО не теряется при переходе в С ЭВМ.

Сеть мокается фейками: реальные соединения не открываются.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mc7401.backends as backends                                    # noqa: E402
from mc7401 import chars                                              # noqa: E402
from mc7401 import TerminalEmulator                                  # noqa: E402
from mc7401.constants import ETX, VK                                 # noqa: E402
from mc7401.link import (                                            # noqa: E402
    DKSLink, IacStripper, TerminalSession, _accepted_link,
    parse_greeting, run_link, run_listener,
)
from mc7401.session import Session                                   # noqa: E402


# ── фейки: реальные соединения не открываются ──────────────────────────────

class FakeLink:
    """Фейк линии ДКС: записывает отправленные байты."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def send(self, data: bytes) -> None:
        self.sent.append(bytes(data))


class FakeConn:
    """Фейк принятого соединения: отдаёт заданные порции данных."""

    def __init__(self, chunks=()) -> None:
        self._chunks = list(chunks)
        self.sent = bytearray()
        self.closed = False

    def recv(self, size: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b''

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def settimeout(self, timeout) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakeServer:
    """Фейк серверного сокета: выдаёт заданные подключения по одному."""

    def __init__(self, conns) -> None:
        self._conns = list(conns)
        self.closed = False

    def setsockopt(self, *args) -> None:
        pass

    def bind(self, addr) -> None:
        pass

    def listen(self, backlog: int) -> None:
        pass

    def accept(self):
        if self._conns:
            return self._conns.pop(0)
        raise KeyboardInterrupt            # так выходит run_listener

    def close(self) -> None:
        self.closed = True


class FakePNG:
    """Фейк PNG-бэкенда: запоминает кадр, который собирался сохранять."""

    saved: dict = {}

    def __init__(self, fb, **kw) -> None:
        FakePNG.saved['fb'] = fb

    def save(self, path) -> None:
        FakePNG.saved['path'] = path


#: Поток с рисованием линии (10,20)–(30,40) и маркером конца кадра.
DRAW = (b'J1' + bytes((VK,)) + b'4 10 20' + bytes((VK,))
        + b'7 1 30 40' + bytes((VK,)) + b'TF>' + bytes((VK,)))


# ── дефект: тело telnet-сабnegoциации протекало в поток команд ─────────────

#: Баннер ДКС, как его присылает линия SIMH.
BANNER = ('\n\r\nConnected to the БЭСМ-6 simulator TTY device, line 3\r\n\n'
          'Encoding is RAW\r\n'
          'WRU – Break to sim> prompt character - is ^G\r\n'
          'Tue Oct 13 07:45:42 2026 from 127.0.0.1\r\n'
          'Connected to DKS\r\n').encode('utf-8')


def test_banner_yields_line_number_machine_and_mode():
    """Баннер SIMH — источник типа линии, терминала и режима.

    **Номера ЭВМ в баннере нет**: «the БЭСМ-6 simulator» — это модель
    машины, а не номер ЭВМ.  Раньше отсюда бралось `machine = 6`, и в
    строке состоянии стояло «ЭВМ=6» — цифра, которая ни о чём не
    говорила.  Настоящий номер приходит из приветствия ОС
    («ЭВМ-3,Т017»), см. `test_os_greeting_gives_machine_and_terminal`.
    """
    info = parse_greeting(BANNER)
    assert info.kind == 'ДКС'
    assert info.machine == 0, 'модель «БЭСМ-6» не должна идти в номер ЭВМ'
    assert info.terminal == 3
    assert info.wire == 'RAW'
    assert info.seen


def test_os_greeting_gives_machine_and_terminal():
    """Приветствие ОС «ЭВМ-3,Т017» — источник номера ЭВМ и терминала.

    Линия ДКС шлёт его кодами КОИ-7, и кириллица приходит **латинскими
    омоглифами**: на месте «ЭВМ» идут байты `7C 42 4D`, то есть `ЭBM`.
    Разбор обязан терпеть омоглифы — иначе номер ЭВМ с живой линии не
    читался бы вовсе.
    """
    from mc7401.chars import display_dks
    wire = bytes([0x7C, 0x42, 0x4D, 0x2D, 0x33, 0x2C, 0x54, 0x30, 0x31, 0x37])
    assert ''.join(display_dks(b) for b in wire) == 'ЭBM-3,T017'
    info = parse_greeting(wire)
    assert info.machine == 3
    assert info.terminal == 17


def test_os_greeting_utf8_gives_machine_and_terminal():
    """То же приветствие в UTF-8 читается так же."""
    info = parse_greeting('ЭВМ-3,Т017'.encode('utf-8'))
    assert info.machine == 3
    assert info.terminal == 17


def test_banner_without_machine_leaves_it_unknown():
    """Обычный tty-баннер: ЭВМ неизвестна, но линия — «КВУ»."""
    info = parse_greeting(b'Connected to tty\nTTY DEVICE, LINE 7\n')
    assert info.terminal == 7
    assert info.machine == 0
    assert info.kind == 'КВУ'


def test_banner_machine_from_short_model_name():
    """Приветствие «ЭВМ-3,Т020» — тоже ЭВМ, номер берётся из него."""
    info = parse_greeting('Connected to DKS\nЭВМ-3,Т020\n'.encode('utf-8'))
    assert (info.machine, info.kind) == (3, 'ДКС')


def koi7(text: str) -> bytes:
    """Текст → коды КОИ-7, как их шлёт линия ДКС в режиме `raw`.

    Русские буквы берутся из строки `0x60…0x7E` знакогенератора в
    порядке `chars.RUS7` (регистронезависимо), латиница — свои коды, но
    **заглавными**: линия переводит латиницу в верхний регистр, и строчные
    коды на самом деле оказываются русскими знаками.
    """
    out = bytearray()
    for ch in text:
        ru = chars.RUS8.find(ch.upper())
        if ru >= 0:
            out.append(0x60 + ru)
        elif ch.isalpha() and ch.isascii():
            out.append(ord(ch.upper()))
        else:
            out.append(ord(ch))
    return bytes(out)


def test_banner_in_koi7_codes_is_recognised():
    """Баннер кодами КОИ-7 — тип линии всё равно распознаётся.

    Линия ДКС в режиме `raw` шлёт семибитные коды и переводит латиницу
    в заглавные; часть букв приходит кириллическими кодами (омоглифы
    знакогенератора), и на экране маркер выглядит как «CОNNЕCTED TO DKS».
    По сырым байтам он не находился, и линия объявлялась обычным tty —
    «КВУ».
    """
    info = parse_greeting(koi7('CОNNЕCTED TO DKS\n'))
    assert info.kind == 'ДКС'


def test_banner_koi7_gives_terminal_and_mode():
    info = parse_greeting(
        koi7('ENCODING IS RAW\n'
             'TTY DEVICE, LINE 3\n'
             'CОNNЕCTED TO DKS\n'))
    assert (info.kind, info.terminal, info.wire) == ('ДКС', 3, 'RAW')


def test_link_keeps_parsing_banner_arriving_in_pieces():
    """Маркер «Connected to DKS» приходит последним пакетом.

    Разбор только первого пакета давал «КВУ» на линии ДКС: стенд шлёт
    приветствие несколькими пакетами, и признак ДКС в первом из них
    отсутствует.
    """
    link = DKSLink(host='h', ports='4202')
    link.sock = FakeConn([b''])
    link.greeting = koi7('ENCODING IS RAW\r\nTTY DEVICE, LINE 3\r\n')
    link.info = parse_greeting(link.greeting)
    assert link.info.kind != 'ДКС'

    link.sock = FakeConn([koi7('Connected to DKS\r\n')])
    link.recv()
    assert link.info.kind == 'ДКС', 'маркер ДКС из последнего пакета потерян'
    assert link.info.terminal == 3
    assert link.info.wire == 'RAW'


def test_banner_never_reaches_the_frame():
    """Приветствие линии не попадает на кадр терминала.

    «Connected to DKS» — служебный заголовок соединения, а не вывод
    терминала: на кадре он занимал верхние ряды вместо ответа ОС Диспак.
    Он уходит в `greeting` (оттуда его читает разбор для строки
    состояния), а терминал получает только то, что после.
    """
    link = DKSLink(host='h', ports='4202')
    link.sock = FakeConn([koi7('ENCODING IS RAW\r\n'
                               'TTY DEVICE, LINE 3\r\n'
                               'Connected to DKS\r\n')])
    assert link.recv() == b'', 'приветствие не должно доходить до терминала'
    assert link.info.kind == 'ДКС', 'но должно разбираться для строки состояния'

    # Дальше идёт уже вывод ОС — он терминалу и принадлежит.
    prompt = b'\x1b' + 'ЭВМ-3,Т020'.encode('utf-8') + b'\r\n'
    link.sock = FakeConn([prompt])
    assert link.recv() == prompt, 'вывод ОС должен доходить целиком'


def test_banner_lookalikes_are_still_banner():
    """Маркер ДКС приходит омоглифами — он всё равно конец приветствия.

    Линия шлёт коды КОИ-7, и часть букв «Connected to DKS» приходит
    кириллическими знаками, читающимися так же: «CОNNЕCTED TO DKS».
    Строка не начинается ни с одного признанного начала, и без свёртки
    омоглифов приветствие ушло бы в терминал целиком.
    """
    link = DKSLink(host='h', ports='4202')
    # «CОNNЕCTED TO DKS» — О и Е кириллические, остальное латинское.
    look = 'CОNNЕCTED TO DKS'
    link.sock = FakeConn([koi7(look + '\r\n')])
    assert link.recv() == b'', 'приветствие с омоглифами не должно попасть в кадр'


def test_os_output_without_banner_is_not_swallowed():
    """Если приветствия нет, данные ОС идут в терминал сразу.

    Баннер отбрасывается по его собственным строкам, а не «пока не
    кончится»: иначе линия без приветствия молчала бы до лимита.
    """
    link = DKSLink(host='h', ports='4202')
    link.sock = FakeConn([DRAW])
    assert link.recv() == DRAW


def test_banner_with_blank_line_is_taken():
    """Приветствие, кончающееся пустой строкой, снимается целиком."""
    link = DKSLink(host='h', ports='4202')
    link.sock = FakeConn([koi7('Connected to the tty\r\n\r\nЭВМ-3,Т020\r\n')])
    assert link.recv() == koi7('ЭВМ-3,Т020\r\n')


def test_link_stops_parsing_banner_after_it_ends():
    """Данные ОС после баннера идут в терминал, а не в приветствие.

    Пустая строка сама по себе приветствие не завершает — на стенде оно
    разделено пустыми строками внутри.  Завершает его первая строка, которая
    не похожа на служебную: это уже вывод ОС.
    """
    link = DKSLink(host='h', ports='4202')
    link._take_banner(koi7('ENCODING IS RAW\r\nTTY DEVICE, LINE 3\r\n\r\n'))
    assert link._banner_open, 'пустая строка внутри приветствия не кончает его'
    link._take_banner(koi7('ЭВМ-3,Т020\r\n'))
    assert not link._banner_open
    assert link._take_banner(b'hello') == b'hello'


def test_empty_banner_gives_nothing():
    empty = parse_greeting(b'')
    assert (empty.kind, empty.machine, empty.terminal, empty.wire) == \
        ('', 0, 0, '')


def test_iac_subnegotiation_is_swallowed():
    """`IAC SB NAWS … IAC SE` не даёт байтов потока команд."""
    s = IacStripper()
    data = s.feed(b'\xff\xfa\x1c\x00\x02\x00\x50\x00\x18\xff\xf0OK')
    assert data == b'OK'


def test_iac_subnegotiation_survives_split_feeds():
    """Сабnegoциация может прийти несколькими чтениями — тело не протекает."""
    s = IacStripper()
    assert s.feed(b'\xff\xfa\x1c\x00') == b''
    assert s.feed(b'\x02\x00\x50') == b''
    assert s.feed(b'\xff\xf0OK') == b'OK'


def test_iac_escaped_iac_does_not_end_subnegotiation():
    """`IAC IAC` внутри сабnegoциации — эсквейп, а не конец тела."""
    s = IacStripper()
    assert s.feed(b'\xff\xfa\x01\xff\xff\x02A\xff\xf0') == b''
    assert s.feed(b'OK') == b'OK'


# ── дефект: ввод кодировался КОИ-8Р вместо семибитной КОИ-7 ───────────────

def test_send_keys_uses_koi7_input_rules():
    """Русские знаки кодируются по КОИ-7, конец строки — ПР.

    Набор ввода по умолчанию — Н2 (набор линии ДКС): «СЕТ» — это
    `0x73 0x65 0x74` (`b'set'`), потому что строка `0x60…0x7E` в КОИ-7
    занята кириллицей, а регистр сворачивает машина.
    """
    link = FakeLink()
    ses = TerminalSession(link)
    ses.send_keys('СЕТ')
    assert link.sent == [b'set\r']


def test_send_bytes_does_not_recode_input():
    """Готовые байты сеанса уходят на линию как есть.

    Повторное кодирование (latin-1 → КОИ-7) превращало русскую «с» в
    `S`, а команду ОС Диспак — в чужую: `send_bytes` не кодирует.
    """
    link = FakeLink()
    ses = TerminalSession(link)
    ses.send_bytes(b'set', eol=0)
    assert link.sent == [b'set']
    ses.send_bytes(b'set')                # конец строки из настройки
    assert link.sent[-1] == b'set\r'


def test_send_keys_eol_comes_from_parameter():
    """Символ конца строки настраивается; 0 — не добавлять."""
    link = FakeLink()
    ses = TerminalSession(link, eol=0o012)       # как `[line] eol = LF`
    ses.send_keys('OK')
    assert link.sent == [b'OK\n']
    ses.send_keys('OK', eol=0)
    assert link.sent[-1] == b'OK'
    # Конец строки уже набран — второй не добавляется.
    default = TerminalSession(link)
    default.send_keys('OK\r')
    assert link.sent[-1] == b'OK\r'


# ── дефект: счётчик кадров удваивался ──────────────────────────────────────

def test_session_counts_frames_once():
    """Счётчик кадров ведёт эмулятор — без двойного счёта в сессии."""
    data = b'TF>' + bytes((VK,)) + b'TF>' + bytes((VK,))
    ses = TerminalSession(FakeLink())
    ses.feed(data)
    emu = TerminalEmulator()
    emu.run(data)
    assert emu.frames == 2
    assert ses.emu.frames == emu.frames


# ── дефект: переход АВТОНОМНО → С ЭВМ терял набранное ──────────────────────

def test_toggle_mode_returns_accumulated_input():
    """Накопленное в АВТОНОМНО уходит вызывающему, а не теряется."""
    s = Session()
    assert s.toggle_mode() == b''               # С ЭВМ → АВТОНОМНО
    assert s.mode == 'local'
    assert s.type_line('ЛИН 3 ') == b''
    out = s.toggle_mode()                       # АВТОНОМНО → С ЭВМ
    assert out == b'lin 3 '                     # накопленное не потеряно
    assert s.mode == 'host'
    assert s.pending() == b''                   # и буфер очищен


def test_set_mode_returns_accumulated_input():
    """`set_mode` при переходе в С ЭВМ тоже отдаёт накопленное."""
    s = Session()
    assert s.set_mode('local') == b''
    s.type_line('СЕТ')
    assert s.set_mode('host') == b'set'
    assert s.mode == 'host'
    assert s.buffer == []


# ── дефект: принятое соединение выбрасывалось при «подключении» ────────────

def test_accepted_link_keeps_socket_and_init_attributes():
    """Линия поверх принятого соединения получает атрибуты `__init__`."""
    conn = FakeConn()
    link = _accepted_link(conn, ('192.0.2.10', 4711), 4202, 5.0)
    assert link.sock is conn                     # сокет не перезаписан
    assert link.port == 4202
    assert link.greeting == b''                  # задан, а не отсутствует
    assert link.strip_iac is True
    assert isinstance(link._iac, IacStripper)
    link.close()
    assert conn.closed


def test_run_listener_serves_accepted_socket(monkeypatch):
    """Обратный режим обслуживает принятый сокет и не переподключается."""
    conn = FakeConn([DRAW, b''])
    srv = FakeServer([(conn, ('192.0.2.10', 4711))])

    def no_connect(*args, **kw):
        raise AssertionError('принятое соединение не должно переподключаться')

    monkeypatch.setattr(socket, 'socket', lambda *a, **kw: srv)
    monkeypatch.setattr(socket, 'create_connection', no_connect)
    monkeypatch.setattr(backends, 'PNGBackend', FakePNG)
    FakePNG.saved.clear()

    assert run_listener('127.0.0.1:4202', output='/tmp/кадр.png') == 0
    assert conn.closed                           # линия закрыта через close()
    assert srv.closed
    # Кадр сохранён из сессии (принятые данные нарисованы), а не пустой.
    assert FakePNG.saved['fb'].count_set() > 0


# ── дефект: `--png`/`--svg` сохраняли пустой кадр ──────────────────────────

def test_run_link_saves_session_frame(monkeypatch):
    """Сохраняется кадр сессии, а не пустой кадр нового эмулятора."""
    conn = FakeConn([b'', DRAW, b''])            # пустое приветствие + поток

    monkeypatch.setattr(socket, 'create_connection', lambda *a, **kw: conn)
    monkeypatch.setattr(backends, 'PNGBackend', FakePNG)
    FakePNG.saved.clear()

    assert run_link('127.0.0.1:4202', output='/tmp/кадр.png') == 0
    assert FakePNG.saved['path'] == '/tmp/кадр.png'
    assert FakePNG.saved['fb'].count_set() > 0   # линия нарисована в кадре
