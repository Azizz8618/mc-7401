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
    c = MachineClock(enabled=True)
    assert not c.known
    assert c.stamp() == ''
    assert c.stale
    assert c.due(), 'первый опрос включённых часов должен быть сразу'


def test_clock_is_silent_by_default():
    """По умолчанию ЭВМ не спрашивают: директива печатается на экране.

    Запрос уходит в линию сам и виден в выдаче — а на экране может идти
    выдача задачи, и строка `ВРЕ ДАТА:…` сбивает её чтение.  Поэтому
    выключатель по умолчанию выключен, и «пора спросить» не наступает
    никогда, сколько бы времени ни шли часы.
    """
    c = MachineClock()
    assert not c.enabled
    assert not c.due(), 'выключенные часы не просят ЭВМ'
    assert c.stamp() == ''
    # И через «много периодов» — молчат.
    c._at = time.monotonic() - 100 * c.POLL_SECONDS
    assert not c.due(), 'выключенные часы не должны оживать со временем'


def test_disabled_clock_still_reads_a_manual_directive():
    """Если `ВРЕ` набрал оператор, ответ разбирается и время видно.

    Выключатель запрещает терминалу **спрашивать самому**, а не
    разбирать ответ: вопрос задал человек, и молчать было бы глупо.
    """
    c = MachineClock()
    assert c.feed(LIVE_REPLY)
    assert c.stamp() == '13.10 22:06', c.stamp()
    assert not c.due(), 'и после ответа выключенные часы не просят'


def test_clock_advances_by_itself_between_polls():
    """Между опросами время идёт само, а не с системных часов."""
    c = MachineClock(enabled=True)
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
    c = MachineClock(enabled=True)
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


# ── выключатель: настройка, окно, линия ────────────────────────────────────

def test_machine_time_is_off_by_default():
    """По умолчанию ЭВМ не спрашивают."""
    from mc7401.config import Config
    assert Config.default().machine_time is False


def test_machine_time_setting_is_read_from_the_file(tmp_path):
    """`[line] machine_time = true` включает запрос."""
    from mc7401.config import Config
    path = tmp_path / 'mc7401.ini'
    path.write_text('[line]\nmachine_time = true\n', encoding='utf-8')
    assert Config.load(path).machine_time is True
    path.write_text('[line]\nmachine_time = false\n', encoding='utf-8')
    assert Config.load(path).machine_time is False


def test_template_ships_the_option_switched_off():
    """В шаблоне настроек опция есть и выключена — иначе молча сработает."""
    from mc7401.config import SAMPLE
    line = [ln for ln in SAMPLE.splitlines()
            if ln.strip().startswith('machine_time')]
    assert line, 'в шаблоне настроек нет machine_time'
    assert line[0].split('=', 1)[1].strip() == 'false', line[0]


class _Recorder:
    """Линия, которая только запоминает, что в неё отправили."""

    name = 'запись'
    greeting = b''

    def __init__(self):
        self.sent: list[str] = []

    def poll(self, t=0.05):
        return b''

    def send_keys(self, text):
        self.sent.append(text)

    def close(self):
        pass


def _window(monkeypatch, **cfg_kw):
    from test_frontend import FakeCanvas, FakePhoto, FakeRoot, FakeText
    from mc7401 import window as window_mod
    from mc7401.config import Config
    cfg = Config.default()
    for key, value in cfg_kw.items():
        setattr(cfg, key, value)
    driver = _Recorder()
    win = window_mod.TerminalWindow(driver, scale=0, config=cfg)
    win._canvas = FakeCanvas(1200, 900)
    win._image_id = 1
    win.lbl_status = FakeText()
    win.lbl_panel = FakeText()
    win._tk = FakeRoot()
    return win, driver


def test_window_sends_nothing_to_the_line_by_default(monkeypatch):
    """Без настройки терминал не отправляет в линию ничего.

    Директива `ВРЕ` печатается на экране и мешает читать выдачу задачи,
    поэтому по умолчанию она не отправляется вовсе — даже если линия
    подключена и часы тикают.
    """
    win, driver = _window(monkeypatch)
    assert not win.mclock.enabled
    for _ in range(5):
        win._ask_machine_time()
    assert not driver.sent, f'в линию ушло: {driver.sent!r}'


def test_window_sends_the_directive_when_asked(monkeypatch):
    """С настройкой `machine_time = true` директива уходит."""
    from mc7401.machine_time import TIME_QUERY
    win, driver = _window(monkeypatch, machine_time=True)
    assert win.mclock.enabled
    win._ask_machine_time()
    assert driver.sent == [TIME_QUERY.decode('latin-1')], driver.sent


def test_status_line_falls_back_to_computer_time_when_off(monkeypatch):
    """Пока ЭВМ не спрашивают, в строке состояния время компьютера.

    И обязательно с меткой `КОМП`: без неё время хоста выдавало бы себя
    за время ЭВМ, а расходятся они на стенде на два с лишним часа.
    """
    win, _ = _window(monkeypatch)
    assert win.mclock.stamp() == ''
    line = ' '.join(win._status_lines())
    assert 'КОМП' in line, line
    assert 'ВРЕМЯ' not in line and '13.10' not in line, line
