"""
test_snapshot.py — инструмент снимка экрана (`tools/snapshot.py`).

Снимок снимается без окна: тот же кадр, та же история и те же служебные
строки, что и в окне, но в файлы.  Проверяется ровно то, ради чего
инструмент нужен: что снимок содержит экран целиком, а разбор находит
именно те дефекты, которые иначе видны только глазами, — пустые ряды
между строками и задвоенные знаки.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / 'tools'):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from mc7401.config import Config                                # noqa: E402
from mc7401.console import CELL_H, ROWS, Console, read_row      # noqa: E402
from mc7401.framebuffer import Framebuffer                      # noqa: E402

import snapshot as S                                           # noqa: E402


def frame_of(win) -> list[str]:
    return S.frame_text(win.shown_frame())


# ── разбор экрана ───────────────────────────────────────────────────────────

def test_gaps_between_rows_are_reported():
    """Пустой ряд между непустыми — это «лишний отступ» в выдаче."""
    rows = ['не пусто', 'тоже', '', 'и снова', 'ещё', '', 'низ']
    gaps = S.find_gaps(rows)
    assert gaps == ['пустой ряд 2 между 1 и 3',
                    'пустой ряд 5 между 4 и 6'], gaps


def test_empty_screen_has_no_gaps():
    """Пустой экран разбор не считает дефектом."""
    assert S.find_gaps([''] * ROWS) == []


def test_doubled_letters_and_words_are_reported():
    """Задвоение знаков и повтор слова ловятся по тексту ряда."""
    doubles = S.find_doubles(['КРАБ', 'КРААБ', 'линия линия', 'обычный'])
    assert any('«РА»' in d or '«АА»' in d for d in doubles), doubles
    assert any('«линия» повторено' in d for d in doubles), doubles
    assert not any('обычный' in d for d in doubles), doubles


def test_analyse_reports_width():
    """Строка длиннее сетки помечается: на МС 7401 такой быть не может."""
    report = S.analyse_screen(['короткая', 'я' * (S.COLS + 3)])
    assert report['too_wide'] and 'знаков' in report['too_wide'][0]
    assert report['empty_rows'] == []
    assert report['lengths'][0] == len('короткая')


# ── консоль с журналом ──────────────────────────────────────────────────────

def test_trace_console_journal_records_every_byte():
    """Каждый байт линии попадает в журнал со своим знакоместом."""
    con = S.TraceConsole(Framebuffer())
    con.write(b'ABC\r\n')
    assert [r['b'] for r in con.log][:3] == [0x41, 0x42, 0x43]
    assert [r['code'] for r in con.log][:3] == ['A', 'B', 'C']
    assert con.log[3]['b'] == 0x0D
    assert con.log[3]['code'] == '', 'у управляющего кода нет знака'


def test_trace_console_spotlights_two_bytes_in_one_cell():
    """Два байта в одном знакоместе — это и есть задвоение на экране."""
    trace = S.TraceConsole(Framebuffer())
    # ЭВМ возвращает каретку и пишет знак заново в то же знакоместо —
    # ровно то, как на экране выглядит задвоение или затирание буквы.
    trace.write(b'A')
    trace.carriage_return()
    trace.write(b'B')
    dup = trace.duplicated()
    assert dup, 'задвоение не найдено'
    assert 'знакомест' in dup[0]


def test_trace_console_tail_reads():
    """Журнал читается строками: байт → что он сделал."""
    trace = S.TraceConsole(Framebuffer())
    trace.write(b'A\x0d')
    lines = trace.tail_text()
    assert any('0x41' in line and 'A' in line for line in lines), lines
    assert any('перевод' in line for line in lines), lines


# ── сборка и запись снимка ──────────────────────────────────────────────────

def test_snapshot_is_taken_without_a_window():
    """Снимок собирается без Tk и без дисплея."""
    win = S.build(Config.default())
    win.console.write('ПРОВЕРКА\r\n'.encode('koi8-r'))
    win._touch_frame()
    fb = S.screen_framebuffer(win)
    assert fb.height > 400, 'в кадр снимка не попали служебные полосы'
    rows = frame_of(win)
    assert 'ПРОВЕРКА' in rows[0], rows[:3]


def test_take_writes_everything_needed_for_analysis(tmp_path=None):
    """Снимок пишет всё, чем разбор потом пользуется."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        win = S.build(Config.default())
        win.console.write('СТРОКА 01\r\nСТРОКА 02\r\n'.encode('koi8-r'))
        win._touch_frame()
        out = S.take(win, Path(tmp) / 'снимок-1', b'\x41\x42', tag='проверка')
        for name in ('экран.png', 'экран.txt', 'кадр.bin', 'линия.bin',
                     'байты.txt', 'снимок.json'):
            assert (out / name).exists(), f'нет файла {name}'
        meta = json.loads((out / 'снимок.json').read_text(encoding='utf-8'))
        assert meta['tag'] == 'проверка'
        assert meta['report']['rows'][0].startswith('СТРОКА 01')
        assert meta['history']['size'] == 0
        assert (out / 'линия.bin').read_bytes() == b'\x41\x42'
        text = (out / 'экран.txt').read_text(encoding='utf-8')
        assert '--- разбор ---' in text
        assert '--- байты линии' in text


def test_frame_text_ignores_the_service_rows():
    """Нумерация рядов кадра не заезжает на подвал и строку состояния."""
    win = S.build(Config.default())
    win._touch_frame()
    rows = frame_of(win)
    assert len(rows) == ROWS
    assert all('F2' not in r for r in rows), 'подвал попал в кадр'


def test_ruler_matches_the_grid():
    """Линейка разбора совпадает с сеткой знакомест."""
    lines = S.ruler().split('\n')
    assert len(lines[0]) == S.COLS
    assert len(lines[1]) == S.COLS
    assert lines[0][0] == '0' and lines[1][0] == '0'
    last_ten = (S.COLS // 10) * 10 - 10
    assert lines[0][last_ten] == str(last_ten // 10 % 10)
    assert lines[0][last_ten + 1] == ' ', 'цифры десятков лишние'
    assert lines[1][last_ten + 1] == '1', 'единицы не на месте'