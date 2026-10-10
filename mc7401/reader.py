"""
reader.py — разбор потока байтов терминала «Электроника МС 7401».

Поток, приходящий от основной ЭВМ, состоит из *команд* — записей,
заканчивающихся кодом `ВК` (0o125 = 85).  Внутри записи параметры
разделены пробелом `П1`.

Кроме команд в потоке встречаются **кадровые маркеры** канала ВУК,
которые программа `graf.pas` формирует в процедурах `ВЫД`/`РИС`:

    '/'  ВК  <20 байт служебной записи>  <данные>  АР2 'F‾' ВК

Служебная запись — `'z'` (`≡172`) и хвост из нулей; командой терминала
не является.  Маркер `АР2 'F‾' ВК` означает конец кадра (вывод на
экран), `АР2 'У  '` — вход в графический кадр.

Модуль выдаёт последовательность событий (`Event`), не занимаясь
разбором параметров (это делает `parser`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum, auto

from .constants import (
    AR2,
    END,
    P1,
    SC_ENDTEXT,
    SC_ROW,
    SC_TXT_LINE,
    VK,
)

log = logging.getLogger(__name__)

#: Служебная запись кадра ВУК: `'z'` (`≡172`) и хвост из нулей.
VUK_HEADER_CODE = 0o172  # 'z'
VUK_HEADER_LEN = 20       # длина служебной записи в байтах вместе с 'z'

#: Префикс блока ВУК: `'/'` + ВК.
VUK_BLOCK = bytes((ord('/'), VK))

#: Заполнитель кадра: 0o32 (SUB) + ВК — операции не несёт.
PAD_CMD = bytes((0o32, VK))


class EventKind(Enum):
    """Тип события, выдаваемого `CommandStreamReader`."""

    COMMAND = auto()      # запись, заканчивающаяся ВК
    FRAME_BEGIN = auto()  # АР2 'У  ' — вход в графический кадр
    FRAME_END = auto()    # АР2 'F‾' ВК — конец кадра
    TEXT_LINE = auto()    # ESC 0o306 0o336 0o215 — конец строки текста
    RU_FONT = auto()      # 0o273 0o240 — возврат к русскому шрифту
    DEFCHAR = auto()      # 0o110 0o240 — начало загрузки пользовательского знака
    DEFROW = auto()       # 0o240 — начало строки пользовательского знака
    DEFDONE = auto()      # 0o215 — конец загрузки пользовательского знака
    VUK_BLOCK = auto()    # '/' ВК — начало блока ВУК
    PAD = auto()          # 0o32 ВК — заполнитель кадра (операции нет)
    END_OF_STREAM = auto()


@dataclass
class Event:
    """Событие потока."""

    kind: EventKind
    data: bytes = b''
    #: Смещение события в потоке (для диагностики).
    offset: int = 0
    meta: dict = field(default_factory=dict)


class _State(Enum):
    NORMAL = auto()
    VUK_HEADER = auto()
    VUK_PAYLOAD = auto()
    DEFCHAR_CODE = auto()   # ждём 7 байт кода знака
    DEFCHAR_ROWS = auto()   # собираем строки знака до 0o215


class CommandStreamReader:
    """Пошаговый разбор потока байтов в события.

    Кормите его порциями (`feed`), забирайте события (`pop`), в конце
    закройте поток (`close`).
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._events: list[Event] = []
        self._pos = 0
        self._closed = False
        self._state = _State.NORMAL
        #: Код знака, загружаемого командой `≡110`.
        self._defcode = 0
        self._defrows: list[int] = []
        #: Последняя запись `9 S ВК` — её данные придут следующей записью.
        self._text_size: bytes = b''
        self._text_mode = False

    # ── состояние ────────────────────────────────────────────────────────────

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def pending(self) -> int:
        """Сколько байт не разобрано (незавершённая запись)."""
        return len(self._buf)

    # ── приём данных ─────────────────────────────────────────────────────────

    def feed(self, data: bytes) -> None:
        self._buf.extend(data)
        self._scan()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._buf:
            self._emit(EventKind.COMMAND, bytes(self._buf))
            self._buf.clear()

    # ── извлечение событий ───────────────────────────────────────────────────

    def pop(self) -> list[Event]:
        out, self._events = self._events, []
        return out

    def __iter__(self):
        return iter(self.pop())

    def __next__(self) -> Event:
        if not self._events:
            raise StopIteration
        return self._events.pop(0)

    # ── внутреннее ───────────────────────────────────────────────────────────

    def _emit(self, kind: EventKind, data: bytes = b'', **meta) -> None:
        self._events.append(Event(kind, data, self._pos, meta))

    def _take(self, n: int) -> bytes:
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    def _skip(self, n: int) -> None:
        del self._buf[:n]
        self._pos += n

    # ── автомат ──────────────────────────────────────────────────────────────

    def _scan(self) -> None:
        while self._buf:
            st = self._state
            if st is _State.VUK_HEADER:
                if len(self._buf) < VUK_HEADER_LEN:
                    return
                self._skip(VUK_HEADER_LEN)
                self._state = _State.VUK_PAYLOAD
                continue

            if st is _State.VUK_PAYLOAD:
                if not self._vuk_payload_step():
                    return
                continue

            if st is _State.DEFCHAR_CODE:
                if len(self._buf) < 7:
                    return
                self._defcode = self._take(7)[0] & 0o77
                self._defrows = []
                self._pos += 6
                self._state = _State.DEFCHAR_ROWS
                continue

            if st is _State.DEFCHAR_ROWS:
                if not self._defrow_step():
                    return
                continue

            if not self._normal_step():
                return

    # ── обычный режим ────────────────────────────────────────────────────────

    def _normal_step(self) -> bool:
        """Обработать очередной маркер или запись. False — данных мало."""
        head = bytes(self._buf[:4])

        # конец строки текста: ESC 0o306 0o336 0o215
        if head == SC_TXT_LINE:
            self._skip(4)
            self._text_mode = False
            self._emit(EventKind.TEXT_LINE)
            return True

        # возврат к русскому шрифту: 0o273 0o240
        if head[:2] == bytes((0o273, SC_ROW)):
            self._skip(2)
            self._emit(EventKind.RU_FONT)
            return True

        # заполнитель кадра: 0o32 ВК (терминал не выполняет операции)
        if head[:2] == PAD_CMD:
            self._skip(2)
            self._emit(EventKind.PAD)
            return True

        # начало блока ВУК: '/' ВК
        if head[:2] == VUK_BLOCK:
            self._skip(2)
            self._state = _State.VUK_HEADER
            self._emit(EventKind.VUK_BLOCK, VUK_BLOCK, vuk=True)
            return True

        # загрузка пользовательского знака: 0o110 0o240
        if head[:2] == bytes((0o110, SC_ROW)):
            self._skip(2)
            self._emit(EventKind.DEFCHAR)
            self._state = _State.DEFCHAR_CODE
            return True

        # маркер конца потока
        if head[:1] == bytes((END,)):
            self._skip(1)
            self._emit(EventKind.END_OF_STREAM)
            return True

        # обычная запись: ищем ВК
        idx = self._buf.find(VK)
        if idx < 0:
            return False
        rec = bytes(self._buf[:idx + 1])
        self._skip(idx + 1)

        # ESC-последовательность кадра
        if rec[:1] == bytes((AR2,)):
            self._text_mode = False
            self._emit_frame_marker(rec)
            return True

        # Запись `9 S` — запоминаем размер шрифта для следующих данных.
        body = rec[:-1]
        if not self._text_mode and len(body) == 2 and body[:1] == b'9':
            self._text_size = body[1:]
            self._emit(EventKind.COMMAND, rec, text_size=self._text_size)
            return True

        # Данные текста: запись `;O <строка>` открывает текстовый режим,
        # последующие записи — продолжение строки (процедура `СТРОКИ`).
        if rec[:1] == b';' or self._text_mode:
            self._text_mode = True
            self._emit(EventKind.COMMAND, rec, text=True)
            return True

        self._emit(EventKind.COMMAND, rec)
        return True

    def _emit_frame_marker(self, rec: bytes) -> None:
        """Разобрать ESC-запись кадра.

        В потоке встречается и отдельный маркер конца кадра
        (`АР2 'F‾' ВК`), и объединённая запись
        (`АР2 'У  ' АР2 'F‾' ВК`) — в последнем случае обе границы
        кадра приходят одной записью.
        """
        fn = rec[1:2]
        is_begin = fn != b'F'
        self._emit(EventKind.FRAME_BEGIN if is_begin
                   else EventKind.FRAME_END, rec)
        if is_begin and bytes((AR2,)) + b'F' in rec[1:]:
            self._emit(EventKind.FRAME_END, rec)

    # ── блок ВУК ─────────────────────────────────────────────────────────────

    def _vuk_payload_step(self) -> bool:
        """Собрать полезную нагрузку блока ВУК до маркера `АР2 'F‾' ВК`."""
        idx = self._buf.find(AR2)
        while idx >= 0:
            tail = bytes(self._buf[idx:idx + 5])
            if len(tail) >= 3 and tail[1:2] == b'F':
                payload = bytes(self._buf[:idx])
                # маркер: АР2 'F' [хвост] ВК
                n = 2
                if len(tail) >= 4 and tail[2:3] != bytes((VK,)):
                    n = 3
                if len(tail) >= n + 1 and tail[n:n + 1] == bytes((VK,)):
                    n += 1
                elif len(tail) < n + 1:
                    return False
                if payload:
                    # Маркер входа в кадр `АР2 'У  '` остался в хвосте
                    # предыдущего блока — выделяем его из полезной нагрузки.
                    while payload[:1] == bytes((AR2,)):
                        self._emit(EventKind.FRAME_BEGIN,
                                   payload[:4])
                        payload = payload[4:]
                    if payload:
                        self._emit(EventKind.COMMAND, payload, payload=True)
                self._skip(idx + n)
                self._state = _State.NORMAL
                return True
            idx = self._buf.find(AR2, idx + 1)
        return False

    # ── пользовательский знак ───────────────────────────────────────────────

    def _defrow_step(self) -> bool:
        """Собрать строки пользовательского знака до `0o215`."""
        while self._buf:
            b = self._buf[0]
            if b == SC_ROW:                       # начало строки
                self._skip(1)
                if len(self._buf) < 3:
                    return False
                self._defrows.append(self._take(3)[0] & 0o377)
                self._pos += 2
                continue
            if b == SC_ENDTEXT:                   # конец знака
                self._skip(1)
                self._emit(EventKind.COMMAND,
                           bytes((0o110, SC_ROW)) +
                           bytes((self._defcode,)) +
                           bytes(self._defrows) +
                           bytes((SC_ENDTEXT,)),
                           defchar=True)
                self._state = _State.NORMAL
                return True
            # Неожиданный байт — пропускаем.
            self._skip(1)
        return False