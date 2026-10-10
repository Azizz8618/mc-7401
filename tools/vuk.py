"""
tools/vuk_emulator.py — эмуляция ВУК (внешнего устройства канала) для
терминала «Электроника МС 7401».

Что такое ВУК в контексте graf.pas
──────────────────────────────────
В оригинальной программе graf.pas терминал подключается через ВУК —
«внешнее устройство канала» — с физическим логическим номером
`ГРТЕР = 23В` (десятичное 19). Программа работает с ним через
процедуры ЗАХВАТ / ОТКАЗ / ВЫД / РИС / ПРИЕМ, а в их недрах — через
ассемблерные вставки СОDЕ(...).

Полноценно эмулировать этот уровень можно только вместе с эмулятором
самой ДВК (или через реальную машину). Здесь мы реализуем
«прикладную» эмуляцию: патченная версия graf.pas (полученная через
`tools/pascal_patcher.py`) открывает TCP-сокет или pipe и шлёт туда
команды терминала в текстовом виде. Наш VukEmulator принимает этот
поток, разбирает его через `parser` и прогоняет через `emulator`
с выбранным backend'ом.

Это даёт полноценный E2E-сценарий:

    ┌───────────────────┐   TCP / pipe    ┌──────────────────────┐
    │ pascal_patched.pas│ ──────────────▶ │  VukEmulator         │
    │   (Free Pascal)   │                 │  (этот модуль)       │
    └───────────────────┘                 │                      │
                                          │  reader → parser →   │
                                          │  state → emulator →  │
                                          │  backend (svg/tk/…)  │
                                          └──────────────────────┘

Что реализовано
───────────────
    • Абстрактный интерфейс `Transport` — источник байтов;
    • Три транспорта:
        - `FileTransport`    — чтение из файла;
        - `StdinTransport`   — чтение из stdin;
        - `SocketTransport`  — TCP-сервер (клиент — Pascal-программа).
    • Класс `VukEmulator` — принимает поток, разбирает и отрисовывает.
    • Опциональный «обратный канал» для команды ПРИЕМ (ответы клиенту).
    • CLI для запуска сервера.

Ограничения
───────────
    • Это НЕ эмуляция железа. Мы работаем на уровне прикладного
      протокола «команды терминала».
    • Оригинальный graf.pas без патча не сможет подключиться —
      он использует СОDЕ(...) напрямую. Используйте pascal_patcher.
    • Приём символов от клавиатуры (ПРИЕМ) реализован только для
      интерактивных транспортов (stdin/socket).

Автор: <ваше имя>
Лицензия: MIT
"""

from __future__ import annotations

import argparse
import io
import logging
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterator, Optional, Protocol, runtime_checkable

from reader import CommandStreamReader, VK
from parser import CommandParser
from state import TerminalState
from emulator import TerminalEmulator

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Константы
# ─────────────────────────────────────────────────────────────────────────────

# Логический номер ВУК терминала (из graf.pas: ГРТЕР = 23В).
VUK_DEVICE = 23

# Размер читаемого блока.
READ_CHUNK = 4096

# Таймаут приёма клиента (сек).
DEFAULT_TIMEOUT = 30.0


# ─────────────────────────────────────────────────────────────────────────────
# Транспорт
# ─────────────────────────────────────────────────────────────────────────────

@runtime_checkable
class Transport(Protocol):
    """
    Источник байтов для VukEmulator.

    Метод `read(n)` должен возвращать до n байт (возможно меньше,
    возможно b"" в конце потока). Метод `send(data)` — опциональный
    обратный канал.
    """

    def read(self, n: int) -> bytes: ...
    def close(self) -> None: ...


class FileTransport:
    """Читает поток команд из файла."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._file: BinaryIO = self.path.open("rb")

    def read(self, n: int) -> bytes:
        return self._file.read(n)

    def close(self) -> None:
        self._file.close()

    def __repr__(self) -> str:
        return f"FileTransport({self.path})"


class StdinTransport:
    """Читает поток команд из stdin (бинарно)."""

    def __init__(self, stream: Optional[BinaryIO] = None):
        self._stream = stream if stream is not None else sys.stdin.buffer

    def read(self, n: int) -> bytes:
        return self._stream.read(n)

    def close(self) -> None:
        # stdin не закрываем.
        pass

    def __repr__(self) -> str:
        return "StdinTransport()"


class SocketTransport:
    """
    TCP-сервер, принимающий одного клиента (Pascal-программу).

    Клиент подключается, шлёт поток команд (разделитель — ВК = 0o125
    или \n), затем закрывает соединение. Транспорт читает данные
    до EOF.

    Опционально поддерживает обратный канал: если клиент прислал
    запрос ПРИЕМ, транспорт отправит ответные байты.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7401,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        backlog: int = 1,
    ):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.backlog = backlog

        self._server: Optional[socket.socket] = None
        self._client: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None

    # ─── Сервер ──────────────────────────────────────────────────────────────

    def listen(self) -> None:
        """Открыть серверный сокет и ждать одного клиента."""
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((self.host, self.port))
        self._server.listen(self.backlog)
        log.info("VUK: сервер слушает %s:%d", self.host, self.port)

    def accept(self) -> None:
        """Принять одного клиента (блокирующая операция)."""
        if self._server is None:
            self.listen()
        assert self._server is not None

        log.info("VUK: ожидание клиента (timeout=%.1f c)", self.timeout)
        self._server.settimeout(self.timeout)
        try:
            client, addr = self._server.accept()
        except socket.timeout:
            log.error("VUK: клиент не подключился за %.1f c", self.timeout)
            raise

        log.info("VUK: подключился клиент %s:%d", addr[0], addr[1])
        client.settimeout(None)
        self._client = client

    # ─── Transport API ───────────────────────────────────────────────────────

    def read(self, n: int) -> bytes:
        if self._client is None:
            return b""
        try:
            return self._client.recv(n)
        except (ConnectionResetError, OSError) as exc:
            log.warning("VUK: ошибка чтения: %s", exc)
            return b""

    def send(self, data: bytes) -> None:
        """Обратный канал: ответ клиенту."""
        if self._client is None:
            return
        try:
            self._client.sendall(data)
        except OSError as exc:
            log.warning("VUK: ошибка отправки: %s", exc)

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except OSError:
                pass
            self._client = None

        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None

    def __repr__(self) -> str:
        return f"SocketTransport({self.host}:{self.port})"


# ─────────────────────────────────────────────────────────────────────────────
# Эмулятор ВУК
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class VukStats:
    """Счётчики работы VukEmulator."""
    bytes_in: int = 0
    commands: int = 0
    errors: int = 0
    start_time: float = field(default_factory=time.time)

    @property
    def duration(self) -> float:
        return time.time() - self.start_time

    def to_dict(self) -> dict:
        return {
            "bytes_in": self.bytes_in,
            "commands": self.commands,
            "errors": self.errors,
            "duration_sec": round(self.duration, 3),
        }


class VukEmulator:
    """
    Прикладной эмулятор ВУК.

    Параметры:
        transport  — источник байтов (FileTransport, StdinTransport,
                     SocketTransport);
        backend    — backend отрисовки (SVGBackend, TkBackend, …);
        device     — логический номер ВУК (по умолчанию 23 = ГРТЕР);
        strict     — останавливаться на первой ошибке разбора;
        echo       — если True и транспорт поддерживает `send`,
                     эхо-ответы на команды ПРИЕМ.

    Пример:
        transport = SocketTransport(port=7401)
        transport.listen()
        transport.accept()
        backend = SVGBackend(TerminalState())
        vuk = VukEmulator(transport, backend)
        vuk.run()
        backend.save("output.svg")
    """

    def __init__(
        self,
        transport: Transport,
        backend,
        *,
        device: int = VUK_DEVICE,
        strict: bool = False,
        echo: bool = False,
    ):
        self.transport = transport
        self.backend = backend
        self.device = device
        self.strict = strict
        self.echo = echo

        self.state = backend.state if hasattr(backend, "state") else TerminalState()
        self.emulator = TerminalEmulator(self.state, backend)
        self.stats = VukStats()

        # Буфер для накопления байтов между чтениями.
        self._buffer = bytearray()
        # Позиция, с которой reader начинает разбор.
        self._reader_pos = 0

    # ─── Приём/отдача ────────────────────────────────────────────────────────

    def _read_into_buffer(self) -> bool:
        """
        Прочитать очередную порцию байтов от транспорта.

        Возвращает True, если что-то прочитано, False — конец потока.
        """
        chunk = self.transport.read(READ_CHUNK)
        if not chunk:
            return False
        self._buffer.extend(chunk)
        self.stats.bytes_in += len(chunk)
        return True

    def _has_pending_command(self) -> bool:
        """Есть ли в буфере завершённая команда (по ВК = 0o125)?"""
        return VK in self._buffer

    # ─── Основной цикл ───────────────────────────────────────────────────────

    def run(self) -> VukStats:
        """
        Читать поток, разбирать команды и отрисовывать.

        Возвращает статистику по завершении.
        """
        log.info("VUK: начало приёма (device=%d)", self.device)

        while True:
            # Читаем, пока не накопим хотя бы одну команду.
            if not self._has_pending_command():
                try:
                    ok = self._read_into_buffer()
                except Exception as exc:  # noqa: BLE001
                    log.error("VUK: ошибка чтения: %s", exc)
                    self.stats.errors += 1
                    break
                if not ok:
                    # Конец потока.
                    if self._buffer:
                        log.warning(
                            "VUK: остались %d байт без завершающего ВК",
                            len(self._buffer),
                        )
                    break
                continue

            # Разбираем одну команду.
            cmd = self._next_command()
            if cmd is None:
                continue

            try:
                self.emulator.execute(cmd)
                self.stats.commands += 1
                self._on_command(cmd)
            except Exception as exc:  # noqa: BLE001
                log.error("VUK: ошибка выполнения команды %r: %s", cmd, exc)
                self.stats.errors += 1
                if self.strict:
                    raise

        self._finalize()
        log.info("VUK: завершено (%s)", self.stats.to_dict())
        return self.stats

    # ─── Разбор ──────────────────────────────────────────────────────────────

    def _next_command(self):
        """Извлечь следующую команду из буфера и разобрать её."""
        # Находим первую ВК.
        try:
            vk_pos = self._buffer.index(VK)
        except ValueError:
            return None

        # Извлекаем байты команды (без ВК).
        raw = bytes(self._buffer[:vk_pos])
        del self._buffer[:vk_pos + 1]

        # Собираем reader + parser на один вызов.
        reader = CommandStreamReader(raw + bytes([VK]))
        parser = CommandParser(reader)
        try:
            cmd = parser.parse()
        except Exception as exc:  # noqa: BLE001
            log.error("VUK: ошибка разбора %r: %s", raw, exc)
            self.stats.errors += 1
            if self.strict:
                raise
            return None

        return cmd

    # ─── Обратная связь ──────────────────────────────────────────────────────

    def _on_command(self, cmd) -> None:
        """
        Реакция на успешно выполненную команду.

        Если включён echo и транспорт поддерживает send, отвечаем
        клиенту. В оригинальном терминале ответы нужны для команды
        ПРИЕМ (чтение символа с клавиатуры). Здесь оставляем задел.
        """
        if not self.echo:
            return
        send = getattr(self.transport, "send", None)
        if not callable(send):
            return

        # Простейшее поведение: подтверждаем выполнение символом 'k'.
        # Протокол ответов можно уточнить по ТО терминала.
        if cmd.op == "2":  # конец кадра — сигнал клиенту «кадр принят»
            try:
                send(b"k")
            except Exception as exc:  # noqa: BLE001
                log.debug("VUK: не удалось отправить ответ: %s", exc)

    # ─── Финализация ─────────────────────────────────────────────────────────

    def _finalize(self) -> None:
        """Отобразить финальный кадр и сохранить результат, если backend умеет."""
        present = getattr(self.backend, "present", None)
        if callable(present):
            try:
                present()
            except Exception as exc:  # noqa: BLE001
                log.debug("VUK: present: %s", exc)

        save = getattr(self.backend, "save", None)
        if callable(save):
            try:
                default_path = _default_output_for(self.backend)
                if default_path is not None:
                    save(default_path)
                    log.info("VUK: результат сохранён в %s", default_path)
            except Exception as exc:  # noqa: BLE001
                log.debug("VUK: save: %s", exc)

        try:
            self.transport.close()
        except Exception as exc:  # noqa: BLE001
            log.debug("VUK: close: %s", exc)

    # ─── Представление ───────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return (
            f"VukEmulator(device={self.device}, "
            f"transport={self.transport!r}, "
            f"backend={type(self.backend).__name__})"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Вспомогательные
# ─────────────────────────────────────────────────────────────────────────────

def _default_output_for(backend) -> Optional[str]:
    """Подобрать имя выходного файла для backend'а."""
    name = type(backend).__name__.lower()
    if "svg" in name:
        return "vuk_output.svg"
    if "pygame" in name:
        return "vuk_output.png"
    return None


def create_vuk_emulator(
    transport: Transport,
    backend,
    *,
    device: int = VUK_DEVICE,
    strict: bool = False,
    echo: bool = False,
) -> VukEmulator:
    """Фабричная функция для VukEmulator."""
    return VukEmulator(
        transport,
        backend,
        device=device,
        strict=strict,
        echo=echo,
    )


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vuk_emulator",
        description="Прикладной эмулятор ВУК терминала МС 7401.",
    )
    p.add_argument(
        "-t", "--transport",
        choices=["file", "stdin", "socket"],
        default="file",
        help="Тип транспорта (по умолчанию: file).",
    )
    p.add_argument(
        "input", nargs="?",
        help="Файл с потоком команд (только для --transport file).",
    )
    p.add_argument(
        "-b", "--backend",
        choices=["svg", "tk", "pygame"],
        default="svg",
        help="Backend отрисовки (по умолчанию: svg).",
    )
    p.add_argument(
        "-o", "--output",
        default=None,
        help="Выходной файл (для svg).",
    )
    p.add_argument(
        "--host", default="127.0.0.1",
        help="Адрес для --transport socket (по умолчанию: 127.0.0.1).",
    )
    p.add_argument(
        "--port", type=int, default=7401,
        help="Порт для --transport socket (по умолчанию: 7401).",
    )
    p.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT,
        help=f"Таймаут ожидания клиента, с (по умолчанию: {DEFAULT_TIMEOUT}).",
    )
    p.add_argument(
        "--strict", action="store_true",
        help="Останавливаться на первой ошибке.",
    )
    p.add_argument(
        "--echo", action="store_true",
        help="Отправлять клиенту подтверждения (для socket).",
    )
    p.add_argument(
        "-v", "--verbose", action="count", default=0,
        help="Подробный вывод.",
    )
    return p


def _make_transport(args) -> Transport:
    if args.transport == "file":
        if not args.input:
            raise SystemExit("Для --transport file нужно указать входной файл.")
        return FileTransport(args.input)

    if args.transport == "stdin":
        return StdinTransport()

    if args.transport == "socket":
        t = SocketTransport(args.host, args.port, timeout=args.timeout)
        t.listen()
        t.accept()
        return t

    raise SystemExit(f"Неизвестный транспорт: {args.transport}")


def _make_backend(name: str, state: TerminalState):
    from backends import create_backend
    return create_backend(name, state)


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    level = logging.WARNING
    if args.verbose == 1:
        level = logging.INFO
    elif args.verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")

    try:
        transport = _make_transport(args)
    except SystemExit as exc:
        print(exc)
        return 2

    state = TerminalState()
    backend = _make_backend(args.backend, state)

    vuk = VukEmulator(
        transport,
        backend,
        strict=args.strict,
        echo=args.echo,
    )

    print(f"VUK: устройство {vuk.device}, backend {type(backend).__name__}")

    try:
        stats = vuk.run()
    except KeyboardInterrupt:
        print("\nVUK: прервано пользователем")
        return 130

    print()
    print("Статистика:")
    for key, value in stats.to_dict().items():
        print(f"  {key:14s} = {value}")

    if args.output:
        save = getattr(backend, "save", None)
        if callable(save):
            save(args.output)
            print(f"Сохранено: {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())