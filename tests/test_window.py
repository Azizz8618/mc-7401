"""Проверки окна терминала: вывод кадра, текст ЭВМ, ввод, графика.

Собрано по жалобам на стенде: строки двоились, приглашения ЭВМ не было
видно, символы не передавались.
"""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mc7401 import chars                                          # noqa: E402
from mc7401.config import Config                                   # noqa: E402
from mc7401.tui import TerminalApp                                # noqa: E402


class FakeDriver:
    """Драйвер без линии: всё заранее известно, отправка собирается."""

    name = 'фейк'
    greeting = b''

    def __init__(self, data: bytes = b'') -> None:
        self.data = data
        self.sent: list[str] = []
        self.closed = False

    def poll(self, timeout: float) -> bytes:
        data, self.data = self.data, b''
        return data

    def send_keys(self, text: str) -> None:
        self.sent.append(text)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def app():
    a = TerminalApp(FakeDriver(), config=Config.default())
    return a


def draw_capture(app, cols=80, rows=24):
    """Отрисовать кадр и вернуть текст, ушедший в терминал."""
    buf = io.StringIO()
    real, sys.stdout = sys.stdout, buf
    try:
        app.draw()
    finally:
        sys.stdout = real
    return buf.getvalue()


# ── вывод кадра ─────────────────────────────────────────────────────────────

def test_draw_never_scrolls_past_window(monkeypatch):
    """Рядов не больше, чем строк в окне: иначе кадр прокручивает экран."""
    from mc7401 import tui
    monkeypatch.setattr(tui, 'terminal_size', lambda default=(80, 24): (80, 24))
    a = TerminalApp(FakeDriver(), config=Config.default())
    out = draw_capture(a)
    rows = [int(n) for n in re.findall(r'\x1b\[(\d+);1H', out)]
    assert rows, 'кадр не выведен'
    assert max(rows) <= 24, f'кадр вышел за окно: ряд {max(rows)}'
    # Каждый ряд ставится курсором — переводов строки быть не должно.
    assert '\n' not in out, 'в кадре есть переводы строки — будет прокрутка'


def test_draw_erases_row_tail():
    """Остаток строки стирается, иначе следы прежнего кадра остаются."""
    from mc7401 import tui
    tui.terminal_size = lambda default=(80, 24): (80, 24)
    a = TerminalApp(FakeDriver(), config=Config.default())
    out = draw_capture(a)
    assert out.count('\x1b[K') >= a.PANEL_ROWS


def test_panel_below_body():
    """Подвал идёт после кадра и занимает столько рядов, сколько занял.

    Высота подвала зависит от ширины окна: ячейки раскладываются по
    плану `panel`, поэтому она не константа.
    """
    a = TerminalApp(FakeDriver(), config=Config.default())
    panel = a._service_panel(80)
    assert len(panel) == a.panel_rows(80)
    rule = next(n for n, line in enumerate(panel) if line.startswith('-'))
    assert 'РЖМ=' in panel[0], 'первая ячейка — режим сеанса'
    assert any('ПОСЛ=' in line for line in panel[:rule]), \
        'ПОСЛ должен быть над чертой'


# ── текст ЭВМ в окне ─────────────────────────────────────────────────────────

def test_dks_text_is_visible():
    """Приглашение ОС Диспак попадает в строки окна."""
    a = TerminalApp(FakeDriver(), config=Config.default())
    a.feed(b'\x7c\x42\x4d-3,\x54\x30\x31\x37\r\n')
    lines = a._text_lines()
    assert lines, 'текст ЭВМ не разобран'
    assert any('T017' in ln for ln in lines), lines


def test_dks_text_takes_lines_of_window():
    """Текст занимает строки окна (а не прятался в подсказку)."""
    a = TerminalApp(FakeDriver(), config=Config.default())
    a.feed(b'\x54\x30\x31\x37\r\n')          # «T017» (омоглиф «Т»)
    assert a._text_window(80, 5)[0].strip() == 'T017'
    assert len(a._text_window(80, 5)) == 5


def test_text_closes_splash():
    """Первый байт от ЭВМ снимает заставку."""
    a = TerminalApp(FakeDriver(), config=Config.default())
    assert a.splash
    a.feed(b'\r\n')
    assert not a.splash


def test_dks_codes_are_koi7():
    """ОС шлёт семибитные коды КОИ-7, а не восьмибитный КОИ-8Р.

    Русские буквы приходят строкой `0x60…0x7E` — так их печатает
    `koi7_rus_to_unicode` в СИМХ — и показываются заглавными.
    Строка `0x40…0x5E` — латиница: именно её рисует эталонный 15ИЭ
    (`koi7_raw_upper` трогает только `0x60…0x7E`).
    """
    assert chars.display_dks(0x7C) == 'Э'
    assert chars.display_dks(0x4D) == 'M'       # латинская M — омоглиф «М»
    assert chars.display_dks(0x54) == 'T'       # латинская T — омоглиф «Т»
    assert chars.display_dks(0x30) == '0'
    assert chars.display_dks(0x2D) == '-'
    assert chars.display_dks(0x68) == 'Х'       # русская строка — «Х», не «h»
    assert chars.display_dks(0x0D) == ''
    assert chars.display_dks(0x20) == ' '


def test_dks_prompt_reads_evm():
    """Приглашение ОС «ЭВМ-3,Т020» читается как ЭВМ, а не как ЭБМ.

    Байты `7C 42 4D` — «Э» из русской строки плюс латинские `B` и `M`,
    омоглифы «В» и «М»: их глифы читаются как «ЭВМ».  Раньше мы
    переводили `0x42` в «Б» и печатали «ЭБМ» — см. JOURNAL.md.
    """
    wire = bytes([0x7C, 0x42, 0x4D, 0x2D, 0x33, 0x2C, 0x54, 0x30, 0x32, 0x30])
    shown = ''.join(chars.display_dks(b) for b in wire)
    assert shown == 'ЭBM-3,T020', shown


def test_dks_rows_match_reference():
    """Строка `0x60…0x7E` — русская заглавными, `0x40…0x5E` — латиница."""
    from mc7401.chars import RUS7
    for code in (0x60, 0x6D, 0x7C):
        assert chars.display_dks(code) == RUS7[code - 0x60]
    for code in (0x40, 0x42, 0x4D, 0x54):
        assert chars.display_dks(code) == chr(code)


# ── передача символов ────────────────────────────────────────────────────────

def test_host_mode_sends_chars_once(app):
    """В режиме С ЭВМ знак уходит сразу, при ВК — только конец строки."""
    for key in 'СЕТ ':
        app.handle_key(key)
    app.handle_key('\r')
    assert app.driver.sent == ['s', 'e', 't', ' ', '\r']


def test_submit_does_not_resend_line(app):
    """`_ПРПС` не должен слать строку второй раз (был дефект)."""
    for key in 'ЛИН3':
        app.handle_key(key)
    app.driver.sent.clear()
    app.handle_key('\r')
    assert app.driver.sent == ['\r'], app.driver.sent


def test_local_mode_accumulates_then_sends(app):
    """АВТОНОМНО: знаки копятся, ПЕРЕДАЧА уходит один раз."""
    from mc7401.term import KEY_MODE, KEY_SEND
    app.handle_key(KEY_MODE)                  # F9 — СЕТЬ
    assert app.session.mode == 'local'
    for key in 'ЛИН3':
        app.handle_key(key)
    assert app.driver.sent == [], 'в АВТОНОМНО знаки уходят по ПЕРЕДАЧА'
    app.handle_key(KEY_SEND)                  # F10 — ПЕРЕДАЧА
    assert app.driver.sent == ['lin3\r']
    assert app.session.buffer == []


def test_line_end_configurable():
    """Символ конца строки берётся из настройки `[line] eol`."""
    from mc7401.config import parse_eol
    assert parse_eol('ETX') == 0o003
    assert parse_eol('LF') == 0o012
    assert parse_eol('CR') == 0o015
    assert parse_eol('0o012') == 0o012
    assert parse_eol('мусор', 0o003) == 0o003


def test_send_uses_configured_eol(tmp_path):
    """`[line] eol = LF` — строка заканчивается переводом строки."""
    p = tmp_path / 'mc7401.conf'
    p.write_text('[line]\neol = LF\n', encoding='utf-8')
    a = TerminalApp(FakeDriver(), config=Config.load(p))
    assert a.eol == 0o012
    for key in 'СЕТ':
        a.handle_key(key)
    a.handle_key('\r')
    assert a.driver.sent[-1] == '\n'


# ── кадр после очистки экрана ───────────────────────────────────────────────

def test_remembered_frame_shown_after_clear(app):
    """`graf.pas` чистит экран в конце — показывается кадр оператора."""
    from mc7401.framebuffer import Framebuffer
    app.emu.fb.point(10, 10)
    app._remember_frame()
    app.emu.fb.clear()
    assert app.emu.fb.count_set() == 0
    assert app._visible_fb().count_set() == 1


def test_current_frame_wins_when_not_empty(app):
    """Пока есть точки на экране, показывается текущий кадр."""
    app._best_fb = app.emu.fb.copy()
    app._best_n = 1
    app.emu.fb.point(20, 20)
    assert app._visible_fb() is app.emu.fb


# ── графика протоколом терминала ────────────────────────────────────────────

def test_kitty_detect_by_environment():
    from mc7401.backends.kitty import detect
    assert detect({'KITTY_WINDOW_ID': '1'})
    assert detect({'TERM': 'xterm-kitty'})
    assert detect({'TERM_PROGRAM': 'WezTerm'})
    assert not detect({'TERM': 'xterm-256color'})


def test_kitty_response_parsed():
    """Ответ на запрос размера клетки разбирается верно."""
    from mc7401.backends.kitty import KittyGraphics, parse_response
    reply = '\x1b_Gi=31;OK,c=10,r=20,a=T,t=d,f=24;AAAA\x1b\\'
    fields = parse_response(reply)
    assert fields['c'] == '10' and fields['r'] == '20'
    g = KittyGraphics()
    assert not g.active
    assert g.apply_response(reply)
    assert g.active and g.cell == (10, 20)


def test_kitty_geometry():
    """Кадр 560×400 занимает столько знакомест, каков размер клетки."""
    from mc7401.backends.kitty import KittyGraphics
    g = KittyGraphics()
    g.cell = (8, 16)
    assert g.size_for(560, 400) == (70, 25)
    g.cell = (10, 20)
    assert g.size_for(560, 400) == (56, 20)


def test_kitty_payload_is_chunked():
    """Данные режутся на куски; ключи — в первом, `m` — у всех.

    По спецификации kitty: полный набор ключей несёт первый кусок,
    куски, кроме последнего, помечены `m=1`, последний — `m=0`.
    Раньше ключи клались в последний кусок без `m`, и изображения
    больше 4 КБ никогда не отображались.
    """
    from mc7401.backends.kitty import MAX_CHUNK, _apc_stream
    png = b'\x89PNG' + bytes(20000)
    out = _apc_stream('a=T,f=100,t=d,i=1', png)
    assert out.count('\x1b_G') > 1
    assert out.endswith('\x1b\\')
    bodies = out.split('\x1b_G')[1:]
    assert bodies[0].startswith('a=T,f=100,t=d,i=1,m=1;')
    for body in bodies[1:-1]:
        assert body.startswith('m=1;')
    assert bodies[-1].startswith('m=0;')
    for body in bodies:
        payload = body.split(';', 1)[1].split('\x1b\\')[0]
        assert len(payload) <= MAX_CHUNK


def test_graphics_disabled_by_config():
    """`[display] graphics = blocks` — протокол не включается."""
    a = TerminalApp(FakeDriver(), config=Config.default())
    a.graphics = 'blocks'
    assert not a._enable_gfx()
    assert a.gfx is None


def test_paint_gfx_reports_height():
    """Графика занимает столько рядов, сколько нужно кадру."""
    from mc7401.backends.kitty import KittyGraphics
    buf = io.StringIO()
    g = KittyGraphics(buf)
    g.cell = (8, 16)
    g.active = True
    a = TerminalApp(FakeDriver(), config=Config.default())
    a.gfx = g
    a.emu.fb.point(100, 100)
    height = a._paint_gfx(0, 80, 30)
    assert height > 0
    assert buf.getvalue().count('\x1b_G') >= 1

# ── раскладка как на 15ИЭ ───────────────────────────────────────────────────

def test_function_keys_parsed():
    """Клавиши F5…F10 разбираются как на 15ИЭ (клавиатура 15ВВВ-97-006)."""
    from mc7401.term import (KEY_CLEAR, KEY_CMDSET, KEY_ECHO, KEY_MODE,
                             KEY_SEND, _arrow_name)
    assert _arrow_name('15~') == KEY_CLEAR      # F5
    assert _arrow_name('17~') == KEY_ECHO       # F6
    assert _arrow_name('19~') == KEY_CMDSET     # F8
    assert _arrow_name('20~') == KEY_MODE       # F9
    assert _arrow_name('21~') == KEY_SEND       # F10
    assert _arrow_name('5~') == 'KEY_PAGEUP'
    assert _arrow_name('6~') == 'KEY_PAGEDOWN'


def test_letters_are_free_for_text(app):
    """Буквы служебных команд уходят на ЭВМ, а не перехватываются.

    Это и был дефект: `x`, `e`, `z`, `L`, `S` были заняты под команды,
    и текст набрать было нельзя.
    """
    for key in 'exzLSq':
        app.handle_key(key)
    assert app.input_line == 'exzLSq'
    assert [s.encode('latin-1') for s in app.driver.sent] == [
        b'e', b'x', b'z', b'L', b'S', b'q']


def test_function_keys_do_not_type(app):
    """Служебные клавиши терминала текстом не считаются."""
    from mc7401.term import KEY_CMDSET, KEY_MODE
    app.handle_key(KEY_MODE)
    app.handle_key(KEY_CMDSET)
    assert app.input_line == ''


def test_emulator_keys_need_ctrl(app):
    """Масштаб, цвет, сетка — только с `Ctrl`, иначе это текст."""
    from mc7401.tui import TerminalApp
    app.emu.fb.point(100, 100)
    app.handle_key('m')                     # без Ctrl — это знак
    assert app.input_line == 'm'
    assert app.renderer.mode == 'color'
    app.ctrl = True
    app.handle_key('m')                     # с Ctrl — действие
    assert app.renderer.mode == 'mono'
    assert app.input_line == 'm'            # текст не изменился


def test_clear_keeps_screen_in_history(app):
    """`ОЧИСТКА` (F5) сохраняет прежний экран, как на 15ИЭ."""
    from mc7401.term import KEY_CLEAR
    app.emu.fb.point(50, 50)
    app._remember_frame()
    assert app._history == []
    app.handle_key(KEY_CLEAR)
    assert len(app._history) == 1
    assert app._history[0].count_set() == 1


def test_panel_shows_function_keys(app):
    """Подвал называет функциональные клавиши, как на 15ИЭ."""
    panel = app._service_panel(80)
    text = '\n'.join(panel)
    for key in ('F5', 'F6', 'F8', 'F9', 'F10'):
        assert key in text, key


def test_frame_fitted_into_window():
    """Кадр вписывается в окно целиком, а не показывается углом."""
    from pathlib import Path
    stream = Path(__file__).resolve().parent.parent / 'golden' / 'streams'
    path = stream / 'ГРА.bin'
    if not path.exists():
        pytest.skip('эталонный поток не собран')
    a = TerminalApp(FakeDriver(), config=Config.default())
    a.feed(path.read_bytes())
    fb = a._visible_fb()
    assert fb is not None and fb.count_set() > 0
    a._body_lines(76, 16)
    vp = a.renderer.vp
    # После вписывания виден весь кадр (или он шире окна по горизонтали).
    assert fb.width <= 76 * vp.scale
    assert vp.x == 0


# ── шрифт ───────────────────────────────────────────────────────────────────

def test_glyph_offsets_are_one_to_one():
    """Смещения знака не схлопываются: разные столбцы → разные X.

    Был дефект: смещение по X считалось как `u - size * 7 / 2`, при
    нечётной ширине получались дробные координаты, округление
    складывало столбцы попарно, и буквы выходили зеркальными.
    """
    from mc7401 import chars
    # Заведомо заполненный знакомест: все 7×7 точек.
    full = ((1,) * 7,) * 7
    for size in (1, 2, 3):
        pts = chars.text_offsets(full, 0, size)
        xs = sorted({dx for dx, _ in pts})
        # По X знак занимает 7 колонок подряд; по Y поднимается от
        # базовой линии на `GLYPH_H - 1` строк (нижняя строка знакоместа
        # под базовой линией пустая).
        assert xs == sorted(u * size for u in range(7)), (size, xs)
        span = chars.GLYPH_H - 1
        assert len({dy for _, dy in pts}) == span, (size, sorted(
            {dy for _, dy in pts}))
        assert max(dy for _, dy in pts) == span * size


def test_glyph_rendered_upright():
    """Буква рисуется справа от маркера, без зеркального переворота."""
    from mc7401.constants import VK
    from mc7401.emulator import TerminalEmulator
    from mc7401 import chars
    emu = TerminalEmulator()
    emu.execute(emu.parser.parse(b'J1' + bytes((VK,))))
    emu.state.x, emu.state.y = 100, 100
    # Размер и направление приходят кодами символов: 0x01 и 0x00.
    rec = b'9\x01' + bytes((VK,)) + b';\x00A' + bytes((VK,))
    emu.execute(emu.parser.parse(rec))
    mat = chars.glyph_matrix(ord('A'), False)
    for v, row in enumerate(mat):
        for u, bit in enumerate(row):
            if bit:
                assert emu.fb.get(100 + u, 100 + 7 - v), (u, v)
    # Второй знак сдвинут на знакомест + 1 колонка.
    step = int(chars.text_advance(0, 1)[0])
    emu.execute(emu.parser.parse(b'9\x01' + bytes((VK,))
                                  + b';\x00A' + bytes((VK,))))
    assert emu.fb.get(100 + step + 3, 107), 'второй знак не сдвинут'


def test_zag_header_readable():
    """ЗАГ: заголовок рисуется глифами (буквы не зеркальные)."""
    from pathlib import Path
    path = (Path(__file__).resolve().parent.parent / 'golden' / 'streams'
            / 'ЗАГ.bin')
    if not path.exists():
        pytest.skip('эталонный поток не собран')
    a = TerminalApp(FakeDriver(), config=Config.default())
    a.feed(path.read_bytes())
    fb = a._visible_fb()
    assert fb is not None
    bb = fb.bbox()
    assert bb is not None
    x0, y0, x1, y1 = bb
    # Строки глифов — горизонтальные полосы из нескольких точек.
    rows = [y for y in range(y0, y1 + 1)
            if sum(1 for x in range(x0, x1 + 1) if fb.get(x, y)) > 3]
    assert len(rows) > 10, f'в заголовке {len(rows)} строк глифов'


def test_probe_enables_graphics_with_reply(monkeypatch):
    """Ответ терминала включает протокол графики."""
    import io as _io
    import select as _select

    class Fake:
        name = 'фейк'
        greeting = b''

        def poll(self, t):
            return b''

        def send_keys(self, t):
            pass

        def close(self):
            pass

    a = TerminalApp(Fake(), config=Config.default())
    a._enable_gfx()
    a.gfx.out = _io.StringIO()
    reply = b'\x1b_Gi=1;OK,c=8,r=16,a=T;AAAA\x1b\\'
    chunks = [reply[:20], reply[20:]]

    class _Stdin:
        def fileno(self):
            return 0

    monkeypatch.setattr(sys, 'stdin', _Stdin())
    monkeypatch.setattr(_select, 'select',
                        lambda r, w_, x, t: (r, [], []))
    monkeypatch.setattr('os.read',
                        lambda fd, n: chunks.pop(0) if chunks else b'')
    assert a._probe_gfx(timeout=1.0)
    assert a.gfx.cell == (8, 16)


def test_probe_failure_reports_fallback(monkeypatch):
    """Нет ответа терминала — псевдографика и явное сообщение."""
    import select as _select

    class Fake:
        name = 'фейк'
        greeting = b''

        def poll(self, t):
            return b''

        def send_keys(self, t):
            pass

        def close(self):
            pass

    a = TerminalApp(Fake(), config=Config.default())
    a._enable_gfx()
    monkeypatch.setattr(_select, 'select', lambda r, w_, x, t: ([], [], []))
    assert not a._probe_gfx(timeout=0.05)
    assert a.gfx is None
    assert 'псевдографикой' in a.message
