"""
tests/test_regression.py — регрессионные проверки исправлений 2026-10-10.

Каждый тест соответствует конкретному дефекту, найденному при разборе
сценариев `tools/gen_golden.py` (см. JOURNAL.md, раздел «Разбор падений
сценариев»).  Задача набора — не перекрыть всё, а не дать этим правкам
снова развалиться.

Поток команд: записи разделены `ВК` (0o125 = 85 = 'U'), координаты
`X` вправо / `Y` вверх, начало в левом нижнем углу, экран 560×400.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mc7401 import Framebuffer, TerminalEmulator                     # noqa: E402
from mc7401.chars import glyph_matrix                               # noqa: E402
from mc7401.constants import END, S0, VK                          # noqa: E402
from mc7401.reader import PAD_CMD                                     # noqa: E402
from mc7401.link import (                                           # noqa: E402
    IacStripper, LinkError, busy_reply, parse_port_spec, spec_hint, split_endpoint,
)
from mc7401.parser import CommandParser, Op                          # noqa: E402
from mc7401.reader import CommandStreamReader, EventKind            # noqa: E402

GOLDEN = ROOT / 'golden' / 'streams'


def stream(*records: bytes | str) -> bytes:
    """Собрать поток: каждой записи дописать `ВК`.

    `str` кодируется в КОИ-8 — так текст идёт в линию от программы.
    """
    out = bytearray()
    for r in records:
        out += (r.encode('koi8-r') if isinstance(r, str) else r) + bytes((VK,))
    return bytes(out)


def run(data: bytes) -> TerminalEmulator:
    emu = TerminalEmulator()
    emu.run(data)
    return emu


def parse_all(data: bytes):
    """Разобрать поток и вернуть список команд."""
    reader = CommandStreamReader()
    reader.feed(data)
    reader.close()
    parser = CommandParser()
    return [parser.parse(ev.data) for ev in reader.pop()
            if ev.kind is EventKind.COMMAND]


# ── дефект 27: маркеры структуры не должны считаться командами ──────────────

def test_pad_and_vuk_block_are_not_commands():
    """Заполнитель `0o32 ВК` и маркер блока `'/' ВК` — не операции.

    Раньше они попадали в разбор как команды и учитывались в счётчике
    «нераспознано»; теперь у них собственные события.
    """
    data = PAD_CMD + b'/' + bytes((VK,)) + b'z' + b'0' * 19 + b'J1' + bytes((VK,))
    events = list(_events(data))
    kinds = [ev.kind for ev in events]
    assert EventKind.PAD in kinds
    assert EventKind.VUK_BLOCK in kinds
    # Командой является только `J1`
    commands = [ev.data for ev in events if ev.kind is EventKind.COMMAND]
    assert commands == [b'J1' + bytes((VK,))]


def _events(data: bytes):
    reader = CommandStreamReader()
    reader.feed(data)
    reader.close()
    return reader.pop()


def test_unknown_count_is_zero_for_plain_commands():
    """Обычные графические команды разбираются без потерь."""
    cmds = parse_all(stream('J1', 'S', '4 10 20', '7 1 30 40'))
    assert [c.op for c in cmds] == [Op.GRAPHICS_MODE, Op.CLEAR,
                                    Op.MOVE_ABS, Op.LINE_ABS]


def test_vuk_payload_tail_marker_is_split_out():
    """`АР2 'У  '` в хвосте блока ВУК не должен попадать в данные.

    Маркер входа в кадр оставался в полезной нагрузке блока и портил
    последующий разбор.
    """
    data = (b'/' + bytes((VK,)) + b'z' + b'0' * 19 + b'J1' + bytes((VK,))
            + b'T#  ' + b'TF>' + bytes((VK,)))
    events = list(_events(data))
    assert any(ev.kind is EventKind.FRAME_BEGIN for ev in events)
    assert any(ev.kind is EventKind.FRAME_END for ev in events)
    # В полезной нагрузке блока маркер входа в кадр остаться не должен
    for ev in events:
        if ev.kind is EventKind.COMMAND and ev.meta.get('payload'):
            assert b'T#  ' not in ev.data


# ── дефект 24: параметры команды `9` приходят кодами символов ───────────────

def test_text_size_argument_is_a_char_code():
    """`9` и `;O` несут коды символов (`СНР(МСШ)`), а не числа.

    Разбор как `int()` падал, и текст выводился нулевым размером.
    """
    data = stream(b'4 100 200', b'9\x01', ';\x00 ПРИВЕТ')
    emu = TerminalEmulator()
    emu.run(data)
    assert emu.stats['chars'] == 6


def test_text_size_is_remembered_between_records():
    """Размер шрифта берётся из предыдущей записи `9 S`."""
    parser = CommandParser()
    parser.parse(b'9\x02' + bytes((VK,)))          # запись размера
    cmd = parser.parse(';\x01 АБВ'.encode('koi8-r') + bytes((VK,)))  # данные
    assert cmd.op is Op.TEXT
    assert cmd.args[0] == b'\x02'
    assert cmd.payload.decode('koi8-r') == 'АБВ'


def test_text_command_draws_glyphs():
    """Текст должен оставлять точки на кадре (регрессия «0 точек»)."""
    emu = run(stream('4 50 300', 'J1', b'9\x01', ';\x00 ИТМ'))
    assert emu.fb.count_set() > 0


# ── дефект 25: спецкоды шрифта доходят до эмулятора ─────────────────────────

def test_s0_switches_to_koi8r():
    """`S0` (0o160) включает русский набор КОИ-8Р.

    Наборов у МС7401 два: Н2 (КОИ-7, VT-52) и КОИ-8Р.  `S0` — русский,
    `S1` — возврат к международному Н2.  Так их использует `ТЕКФАЙ`
    в `graf.pas`: перед русскими знаками посылается `S0`.
    """
    emu = run(stream('J1', '4 10 300', b'9\x01', ';\x00 ' + chr(S0) + 'АБВ'))
    assert emu.state.font == 'koi8r'


def test_s1_returns_to_default_set():
    """`S1` (0o161) возвращает набор по умолчанию — Н2 (КОИ-7)."""
    from mc7401.constants import S0, S1
    emu = run(stream('J1', '4 10 300', b'9\x01',
                     ';\x00 ' + chr(S0) + chr(S1) + 'АБВ'))
    assert emu.state.font == 'n2'


def test_default_charset_set_is_n2():
    """По умолчанию включён Н2 (КОИ-7) — набор команд VT-52."""
    from mc7401.chars import DEFAULT_SET, SETS
    assert DEFAULT_SET == 'n2'
    assert SETS == ('n2', 'koi8r')
    assert TerminalEmulator().state.font == 'n2'


def test_default_charset_set_is_n2():
    """По умолчанию включён Н2 (КОИ-7) — набор команд VT-52."""
    from mc7401.chars import DEFAULT_SET, SETS
    assert DEFAULT_SET == 'n2'
    assert SETS == ('n2', 'koi8r')
    assert TerminalEmulator().state.font == 'n2'


def test_russian_glyph_differs_from_latin():
    """Один и тот же код в русском и латинском шрифте даёт разный знак."""
    ru = glyph_matrix(0xE9, True)
    lat = glyph_matrix(0xE9, False)
    assert any(any(r) for r in ru)
    assert ru != lat


def test_s0_is_not_drawn_as_character():
    """`S0` — управляющий код, он не должен попадать в строку."""
    emu = run(stream('J1', '4 10 300', b'9\x01',
                     ';\x00 ' + chr(S0) + 'АБВ'))
    assert emu.stats['chars'] == 3


# ── дефект 21/26: разбор потока из реальных программ ────────────────────────

def render(name: str) -> Framebuffer:
    """Отрисовать эталонный поток и вернуть кадр оператора.

    Программа (`graf.pas`) очищает экран командой `S` в начале каждой
    картины и оставляет его пустым после выхода.  Поэтому берётся
    состояние на границе блока ВУК с наибольшим числом точек — именно
    его видел оператор.  Так же работает `tools/gen_golden.py`.
    """
    path = GOLDEN / f'{name}.bin'
    if not path.exists():                     # поток не сгенерирован
        pytest.skip(f'{path.name} отсутствует')
    emu = TerminalEmulator()
    best, best_n = emu.fb.copy(), 0
    reader = CommandStreamReader()
    reader.feed(path.read_bytes())
    reader.close()
    for ev in reader.pop():
        if ev.kind is EventKind.VUK_BLOCK:
            n = emu.fb.count_set()
            if n > best_n:
                best, best_n = emu.fb.copy(), n
        emu.handle_event(ev)
    return best if best_n else emu.fb


@pytest.mark.parametrize('name', ['СЕТ', 'СОЛ', 'КОТ', 'ЦЕП', 'ФУН',
                                  'ЛИН', 'ОКР', 'ГРА', 'ШАР', 'ЛЕД',
                                  'УЗО', 'РОЗ', 'ТИП', 'СИМ'])
def test_golden_streams_render(name: str):
    """Эталонные потоки `graf.pas` дают непустой кадр."""
    assert render(name).count_set() > 0, f'{name}: пустой кадр'


def test_grid_fills_most_of_screen():
    """`СЕТ` рисует сетку — кадр должен быть заполнен существенно."""
    assert render('СЕТ').count_set() > 10000


def test_title_screen_renders_text():
    """`ЗАГ` — титульный экран: должны быть и рамки, и текст."""
    assert render('ЗАГ').count_set() > 100


# ── геометрия и кадр ───────────────────────────────────────────────────────

def test_framebuffer_dimensions():
    fb = Framebuffer()
    assert (fb.width, fb.height) == (560, 400)


def test_origin_is_bottom_left():
    """Точка (0, 0) — левый нижний угол."""
    fb = Framebuffer()
    fb.point(0, 0)
    rows = [bytes(r) for r in fb.to_rows_bottom_up()]
    assert rows[0][0] & 0x80          # старший бит первого байта
    assert not rows[-1][0] & 0x80


def test_clear_empties_frame():
    fb = Framebuffer()
    fb.point(10, 10)
    fb.clear()
    assert fb.count_set() == 0


def test_copy_is_independent():
    fb = Framebuffer()
    fb.point(5, 5)
    c = fb.copy()
    c.point(6, 6)
    assert not fb.get(6, 6)
    assert c.get(5, 5)


# ── связь с БЭСМ-6 по ДКС ─────────────────────────────────────────────────

def test_parse_port_spec_variants():
    """Спецификация портов: одиночный, список, диапазон, смешанный."""
    assert parse_port_spec('4202') == [4202]
    assert parse_port_spec('4202,4210') == [4202, 4210]
    assert parse_port_spec('4202-4204') == [4202, 4203, 4204]
    assert parse_port_spec('4202-4203,4210') == [4202, 4203, 4210]


def test_parse_port_spec_deduplicates():
    """Повторы отбрасываются, порядок сохраняется."""
    assert parse_port_spec('4210,4202,4210') == [4210, 4202]


def test_parse_port_spec_rejects_bad_input():
    with pytest.raises(LinkError):
        parse_port_spec('')
    with pytest.raises(LinkError):
        parse_port_spec('0')
    with pytest.raises(LinkError):
        parse_port_spec('4205-4202')       # перевёрнутый диапазон


def test_spec_hint_compacts_ranges():
    assert spec_hint([4202]) == '4202'
    assert spec_hint([4202, 4203, 4204]) == '4202-4204'
    assert spec_hint([4202, 4205, 4206]) == '4202,4205-4206'


def test_split_endpoint_uses_default_ports():
    """Без указания портов берётся диапазон ДКС 4202-4223."""
    host, ports = split_endpoint('127.0.0.1')
    assert host == '127.0.0.1'
    assert ports[:1] == [4202] and ports[-1] == 4223


def test_split_endpoint_with_explicit_port():
    host, ports = split_endpoint('localhost:4210')
    assert host == 'localhost'
    assert ports == [4210]


def test_busy_reply_detects_refusal():
    """SIMH отвечает отказом строкой — такой порт пропускаем."""
    assert busy_reply(b'DKS: line busy')
    assert busy_reply(b'CONNECTION NOT AVAILABLE')
    assert not busy_reply(b'\x01\x02\x03')


def test_iac_stripper_removes_negotiation():
    """telnet-переговоры не должны попасть в поток команд."""
    s = IacStripper()
    data = s.feed(b'\xff\xfb\x18\xff\xfd\x03OK')     # WILL/DO + данные
    assert data == b'OK'


def test_iac_stripper_subnegotiation():
    """`IAC SB … IAC SE` отбрасывается целиком."""
    s = IacStripper()
    assert s.feed(b'\xff\xfa\x01\xff\xf0OK') == b'OK'


def test_iac_stripper_keeps_plain_data():
    s = IacStripper()
    assert s.feed(b'J1\x554 10 20') == b'J1\x554 10 20'


def test_iac_stripper_state_survives_split():
    """Последовательность IAC может прийти двумя чтениями."""
    s = IacStripper()
    first = s.feed(b'\xff')
    assert first == b''
    assert s.feed(b'\xfb\x18OK') == b'OK'

# ── дефекты 30-33: текст, размеры, заглушки ───────────────────────────────

def test_text_size_command_sets_size():
    """Запись `9 : N` (`':0 '`) задаёт размер для следующих строк.

    В `СТРОКИ` размер приходит отдельной записью `9 : N`; раньше она
    разбиралась как неизвестная, и знаки выходили вырожденными.
    """
    parser = CommandParser()
    parser.parse(b'91' + bytes((VK,)))          # запись размера `9 1`
    cmd = parser.parse(b':0 ' + bytes((VK,)))    # установка размера 0
    assert cmd.op is Op.SET_TEXT_SIZE
    assert cmd.args[0] == b'\x00'
    assert parser.text_size == b'\x00'


def test_end_byte_terminates_text():
    """`ЕND` (0xFF) обрывает строку текста, а не рисуется как знак.

    Программа его не выводит (цикл `WНILЕ INРUТ↑≠ЕND`), но в отличие от
    прочих служебных кодов он не должен попадать на экран.
    """
    data = (stream('J1', '4 10 300', b'9\x01')
            + b';\x00 ' + 'АБ'.encode('koi8-r') + bytes((END,))
            + 'ВГ'.encode('koi8-r') + bytes((VK,)))
    emu = TerminalEmulator()
    emu.run(data)
    assert emu.stats['chars'] == 2      # только «АБ», «ВГ» отброшено


def test_zero_text_size_still_draws_glyphs():
    """Размер 0 — это «мелкий знак», а не отсутствие знака."""
    data = stream('J1', '4 10 300', b'91', b':0 ', ';\x00 АБВ')
    emu = TerminalEmulator()
    emu.run(data)
    assert emu.stats['chars'] == 3
    assert emu.fb.count_set() > 3          # знаки не выродились в точку


def test_bind_does_not_pollute_stream():
    """`ВIND` привязывает файл к устройству и ничего не пишет.

    Заглушка выводила строку устройства (`'> ≡377'`) в поток команд,
    откуда в текст попадали посторонние символы.
    """
    sys.path.insert(0, str(ROOT / 'tools'))
    import pascal.runtime as rt
    rt._terminal.clear()
    del rt._terminal.wire[:]
    rt.BIND('> ≡377')
    rt.BIND(' ==) ≡377')
    assert bytes(rt._terminal.wire) == b''


# ── кириллица: знаки из ПЗУ знакогенератора ───────────────────────────────

def test_cyrillic_font_covers_alphabet():
    """В таблице есть все буквы русского алфавита, включая «ё»."""
    from mc7401.chars import FONT_7X8_CYR
    for ch in 'АБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдежзийклмнопрстуфхцчшщъыьэюя':
        assert ch in FONT_7X8_CYR, f'нет знака {ch!r}'
    assert 'Ё' in FONT_7X8_CYR and 'ё' in FONT_7X8_CYR


def test_every_cyrillic_letter_maps_to_a_glyph():
    """Каждой букве КОИ-8 0x80…0xFF сопоставлен знак, а не «?»."""
    from mc7401.chars import GLYPHS_RU
    for ch in 'АБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдежзийклмнопрстуфхцчшщъыьэюя':
        code = ch.encode('koi8-r')[0] & 0x7F
        assert code in GLYPHS_RU, f'код {code:#04x} ({ch}) без знака'


def test_cyrillic_glyphs_are_7x8_cells():
    """Глифы ПЗУ занимают знакомест 7×8 и не пусты."""
    from mc7401.chars import FONT_7X8_CYR
    for ch, art in FONT_7X8_CYR.items():
        assert len(art) == 8, f'{ch}: {len(art)} строк'
        for row in art:
            assert len(row) == 7, f'{ch}: строка шириной {len(row)}'
        assert any('#' in row for row in art), f'{ch}: пустой знак'


def test_latin_glyphs_are_padded_to_cell():
    """Латинские знаки 5×7 выровнены в знакомест 7×8."""
    from mc7401.chars import _LAT_MATRIX
    for code, mat in _LAT_MATRIX.items():
        if code == 0x20:                       # пробел — пустой по определению
            continue
        assert len(mat) == 8
        for row in mat:
            assert len(row) == 7


def test_cyrillic_code_mapping_is_seven_bit_koi8():
    """Правило кодов: семибитный код `c` на странице `S0` = КОИ-8 `c|0x80`.

    Подтверждено статьёй («7 бит используется в каждом байте»):
    `п` = 0x70, `и` = 0x69, `т` = 0x74 — при кодах КОИ-8 0xF0/0xE9/0xF4.
    """
    from mc7401.chars import GLYPHS_RU
    for ch, koi8 in (('п', 0xF0), ('и', 0xE9), ('т', 0xF4)):
        assert (koi8 & 0x7F) in GLYPHS_RU


# ── развёртывание и документация ───────────────────────────────────────────

def test_packaging_points_at_real_package():
    """`pyproject.toml` описывает пакет `mc7401`, а не старые модули.

    Секция `[tool.setuptools]` прежде перечисляла плоские `main`,
    `reader`, `backends*` — установка падала бы.
    """
    import tomllib
    root = Path(__file__).resolve().parent.parent
    with open(root / 'pyproject.toml', 'rb') as fh:
        cfg = tomllib.load(fh)
    assert cfg['tool']['setuptools']['packages'] == ['mc7401', 'mc7401.backends']
    assert cfg['project']['scripts']['mc7401'] == 'mc7401.cli:main'


def test_shell_helpers_exist():
    """Сценарии развёртывания и короткие команды на месте."""
    root = Path(__file__).resolve().parent.parent
    for rel in ('install.sh', 'install.ps1', 'tools/mc7401-completion.bash',
                'tools/mc7401.fish', 'tools/check_dks_link.py',
                'docs/DEPLOY.md', 'docs/COMMANDS.md'):
        assert (root / rel).is_file(), f'нет {rel}'


def test_completion_scripts_are_valid():
    """Скрипты автодополнения разбираются оболочкой."""
    import subprocess
    root = Path(__file__).resolve().parent.parent
    for script in ('tools/mc7401-completion.bash',):
        r = subprocess.run(['bash', '-n', str(root / script)],
                           capture_output=True)
        assert r.returncode == 0, r.stderr.decode()


# ── фронтенд: экран, перо, терминал ────────────────────────────────────────

def test_viewport_maps_terminal_cells_to_pixels():
    """Ячейка терминала → пиксель кадра; Y переворачивается."""
    from mc7401.screen import ScreenRenderer, Viewport
    fb = Framebuffer()
    fb.point(100, 50)
    # моно-режим: один знак на знакомест, без escape-последовательностей
    r = ScreenRenderer(fb, mode='mono', vp=Viewport(scale=1, x=0, y=0))
    # Пиксель Y=50 стоит на 399-50=349 сверху; знакомест закрывает два
    # пикселя по вертикали, поэтому строка вывода 349//2 = 174.
    lines = r.render(560, 400).split('\n')
    assert len(lines) == 400
    assert lines[174][100] != ' '


def test_viewport_fit_and_clamp():
    """`fit` вписывает кадр в окно, `clamp` не выпускает за границы."""
    from mc7401.screen import Viewport
    fb = Framebuffer()
    vp = Viewport()
    vp.fit(fb, 80, 24)
    assert vp.scale >= 1
    vp.x, vp.y = -50, -50
    vp.clamp(fb, 80, 24)
    assert vp.x == 0 and vp.y == 0


def test_renderer_produces_two_pixels_per_cell():
    """Один знакомест закрывает два пикселя по вертикали."""
    from mc7401.screen import ScreenRenderer, Viewport
    fb = Framebuffer()
    fb.hline(0, 559, 399)          # верхняя строка кадра
    r = ScreenRenderer(fb, vp=Viewport(scale=1, x=0, y=0))
    body = r.render(60, 4)
    assert body.split('\n')[0].strip()


def test_pen_maps_mouse_and_draws_stroke():
    """Мышь работает как световое перо: нажатие и протяжка рисуют линию."""
    from mc7401.pen import LightPen
    pen = LightPen()
    pen.feed_mouse('\x1b[<0;20;10M', cols=80, rows=24)
    assert not pen.down
    pen.stroke(pen.fb)
    pen.feed_mouse('\x1b[<1;30;10M', cols=80, rows=24)
    assert pen.down
    pen.feed_mouse('\x1b[<32;60;10M', cols=80, rows=24)
    assert pen.down, 'протяжка сохраняет состояние пера'
    assert pen.stroke(pen.fb)
    assert pen.fb.count_set() > 0
    pen.feed_mouse('\x1b[<0;60;10m', cols=80, rows=24)
    assert not pen.down


def test_pen_coordinates_follow_viewport():
    """Смещение окна сдвигает перо согласованно с картинкой."""
    from mc7401.pen import LightPen
    a = LightPen().feed_mouse('\x1b[<0;10;5M', cols=80, rows=24, scale=1, vx=0, vy=0)
    b = LightPen().feed_mouse('\x1b[<0;10;5M', cols=80, rows=24, scale=1, vx=100, vy=50)
    assert b[0] - a[0] == 100


def test_pen_event_encoding_is_provisional():
    """Событие пера кодируется для линии связи (кодирование provisional)."""
    from mc7401.pen import PenEvent
    ev = PenEvent(200, 300, True)
    assert ev.encode() == b'\x1b[1;200;300M'
    assert 'PROVISIONAL' in __import__('mc7401.pen', fromlist=['x']).__doc__


# ── режимы терминала по образцу 15ИЭ-00-013 ────────────────────────────────

def test_host_mode_sends_every_character_immediately():
    """В режиме С ЭВМ каждый знак уходит в линию немедленно."""
    from mc7401.session import Session
    s = Session()
    assert s.mode == 'host'
    assert s.type_line('СЕТ') == 'СЕТ'.encode('koi8-r')
    assert s.pending() == b''


def test_local_mode_buffers_until_send():
    """В режиме АВТОНОМНО набор копится, SEND передаёт накопленное."""
    from mc7401.session import Session
    s = Session()
    s.toggle_mode()
    assert s.mode == 'local'
    assert s.type_line('ЛИН 3 ') == b''          # в линию не уходит
    assert s.pending() == 'ЛИН 3 '.encode('koi8-r')
    out = s.send()
    assert out.endswith(bytes((VK,)))            # SEND добавляет ВК
    assert 'ЛИН 3'.encode('koi8-r') in out
    assert s.pending() == b''                    # буфер очищен


def test_clear_command_goes_to_computer_only_in_host_mode():
    """ОЧИСТКА: в HOST команда `S` уходит на ЭВМ, в LOCAL — локально."""
    from mc7401.session import Session
    s = Session()
    assert s.clear_command() == b'S' + bytes((VK,))
    s.toggle_mode()
    assert s.clear_command() == b''


def test_echo_toggles():
    """Клавиша ЭХО: снята — набираемое не печатается."""
    from mc7401.session import Session
    s = Session()
    assert s.echo is True
    assert s.toggle_echo() is False
    assert 'эхо выкл' in s.panel()
    assert s.toggle_echo() is True


def test_russian_layout_exists_only_in_koi8r():
    """Русская раскладка живёт только в русском наборе — как в 15ИЭ."""
    from mc7401.session import Session
    s = Session()
    assert s.charset_set == 'n2'
    assert s.layout is None
    s.set_charset('koi8r')
    assert s.layout == 'phonetic'
    s.set_charset('n2')
    assert s.layout is None
