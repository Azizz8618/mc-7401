"""
machine_time.py — время БЭСМ-6, а не время компьютера.

Строка состояния показывала `datetime.now()` — время машины, на которой
лежит терминал. На стенде это выглядит правдоподобно, но это **не то
время**: ЭВМ ведёт счёт по своим часам, и на живой линии они расходятся
с хостовыми (проверено: ЭВМ отвечала `ВРЕМЯ:22.04.15`, а баннер линии
в тот же момент несла `19:45`). Оператор, сверявший время работы по
часам терминала, сверял не с тем.

Время берётся у ОС Диспак директивой `ВРЕ` — она же `ДАТА` и `ВРЕМЯ`
одной строкой:

    >>> ВРЕ⏎
    ДАТА:13.10.26 ВРЕМЯ:22.04.15

Директива набирается кодами КОИ-7 (`ВРЕ` = `BPE`) и **окончивается
ПРПС**: без перевода строки ОС её не выполняет (проверено на линии —
эхо возвращалось, ответа не было).

Между опросами время идёт само: часы не спрашивают каждую секунду, а
продвигают последнее известное значение на прошедшее.  Опрос раз в
`POLL_SECONDS`; при обрыве линии часы помечаются как **устаревшие**,
и строка состояния показывает это прямо, а не молчащее время хоста.

    >>> from mc7401.machine_time import MachineClock
    >>> c = MachineClock()
    >>> c.feed('ДАТА:13.10.26 ВРЕМЯ:22.04.15'.encode('koi8-r'))
    True
    >>> c.stamp()
    '13.10 22:04:15'
    >>> c.stale
    False
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

from . import chars

log = logging.getLogger(__name__)

#: Директива запроса времени.  `ВРЕ` кодами КОИ-7: `В`=0x42, `Р`=0x50,
#: `Е`=0x45 — латинские омоглифы кириллицы, как их шлёт знакогенератор.
TIME_DIRECTIVE = b'BPE'
#: Директива оканчивается переводом строки: без `ПРПС` ОС её не выполняет.
TIME_DIRECTIVE_EOL = b'\r'
#: Что уходит в линию целиком.
TIME_QUERY = TIME_DIRECTIVE + TIME_DIRECTIVE_EOL

#: Ответ ОС: `ДАТА:ДД.ММ.ГГ ВРЕМЯ:ЧЧ.ММ.СС`.
#:
#: Буквы приходят **двумя** способами, и образец обязан понимать оба:
#:
#: * по линии ДКС — кодами КОИ-7, где кириллица выглядит латиницей:
#:   `Д` приходит как `D` (0x44), `ВРЕМЯ` — как `BPEM` с `Я` (0x71);
#: * из эмулятора — настоящей кириллицей в UTF-8.
#:
#: Поэтому у **каждой** буквы класса из обеих форм: иначе `Д` совпал бы,
#: а следующая за ней `А` — нет (`[DД]ATA:` не находит `ДАТА:`).
_TIME_RE = re.compile(
    r'[DД][AА][TТ][AА]:\s*(\d{2})\s*\.\s*(\d{2})\s*\.\s*(\d{2})'
    r'.*?'
    r'[BВ][PР][EЕ][MМ][ЯЯ]:\s*(\d{2})\s*\.\s*(\d{2})\s*\.\s*(\d{2})',
    re.IGNORECASE | re.DOTALL)


def parse_machine_time(data: bytes) -> Optional[tuple[int, int, int, int, int, int]]:
    """Разобрать ответ ОС на `ВРЕ`.

    Возвращает `(день, месяц, год2, час, минута, секунда)` либо `None`.
    Байты читаются через `chars.display_dks`, поэтому берутся и коды
    КОИ-7 с линии, и UTF-8 из эмулятора, и «всё сразу».
    """
    if not data:
        return None
    texts = []
    try:
        texts.append(data.decode('utf-8', errors='replace'))
    except UnicodeDecodeError:                      # pragma: no cover
        pass
    texts.append(''.join(chars.display_dks(b) for b in data))
    for text in texts:
        m = _TIME_RE.search(text)
        if m:
            return tuple(int(g) for g in m.groups())   # type: ignore[return-value]
    return None


@dataclass
class MachineClock:
    """Часы ЭВМ: последнее известное время и его ход между опросами.

    Время **движется само** между опросами: счёт идёт от последнего
    полученного от ЭВМ значения, а не от системных часов компьютера.
    Иначе часы терминала прыгали бы назад-вперёд вместе с хостовыми.
    """

    #: Как часто спрашивать у ОС, с.  Раз в минуту — счёт идёт сам, а
    #: лишний трафик по линии ДКС оператору не нужен.
    POLL_SECONDS = 60

    #: `(день, месяц, год2, час, минута, секунда)` от ЭВМ.
    value: Optional[tuple[int, int, int, int, int, int]] = None
    #: Монотонное время, когда часы приняли значение; `None` — ещё нет.
    _at: Optional[float] = None

    def feed(self, data: bytes) -> bool:
        """Разобрать ответ ОС.  `True`, если время получено."""
        parsed = parse_machine_time(data)
        if parsed is None:
            return False
        self.value = parsed
        self._at = time.monotonic()
        return True

    @property
    def known(self) -> bool:
        """Известно ли время ЭВМ."""
        return self.value is not None

    @property
    def stale(self) -> bool:
        """Устарело ли время: прошло больше двух периодов опроса.

        Значит, линия молчит и показанное время уже неправдоподобно —
        строка состояния говорит об этом прямо.
        """
        if self._at is None:
            return True
        return time.monotonic() - self._at > 2 * self.POLL_SECONDS

    def due(self) -> bool:
        """Пора ли опросить ЭВМ."""
        if self._at is None:
            return True
        return time.monotonic() - self._at >= self.POLL_SECONDS

    def now(self) -> Optional[tuple[int, int, int, int, int, int]]:
        """Текущее время ЭВМ с точностью до секунды.

        Между опросами к принятому значению прибавляется прошедшее
        время; при переходе через полночь день увеличивается, месяц и
        год — нет: смена месяца без прихода новой выдачи не нужна.
        """
        if self.value is None or self._at is None:
            return None
        day, month, year, hh, mm, ss = self.value
        add = int(time.monotonic() - self._at)
        total = hh * 3600 + mm * 60 + ss + add
        day += total // 86400
        total %= 86400
        return (day, month, year, total // 3600, total // 60 % 60, total % 60)

    def stamp(self, *, full: bool = False) -> str:
        """Время для строки состояния.

        Пустая строка — время неизвестно: подставлять время компьютера
        нельзя, оператор принял бы его за время ЭВМ.
        """
        now = self.now()
        if now is None:
            return ''
        day, month, year, hh, mm, ss = now
        base = f'{day:02d}.{month:02d} {hh:02d}:{mm:02d}'
        return f'{base}:{ss:02d}' if full else base


__all__ = ['MachineClock', 'parse_machine_time',
           'TIME_DIRECTIVE', 'TIME_QUERY']