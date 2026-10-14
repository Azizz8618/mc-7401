"""
test_machine_time.py — время в строке состояния берётся у ЭВМ, а не у
компьютера.

Основание: на живой линии ЭВМ на директиву `ВРЕ` отвечала
`ДАТА:13.10.26 ВРЕМЯ:22.06.59`, а баннер линии в тот же момент несел
`19:45` — время хостовое и время ЭВМ расходились.  Часы компьютера в
строке состоянии вводили в заблуждение, поэтому их заменили.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mc7401 import chars                                    # noqa: E402
from mc7401.machine_time import (MachineClock, TIME_DIRECTIVE,  # noqa: E402
                                 TIME_QUERY, parse_machine_time)

#: Настоящий ответ ОС, снятый с живой линии ДКС.
#: `ВРЕ ДАТА:13.10.26 ВРЕМЯ:22.06.59` — кириллица пришла кодами КОИ-7
#: и выглядит латиницей: `BPE ДATA:13.10.26 BPEMЯ:22.06.59`.
LIVE_REPLY = bytes.fromhex(
    '42504520444154413a31332e31302e3236'
    '2042504 54d713a32322e30362e3539'
    .replace(' ', ''))


def test_parse_live_reply_from_dks_line():
    """Ответ с линии ДКС разбирается — коды КОИ-7, кириллица латиницей."""
    shown = ''.join(chars.display_dks(b) for b in LIVE_REPLY)
    assert shown == 'BPE DATA:13.10.26 BPEMЯ:22.06.59'
    assert parse_machine_time(LIVE_REPLY) == (13, 10, 26, 22, 6, 59)


def test_parse_accepts_real_cyrillic():
    """Тот же ответ настоящей кириллицей (из эмулятора, UTF-8)."""
    text = 'ВРЕ ДАТА:13.10.26 ВРЕМЯ:22.06.59'
    assert parse_machine_time(text.encode('utf-8')) == (13, 10, 26, 22, 6, 59)


def test_parse_ignores_everything_else():
    """Ни баннер, ни приглашение ОС, ни мусор временем не считаются."""
    for data in (b'Connected to the simulator TTY device, line 10',
                 bytes([0x7C, 0x42, 0x4D, 0x2D, 0x33, 0x2C,
                        0x54, 0x30, 0x32, 0x31]),      # ЭBM-3,T021
                 b'', b'\x00\x01\x02'):
        assert parse_machine_time(data) is None, data


def test_directive_is_koi7_vre_and_needs_return():
    """Директива — `ВРЕ` кодами КОИ-7 и оканчивается переводом строки.

    Без `ПРПС` ОС её не выполняет: проверено на линии — приходило только
    эхо, ответа не было.
    """
    assert TIME_DIRECTIVE == b'BPE'
    assert ''.join(chars.display_dks(b) for b in TIME_DIRECTIVE) == 'BPE'
    assert TIME_QUERY == b'BPE\r'


def test_clock_starts_unknown_and_stamps_nothing():
    """Пока ЭВМ не ответила, времени нет — подставлять время хоста нельзя."""
    c = MachineClock()
    assert not c.known
    assert c.stamp() == ''
    assert c.stale
    assert c.due(), 'первый опрос должен быть сразу'


def test_clock_advances_by_itself_between_polls():
    """Между опросами время идёт само, а не с системных часов."""
    c = MachineClock()
    assert c.feed(LIVE_REPLY)
    assert c.known and not c.stale
    first = c.now()
    time.sleep(1.1)
    second = c.now()
    assert first is not None and second is not None
    # Секунды идут: сравниваем по времени суток в целом, а не по полю
    # секунд — за секунду теста может перейти минутная граница.
    def secs(v):
        return v[3] * 3600 + v[4] * 60 + v[5]
    assert 1 <= secs(second) - secs(first) <= 2, (first, second)
    assert c.stamp(full=True).endswith(f'{second[5]:02d}')


def test_clock_polls_once_per_period():
    """Опрос не частится: часы идут сами, линия не засоряется."""
    c = MachineClock()
    c.feed(LIVE_REPLY)
    assert not c.due(), 'сразу после ответа опроса быть не должно'
    c._at -= c.POLL_SECONDS + 1          # как будто прошёл период
    assert c.due()


def test_clock_goes_stale_when_the_line_falls_silent():
    """Линия молчит — часы помечаются устаревшими, а не врут."""
    c = MachineClock()
    c.feed(LIVE_REPLY)
    assert not c.stale
    c._at -= 3 * c.POLL_SECONDS
    assert c.stale, 'устаревшее время должно быть видно'


def test_status_line_shows_machine_time(monkeypatch):
    """Строка состояния показывает время ЭВМ, а при его отсутствии — КОМП."""
    from mc7401 import chrome, panel
    from mc7401.panel import Status
    st = Status(banner=True, link_kind='ДКС', machine=3, terminal=21)
    text = panel.render_plain(chrome.status_rows(st, clock='13.10 22:07')[0])
    assert '13.10 22:07' in text, text
    assert 'КОМП' not in text, 'время ЭВМ не должно выдаваться за компьютерное'
    # Без ответа ЭВМ время компьютера помечается, чтобы спутать нельзя.
    text2 = panel.render_plain(chrome.status_rows(st)[0])
    assert 'КОМП' in text2, text2
