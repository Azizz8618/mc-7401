"""
test_link_echo.py — протокол линии ДКС: эхо и запрос приглашения ОС.

Поведение взято из эталона 15ИЭ (`ie15emu/session.py::_queue_echo`,
`_strip_pending_echo`; `agent.py` — `drain()`, затем `key("KEY_ENTER")`)
и проверено на живой машине:

* линия ДКС всегда эхит отданные знаки: ПР возвращается ПС,
  ВК — тройкой «ВК ПРОБЕЛ ВК»;
* приглашение ОС «ЭВМ-3,ТNNN» запрашивается ПРПС (пустой строкой) и
  повторяется, пока Э-60 не зарегистрируется в ОС (ранние строки
  ОС проглатывает);
* окончание строки на ДКС — ПР (`\r`), ETX для линии просто знак.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mc7401.link import EchoStrip                              # noqa: E402


# ── срезка эха ─────────────────────────────────────────────────────────────

def test_echo_transforms_are_line_specific():
    """ПР эхится ПС, ВК — тройкой «ВК ПРОБЕЛ ВК» (живая линия)."""
    e = EchoStrip()
    e.sent(b'set\r')
    assert bytes(e.pending) == b'set\n'
    e2 = EchoStrip()
    e2.sent(b'\x08')
    assert bytes(e2.pending) == b'\x08 \x08'


def test_echo_is_stripped_from_stream():
    """Эхо-ответ на «сет»+ПР срезается, ответ ЭВМ остаётся."""
    e = EchoStrip()
    e.sent(b'set\r')
    out = e.strip(b'set\n o{ib\r\n')
    assert out == b' o{ib\r\n'


def test_echo_cr_and_lf_are_interchangeable():
    """Линия эхит ПР как ПС (и наоборот) — сверка не различает их."""
    e = EchoStrip()
    e.sent(b'\r')
    assert e.strip(b'\r') == b''          # эхо пришло ПР, ждали ПС
    e2 = EchoStrip()
    e2.sent(b'\r')
    assert e2.strip(b'\n') == b''


def test_echo_of_bs_erases_on_screen():
    """Эхо ВК = ВК ПРОБЕЛ ВК — визуальное стирание знака."""
    e = EchoStrip()
    e.sent(b'\x08')
    assert e.strip(b'\x08 \x08') == b''


def test_mismatch_stops_stripping_but_keeps_queue():
    """Совпавший префикс — эхо; несовпадение — вывод машины.

    Очередь сохраняется: эхо опоздавшего знака придёт позже и
    будет срезано (эталон 15ИЭ, `_strip_pending_echo`).
    """
    e = EchoStrip()
    e.sent(b'ab')
    out = e.strip(b'ax')
    assert out == b'x'                    # 'a' — эхо, 'x' — вывод
    assert bytes(e.pending) == b'b'       # ждём эхо второго знака
    assert e.strip(b'b') == b''


def test_unmatched_stream_passes_through():
    """Без ожидающего эхо поток проходит как есть."""
    e = EchoStrip()
    assert e.strip(b' |BM-3,T020\r\n') == b' |BM-3,T020\r\n'


# ── запрос приглашения ОС ──────────────────────────────────────────────────

class FakeLine:
    """Фейк линии ДКС: записывает отправленное, отдаёт заданный приём."""

    instances: list = []

    def __init__(self, host, ports):
        self.port = 4202
        self.greeting = b''
        self.sent: list = []
        self.rx: list = []
        FakeLine.instances.append(self)

    def connect(self) -> None:
        pass

    def send(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, size: int = 4096, timeout=None) -> bytes:
        return self.rx.pop(0) if self.rx else b''

    def close(self) -> None:
        pass


def _driver(monkeypatch, *, prompt=True):
    """Драйвер на поддельной линии; `prompt` — автозапрос приглашения.

    По умолчанию здесь `prompt=True`: проверки ниже относятся именно к
    механизму запроса, а молчание линии по умолчанию проверяется
    отдельно (`test_no_prompt_requested_by_default`).
    """
    from mc7401 import link as linkmod
    from mc7401.tui import LiveDriver
    FakeLine.instances.clear()
    monkeypatch.setattr(linkmod, 'DKSLink', FakeLine)
    return (LiveDriver('127.0.0.1:4202', prompt=prompt),
            FakeLine.instances[0])


def test_no_prompt_requested_by_default(monkeypatch):
    """По умолчанию линия не трогается: терминал молчит до ПРПС.

    Автозапрос был нужен только для проверки работоспособности на
    начальном этапе.  Теперь терминал подключается и не отправляет
    ничего, пока оператор не нажмёт ПРПС.
    """
    from mc7401.config import Config
    assert Config.default().prompt is False, 'автозапрос включён по умолчанию'
    drv, line = _driver(monkeypatch, prompt=False)
    for _ in range(10):
        drv._prompt_at = 0.0
        drv.poll(0)
    assert line.sent == [], 'терминал что-то отправил в линию сам'
    assert not drv._prompted


def test_prompt_is_still_available(monkeypatch):
    """Механизм запроса не выброшен: его включает `[line] prompt`."""
    drv, line = _driver(monkeypatch, prompt=True)
    assert drv.auto_prompt
    drv._prompt_at = 0.0
    drv.poll(0)
    assert line.sent == [b'\r']


def test_config_reads_prompt_flag(monkeypatch, tmp_path):
    """`[line] prompt = true` в файле включает автозапрос."""
    from mc7401.config import Config
    path = tmp_path / 'mc7401.ini'
    path.write_text('[line]\nprompt = true\n', encoding='utf-8')
    assert Config.load(path).prompt is True
    assert Config.default().prompt is False


def test_prompt_requested_with_prps(monkeypatch):
    """Приглашение запрашивается ПРПС — тем же ПР, что конец строки."""
    drv, line = _driver(monkeypatch)
    drv._prompt_at = 0.0                  # запрос немедленно
    drv.poll(0)
    assert line.sent == [b'\r']


def test_prompt_repeats_until_answered(monkeypatch):
    """Без ответа ОС запрос повторяется (Э-60 регистрируется не сразу)."""
    drv, line = _driver(monkeypatch)
    for _ in range(3):
        drv._prompt_at = 0.0
        drv.poll(0)
    assert line.sent == [b'\r', b'\r', b'\r']
    assert not drv._prompted


def test_prompt_stops_after_machine_answered(monkeypatch):
    """После ответа машины (приглашение «ЭВМ-3,ТNNN») запросы кончаются."""
    drv, line = _driver(monkeypatch)
    drv._prompt_at = 0.0
    drv.poll(0)
    line.rx.append(b' |BM-3,T020\r\n')
    data = drv.poll(0)
    assert b'|BM-' in data
    assert drv._prompted
    drv._prompt_at = 0.0
    drv.poll(0)
    assert line.sent == [b'\r']           # повторных запросов нет


def test_prompt_gives_up_after_max_tries(monkeypatch):
    """Число повторов конечно — дальше жмёт оператор (ПРПС)."""
    drv, line = _driver(monkeypatch)
    for _ in range(drv.PROMPT_TRIES + 3):
        drv._prompt_at = 0.0
        drv.poll(0)
    assert len(line.sent) == drv.PROMPT_TRIES


def test_send_keys_records_echo(monkeypatch):
    """Отданные знаки попадают в очередь ожидаемого эха.

    Набор ввода — Н2 (КОИ-7): строчная латинская уходит **заглавной**
    (`ab` → `AB`), и в очередь эха кладётся ровно то, что ушло в линию.
    """
    drv, line = _driver(monkeypatch)
    drv.send_keys('ab')
    assert line.sent == [b'AB']
    assert bytes(drv._echo.pending) == b'AB'
    # эхо срезается из приёма
    assert drv.poll(0) == b''
    line.rx.append(b'AB')
    assert drv.poll(0) == b''


def test_stale_echo_queue_stops_eating_machine_output(monkeypatch):
    """Просроченная очередь эха не съедает выдачу ЭВМ.

    Внутри программы ЭВМ линия не эхит: очередь накапливалась, а когда
    машина наконец отвечала, срезка съедала первые байты ответа — выдача
    массива по `В` начинается с таких же байтов и пропадала.  Через
    `ECHO_TTL` очередь снимается целиком.
    """
    drv, line = _driver(monkeypatch)
    drv.send_keys('В')                   # линия не эхит (внутри программы)
    drv._echo._sent_at -= drv._echo.ECHO_TTL + 1
    answer = 'В\r\n' + '4103 2530'
    line.rx.append(answer.encode('koi8-r'))
    assert drv.poll(0) == answer.encode('koi8-r')
    assert not drv._echo.pending


def test_echo_queue_is_bounded(monkeypatch):
    """Очередь эха не растёт без предела."""
    drv, line = _driver(monkeypatch)
    drv.send_keys('x' * 500)
    assert len(drv._echo.pending) <= drv._echo.MAX_PENDING
