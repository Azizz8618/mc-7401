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

#: Коды `≡240` (`SC_ROW`) в потоке.
#:
#: На реальной линии приходит код 0o240 как есть.  В потоках, снятых
#: нашими средствами (`tools/pascal/runtime.py`), `≡240` проходит через
#: КОИ-8 как NBSP и приходит 0o232 (то же — для `≡267` и других
#: типографских кодов).  Оба варианта допустимы.
SC_ROW_CODES = (SC_ROW, 0o232)

#: Служебный хвост `ВНУК`: `≡257≡215` и заполнитель нулями.
VUK_LINE_END = bytes((0o257, SC_ENDTEXT))

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
        #: Последняя запись `9 S ВК` — её данные придут следующей записью.
        self._text_size: bytes = b''
        #: Накопленные данные строки текста.
        self._text_buf = bytearray()
        self._text_mode = False
        #: Строка текста без записей `ВК` (данные до `≡215`).
        self._row_mode = False

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
        self._flush_text()
        if self._row_mode and self._buf:
            row, self._row_mode = bytes(self._buf), False
            self._buf.clear()
            self._emit(EventKind.COMMAND, row, row=True)
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

            if not self._normal_step():
                return

    # ── обычный режим ────────────────────────────────────────────────────────

    def _normal_step(self) -> bool:
        """Обработать очередной маркер или запись. False — данных мало."""
        head = bytes(self._buf[:4])

        # конец строки текста: ESC 0o306 0o336 0o215
        if head == SC_TXT_LINE:
            self._skip(4)
            self._flush_text()
            self._text_mode = False
            self._emit(EventKind.TEXT_LINE)
            return True

        # Строка набора: 0o273 0o240 … 0o215 (так пишет строки `СИМВОЛЫ`
        # в graf.pas).  Пара — признак строки данных: знаки идут до
        # `≡215` без записей `ВК`; регистр (русский/латинский) задают
        # коды `0o16`/`0o17` внутри строки, поэтому сам маркер шрифт
        # не меняет: таблица знаков ведёт и латинскую, и русскую секции.
        if head[:2] in (bytes((0o273, SC_ROW)), bytes((0o273, 0o232))):
            self._skip(2)
            self._row_mode = True
            return True

        # данные строки текста — до `≡215` (конец текста)
        if self._row_mode:
            return self._text_row_step()

        # Служебный хвост ВНУК: `≡257≡215` и заполнитель нулями.  Встречается
        # после строк набора и после блоков знаков; без обработчика он
        # съедал следующую строку вместе с записью.
        if bytes(self._buf[:2]) == VUK_LINE_END:
            self._skip(2)
            zeros = 0
            while zeros < 14 and self._buf[:1] == b'0':
                self._skip(1)
                zeros += 1
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

        # Описание пользовательского знака: `≡110≡240 … ≡215`
        if head[:2] in (bytes((0o110, SC_ROW)), bytes((0o110, 0o232))):
            end = self._buf.find(SC_ENDTEXT)
            if end < 0:
                return False
            block = bytes(self._buf[:end + 1])
            self._skip(end + 1)
            self._emit(EventKind.DEFCHAR, block, defchar=True)
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
        # а процедура `СТРОКИ` дописывает символы по одному
        # (`WРИТЕ(INРUТ↑)`), каждый — отдельной записью.  Продолжения
        # накапливаются и отдаются одной записью при `≡033≡306≡336≡215`
        # (конец строки текста) либо перед следующей командой — иначе
        # каждый символ разбирался бы как отдельная команда.
        if rec[:1] == b';' or self._text_mode:
            if len(rec) <= 2:            # одиночный символ — продолжение
                self._text_mode = True
                # Разделитель записей `ВК` внутрь строки не попадает.
                piece = rec[:-1] if rec.endswith(bytes((VK,))) else rec
                if rec[:1] == b';':
                    self._flush_text()
                    self._text_buf = bytearray(piece)
                else:
                    self._text_buf += piece
                return True
            # Строка закончилась: отдаём накопленное, запись разбирается
            # как обычная команда.
            self._flush_text()
            self._text_mode = False

        self._emit(EventKind.COMMAND, rec)
        return True

    def _flush_text(self) -> None:
        """Отдать накопленную строку текста как одну запись."""
        if not self._text_buf:
            return
        buf, self._text_buf = bytes(self._text_buf), bytearray()
        self._emit(EventKind.COMMAND, buf, text=True)

    def _text_row_step(self) -> bool:
        """Строка текста без записей `ВК`: знаки идут до `≡215` (конец текста).

        Так строки набора пишет `СИМВОЛЫ` в graf.pas: после пары
        `≡033≡306≡336≡215` `≡273≡240` следуют знаки строки, за ними
        `≡215` и служебный хвост `ВНУК`.  У строки, которая заканчивается
        хвостом напрямую (демо-строка заказных знаков), перед `≡215`
        стоит `≡257` — он часть хвоста, а не знак.
        """
        end = self._buf.find(SC_ENDTEXT)
        if end < 0:
            return False
        row = bytes(self._buf[:end])
        self._skip(end + 1)
        self._row_mode = False
        if row.endswith(VUK_LINE_END[:1]):       # `≡257` — начало хвоста ВНУК
            row = row[:-1]
            zeros = 0
            while zeros < 14 and self._buf[:1] == b'0':
                self._skip(1)
                zeros += 1
        if row:
            self._emit(EventKind.COMMAND, row, row=True)
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
                    # Маркер входа в кадр `АР2 'У  '` приходит в хвосте
                    # блока (перед `АР2 'F‾' ВК`, завершающим кадр):
                    # выделяем его из полезной нагрузки, иначе он портит
                    # разбор последней команды блока.
                    while len(payload) >= 4 and payload[-4:-3] == bytes((AR2,)):
                        self._emit(EventKind.FRAME_BEGIN, payload[-4:])
                        payload = payload[:-4]
                marker = bytes(self._buf[idx:idx + n])
                del self._buf[idx:idx + n]
                if payload:
                    # Нагрузка блока — обычные записи и строки текста
                    # (`СИМВОЛЫ`): возвращаем её в разбор, а не считаем
                    # одной нераспознанной командой.
                    self._buf[:idx] = payload
                self._pos += n
                self._emit(EventKind.FRAME_END, marker)
                self._state = _State.NORMAL
                return True
            idx = self._buf.find(AR2, idx + 1)
        return False