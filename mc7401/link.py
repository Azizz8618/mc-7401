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
import socket
import time
from typing import Callable, Iterable, Optional

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
    прийти двумя чтениями.  Данные терминала не содержат 0xFF, поэтому
    эсквейп IAC IAC сюда не попадает и просто отбрасывается.
    """

    def __init__(self) -> None:
        self.state = 'data'
        self.skip = 0

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
                if b == IAC:
                    self.skip = 2                 # ждём IAC SE
                else:
                    self.state = 'data'
            elif self.state == 'skip':
                self.skip -= 1
                if self.skip == 0:
                    self.state = 'data'
        return bytes(out)


# ── канал связи ────────────────────────────────────────────────────────────

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
            log.info('линия ДКС %s:%d установлена', self.host, port)
            return
        raise LinkError(f'нет свободной линии ДКС на {self.host}:'
                        f'{spec_hint(refused)}')

    def send(self, data: bytes) -> None:
        if self.sock is None:
            raise LinkError('соединение не установлено')
        self.sock.sendall(data)

    def recv(self, size: int = 4096) -> bytes:
        """Прочитать данные; ``b''`` — соединение закрыто."""
        if self.sock is None:
            raise LinkError('соединение не установлено')
        data = self.sock.recv(size)
        if not data:
            return b''
        return self._iac.feed(data) if self.strip_iac else data

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
                 on_key: Optional[Callable[[bytes], None]] = None) -> None:
        self.link = link
        self.on_frame = on_frame
        self.on_key = on_key
        self.emu = TerminalEmulator()
        self.reader = CommandStreamReader()
        self.closed = False

    def send_keys(self, text: str) -> None:
        """Отправить ввод с клавиатуры (ДКС сам делает эхо)."""
        self.link.send(text.encode('koi8-r', errors='replace'))

    def feed(self, data: bytes) -> None:
        self.reader.feed(data)
        self._drain()

    def _drain(self) -> None:
        for ev in self.reader.pop():
            if ev.kind.name == 'FRAME_END':
                self.emu.frames += 1
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

def _renderer(fb, backend: str, output: str, scale: int):
    """Создать сохранение кадра по имени backend'а."""
    from .backends import PNGBackend, SVGBackend

    def save() -> None:
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
        save = _renderer(TerminalEmulator().fb, backend, output, scale)
        session = TerminalSession(link, on_frame=lambda fb: save())
        log.info('ожидание данных от %s:%s (Ctrl-C — выход)',
                 host, spec_hint(link.ports))
        try:
            session.run()
        except KeyboardInterrupt:                    # pragma: no cover
            log.info('прервано пользователем')
    return 0


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
            link = DKSLink.__new__(DKSLink)
            link.host, link.ports = addr[0], [ports[0]]
            link.timeout, link.strip_iac = (timeout or 10.0), True
            link.sock, link.port = conn, ports[0]
            link._iac = IacStripper()
            with link:
                save = _renderer(TerminalEmulator().fb, backend,
                                 output, scale)
                session = TerminalSession(link, on_frame=lambda fb: save())
                try:
                    session.run()
                except KeyboardInterrupt:            # pragma: no cover
                    log.info('прервано пользователем')
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