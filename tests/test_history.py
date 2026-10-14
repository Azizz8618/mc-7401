"""
test_history.py — история выданных на экран строк, как в терминале 15ИЭ.

Проверяется `mc7401.history.History` и её связь с окном: строки, сошедшие
с кадра при автоскролле, помнятся и доступны колесом мыши и клавишами
`ЭКРАН↑`/`ЭКРАН↓`; глубина и число шагов правки задаются конфигурацией.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mc7401.config import Config                                  # noqa: E402
from mc7401.console import (CELL_H, Console, read_row, ROWS)     # noqa: E402
from mc7401.framebuffer import Framebuffer                       # noqa: E402
from mc7401.history import DEFAULT_LIMIT, History                # noqa: E402

sys.path.insert(0, str(ROOT / 'tests'))
from test_frontend import make_window as _make_window             # noqa: E402


def fill(con: Console, count: int, first: int = 1) -> None:
    """Вывести `count` строк вида «СТРОКА NN»."""
    for i in range(first, first + count):
        con.write(f' stroka {i:02d}'.encode('latin-1') + b'\r\n')


def frame_with_history(count: int = 80, **kw):
    """Кадр с консолью, историей и заполненными строками."""
    fb = Framebuffer()
    con = Console(fb)
    hist = History(**kw)
    con.history = hist
    fill(con, count)
    return fb, con, hist


# ── наполнение ──────────────────────────────────────────────────────────────

def test_history_default_is_1000_lines():
    """По умолчанию помнится тысяча строк — как в эталоне 15ИЭ."""
    assert DEFAULT_LIMIT == 1000
    assert Config.default().history == 1000
    assert History().limit == 1000


def test_lines_that_scrolled_off_are_kept():
    """Всё, что сошло с кадра при автоскролле, помнится.

    Первый ряд кадра заполняется на 40-й строке, и автоскролл уводит
    первую строку уже на 41-й: в историю кладётся на строку больше, чем
    `выведено - 40`.
    """
    fb, con, hist = frame_with_history(80)
    assert hist.size == 80 - ROWS + 1, f'в истории {hist.size} строк'
    assert hist.texts()[-1] == ' СТРОКА 41'
    assert hist.texts()[0] == ' СТРОКА 01'


def test_blank_lines_are_not_stored():
    """Пустые строки в историю не пишутся — прокрутка не уходит в пустоту."""
    fb = Framebuffer()
    con = Console(fb)
    hist = History()
    con.history = hist
    for _ in range(50):
        con.write(b'\r\n')                 # только переводы строк
    assert hist.size == 0, 'пустые строки попали в историю'


def test_history_is_bounded_and_counts_dropped():
    """Глубина ограничена; вытесненное считается, а не теряется молча."""
    fb = Framebuffer()
    con = Console(fb)
    hist = History(limit=5)
    con.history = hist
    fill(con, 60)
    assert hist.size == 5
    assert hist.pushed == 60 - ROWS + 1
    assert hist.dropped == hist.pushed - 5
    # Хранятся последние пять из ушедших: 17…21.
    assert hist.texts()[-1] == ' СТРОКА 21', hist.texts()[-1]
    assert hist.texts()[0] == ' СТРОКА 17', hist.texts()[0]


def test_history_off_when_limit_zero():
    """`[display] history = 0` — история не ведётся."""
    fb, con, hist = frame_with_history(60, limit=0)
    assert hist.size == 0
    assert hist.texts() == []


# ── окно просмотра ───────────────────────────────────────────────────────────

def test_scroll_back_and_forward():
    """Листание туда-обратно по строке за раз."""
    fb, con, hist = frame_with_history(80)
    assert hist.position == 0
    hist.scroll(-1)
    assert hist.position == 1
    hist.scroll(-3)
    assert hist.position == 4
    hist.scroll(2)
    assert hist.position == 2


def test_scroll_stops_at_oldest_and_returns_to_frame():
    """Дальше самой старой строки не ходим, и к кадру всегда вернуться."""
    fb, con, hist = frame_with_history(80)
    hist.scroll(-10_000)
    assert hist.position == hist.size, 'прокрутка ушла за первую строку'
    hist.scroll(10_000)
    assert hist.position == 0


def test_scroll_without_history_does_nothing():
    """Пустой истории листать нечего."""
    hist = History()
    assert hist.scroll(-1) == 0
    assert hist.scroll(1) == 0


def test_view_shows_history_above_the_frame():
    """В окне листания: сверху строки истории, под ними верх кадра."""
    fb, con, hist = frame_with_history(80)
    hist.scroll(-3)
    view = hist.view(fb)
    assert view is not None
    rows = [read_row(view, (ROWS - 1 - n) * CELL_H).rstrip() for n in range(4)]
    assert rows[0] == ' СТРОКА 39', rows[0]
    assert rows[1] == ' СТРОКА 40', rows[1]
    assert rows[2] == ' СТРОКА 41', rows[2]
    # Под историей — кадр: его верхняя строка (первая, что ушла).
    assert rows[3] == ' СТРОКА 42', rows[3]
    assert rows[3] == read_row(fb, (ROWS - 1) * CELL_H).rstrip()


def test_view_fills_the_whole_window_no_seam():
    """Окно листания заполнено целиком: стык истории и кадра без разрыва.

    `top_bits` берёт **точечные** ряды, а `rows`/`from_hist` — **текстовые**.
    Без умножения на `CELL_H` кадр обрезался в `CELL_H` раз: при листании
    на страницу в окно попадало 12 % кадра, остальное заливалось нулями —
    и «смазанная склейка» была видна как пустота под последней строкой
    истории.  Проверка идёт по всем рядам окна: ни пустых, ни разрывов.

    Масштаб знака приводится к умолчанию: `CELL_H`/`ROWS` — глобальные
    величины `console`, и при чужом масштабе проверка считала бы ряды по
    другой сетке (падало бы в зависимости от порядка запуска тестов).
    """
    import mc7401.console as console_mod
    saved = console_mod.GLYPH_SCALE
    console_mod.set_glyph_scale(1)
    try:
        fb, con, hist = frame_with_history(4 * ROWS + 40)
        for back in (1, 2, 3, 5, 20, ROWS - 1, ROWS, ROWS + 1, 2 * ROWS):
            hist.back = back
            view = hist.view(fb)
            assert view is not None, back
            nums = []
            for n in range(ROWS):
                text = read_row(view, (ROWS - 1 - n) * CELL_H).strip()
                nums.append(int(text.split()[1])
                            if text.startswith('СТРОКА') else None)
            # Рядов ровно столько, сколько в кадре: окно не обрезано.
            assert None not in nums, f'back={back}: пустой ряд в окне'
            # Склейка плавная: номера идут подряд, без повторов и разрывов.
            gaps = [(i, a, b) for i, (a, b) in enumerate(zip(nums, nums[1:]))
                    if b != a + 1]
            assert not gaps, f'back={back}: разрыв в склейке {gaps}'
    finally:
        console_mod.set_glyph_scale(saved)


def test_view_top_takes_whole_text_rows():
    """Из кадра в окно берётся ровно столько точек, сколько нужно рядов."""
    fb, con, hist = frame_with_history(200)
    stride = fb.width // 8
    for back in (1, 5, 20, ROWS - 1):
        hist.back = back
        n = len(hist.lines)
        start = n + ROWS - back - ROWS
        from_hist = max(0, min(ROWS, n - start))
        want_rows = ROWS - from_hist
        got = len(fb.top_bits(want_rows * CELL_H))
        assert got == want_rows * CELL_H * stride, (
            f'back={back}: взято {got} байт вместо '
            f'{want_rows * CELL_H * stride}')


def test_view_is_none_at_the_frame_or_without_history():
    """У кадра окно листания не собирается."""
    fb, con, hist = frame_with_history(80)
    assert hist.view(fb) is None, 'окно собрано без листания'
    fb2 = Framebuffer()
    con2 = Console(fb2)
    con2.history = History()
    assert con2.history.view(fb2) is None


def test_view_goes_past_the_frame_entirely():
    """Отойти можно дальше окна: кадр уходит вниз, видны строки истории.

    Прокрутка идёт по общему потоку «история, затем кадр», как в 15ИЭ:
    отойдя на всё окно, кадр из него исчезает.
    """
    fb, con, hist = frame_with_history(80)
    hist.scroll(-hist.size)
    view = hist.view(fb)
    assert view is not None
    rows = [read_row(view, (ROWS - 1 - n) * CELL_H).rstrip()
            for n in range(ROWS)]
    # Окно накрывает первые сорок строк истории; сорок первая — уже в кадре.
    assert rows[0] == hist.texts()[0].rstrip(), rows[0]
    assert rows[-1] == hist.texts()[-2].rstrip(), rows[-1]
    assert rows[0] == ' СТРОКА 01', rows[0]
    assert rows[-1] == ' СТРОКА 40', rows[-1]


# ── окно терминала ──────────────────────────────────────────────────────────

def make_window(monkeypatch, **kw):
    """Окно с поддельным Tk — без дисплея, с настройками из `kw`."""
    from mc7401.window import TerminalWindow
    import test_frontend as T
    monkeypatch.setitem(sys.modules, 'tkinter',
                        types.SimpleNamespace(PhotoImage=T.FakePhoto))
    cfg = Config.default()
    for k, v in kw.items():
        setattr(cfg, k, v)
    win = TerminalWindow(None, config=cfg)
    win._canvas = T.FakeCanvas(1200, 900)
    win._image_id = 1
    win._tk = T.FakeRoot()
    return win


def test_window_takes_limits_from_config(monkeypatch):
    """Глубина истории и шагов отмены берутся из конфигурации."""
    win = make_window(monkeypatch, history=250, undo=7)
    assert win.hist.limit == 250
    assert win.undo.limit == 7
    assert win.console.history is win.hist, 'консоль не знает об истории'


def test_window_history_fills_on_scrolling_text(monkeypatch):
    """Вывод, длиннее кадра, кладёт ушедшие строки в историю окна."""
    win = make_window(monkeypatch)
    fill(win.console, 80)
    assert win.hist.size == 80 - ROWS + 1


def test_window_shows_history_when_scrolled_back(monkeypatch):
    """Колесо листает историю строк, а у кадра окно собирается."""
    win = make_window(monkeypatch)
    fill(win.console, 80)
    win._touch_frame()
    assert win.scroll_history(-2) == 2
    view = win.shown_frame()
    top = read_row(view, (ROWS - 1) * CELL_H).rstrip()
    assert top == ' СТРОКА 40', f'верх окна: {top!r}'
    assert win.scroll_history(2) == 0
    assert win.hist.position == 0


def test_window_scroll_stops_at_the_oldest_line(monkeypatch):
    """Прокрутка упирается в самую старую строку и не уходит в пустоту."""
    win = make_window(monkeypatch)
    fill(win.console, 80)
    win._touch_frame()
    for _ in range(win.hist.size + 5):
        win.scroll_history(-1)
    assert win.hist.position == win.hist.size, 'прокрутка ушла в пустоту'
    assert win.scroll_history(1) == win.hist.size - 1


def test_wheel_scrolls_lines_then_frames(monkeypatch):
    """Колесо сперва листает строки, а у самой старой — кадры `F5 ОЧИСТКА`."""
    win = make_window(monkeypatch)
    fill(win.console, 80)
    win._touch_frame()
    old = win.emu.fb
    win.clear_screen()                # F5 ОЧИСТКА: кадр ушёл в историю кадров
    assert win._history, 'кадр не сложился в историю'
    # Первое колесо назад уходит по строкам.
    assert win.scroll_display(-1) is True
    assert win.hist.position == 1
    # У самой старой строки колесо переходит к кадрам.
    win.hist.scroll(-win.hist.size)
    assert win.scroll_display(-1) is False
    assert win._hist_at is not None, 'колесо не дошло до кадров F5'


def test_wheel_returns_to_the_frame(monkeypatch):
    """Прокрутка вперёд всегда доводит окно до текущего кадра."""
    win = make_window(monkeypatch)
    fill(win.console, 80)
    win._touch_frame()
    for _ in range(5):
        win.scroll_display(-1)
    assert win.hist.position == 5
    for _ in range(10):
        win.scroll_display(1)
    assert win.hist.position == 0
    assert win._hist_at is None


def test_footer_shows_where_the_window_is(monkeypatch):
    """Ячейка `ИСТ=` пуста у живого кадра и `-N` при листании назад."""
    from mc7401.panel import EMPTY, Status, state_rows

    def ist(value: str) -> str:
        cells = state_rows(Status(history=value))
        label, val = next(c for c in cells if c[0][0] == 'ИСТ=')
        assert label[1] == 'label'
        return val[0]

    assert ist('') == EMPTY, 'у живого кадра ИСТ должен быть пуст'
    assert ist('-137') == '-137'
    assert '-' in EMPTY, 'у пустой ячейки должен быть знак-заполнитель'

    win = make_window(monkeypatch)
    fill(win.console, 80)
    win._touch_frame()
    assert win.status().history == '', 'ИСТ горит у живого кадра'
    win.scroll_display(-3)
    assert win.status().history == '-3'


def test_footer_height_unchanged(monkeypatch):
    """Новая ячейка `ИСТ=` не съела ряд подвала."""
    from mc7401.chrome import footer_rows
    from mc7401.panel import Status
    for help_ in (True, False):
        rows = footer_rows(Status(history='-137'), help_=help_)
        assert len(rows) == (7 if help_ else 5), f'подвал {len(rows)} рядов'


def test_undo_depth_comes_from_config(monkeypatch):
    """Число шагов правки — из конфигурации, а не константой."""
    win = make_window(monkeypatch, undo=3)
    win.session.set_mode('local')       # история правки живёт в АВТОНОМНО
    for i in range(10):
        win._undo_push(f'шаг {i}')
    assert len(win.undo) == 3, 'глубина отмены не ограничена конфигурацией'


def test_config_reads_history_and_undo(tmp_path=None):
    """`[display] history` и `[display] undo` читаются из файла."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'mc7401.conf'
        path.write_text('[display]\nhistory = 500\nundo = 33\n',
                        encoding='utf-8')
        cfg = Config.load(path)
        assert cfg.history == 500
        assert cfg.undo == 33


# ── быстродействие: кэш окна и раскладка кадра ──────────────────────────────

def test_view_cache_follows_the_frame_top(monkeypatch):
    """Кэш окна следит за верхом кадра: ЭВМ пишет — окно пересобирается.

    Окно кэшируется, и ошибка здесь страшнее медленной перерисовки:
    застывшее окно показывало бы оператору уже ушедшую выдачу.
    """
    win = make_window(monkeypatch)
    fill(win.console, 80)
    win._touch_frame()
    win.scroll_display(-3)
    first = read_row(win.shown_frame(), (ROWS - 1) * CELL_H).rstrip()
    assert win.shown_frame() is win.shown_frame(), 'окно не кэшировано'

    # Кадр дописан **внизу**: верх окна не меняется — кэш оправдан.
    win.console.write(b'niz\r\n')
    assert read_row(win.shown_frame(), (ROWS - 1) * CELL_H).rstrip() == first

    # Новый кадр от ЭВМ (F5) — верх другой, окно обязано пересобраться.
    win.clear_screen()
    win.console.write(b'novyy kad\r\n')
    win.hist.back = 1                     # окно вплотную к кадру
    view = win.shown_frame()
    top = read_row(view, (ROWS - 1) * CELL_H).rstrip()
    frame_top = read_row(win.emu.fb, (ROWS - 1) * CELL_H).rstrip()
    assert frame_top, 'новый кадр пуст'
    # Под последней строкой истории окно показывает верх кадра — и он
    # принадлежит новому кадру, а не тому, что был до F5.
    below = read_row(view, (ROWS - 2) * CELL_H).rstrip()
    assert below == frame_top, (
        f'окно показало застывшее: {below!r} != {frame_top!r}')
    assert top == win.hist.texts()[-1].rstrip(), f'верх окна: {top!r}'


def test_view_cache_is_dropped_when_history_moves(monkeypatch):
    """Прокрутка на строку меняет окно, а не достаёт старое из кэша."""
    win = make_window(monkeypatch)
    fill(win.console, 80)
    win._touch_frame()
    win.scroll_display(-1)
    a = read_row(win.shown_frame(), (ROWS - 1) * CELL_H).rstrip()
    win.scroll_display(-1)
    b = read_row(win.shown_frame(), (ROWS - 1) * CELL_H).rstrip()
    assert a != b, 'колесо не сдвинуло окно'


def test_framebuffer_bit_count_survives_fast_paths():
    """Быстрые пути не врут про число зажжённых точек.

    Подсчёт битов переведён на таблицу (`popcount`) — на нём стоит
    `count_set()`, то есть счётчик точек в строке подвала и в логике
    сбора кадра для показа.
    """
    from mc7401.framebuffer import popcount

    assert popcount(b'') == 0
    assert popcount(b'\x00') == 0
    assert popcount(b'\xff') == 8
    assert popcount(b'\x81') == 2
    assert popcount(bytes(range(256))) == sum(i.bit_count()
                                             for i in range(256))

    fb = Framebuffer()
    fb.hline(0, 559, 399)
    assert fb.count_set() == 560
    row = fb.row_bytes(399)
    fb.put_row_bytes(399, bytes(70))
    assert fb.count_set() == 0, 'ряд не вычищен из счётчика'
    fb.put_row_bytes(399, row)
    assert fb.count_set() == 560, 'ряд не вернулся в счётчик'
    gone = fb.scroll_up(10)
    assert fb.count_set() == 0
    assert sum(popcount(r) for r in gone) == 560


def test_top_bits_and_from_bits_round_trip():
    """`top_bits` и `from_bits` — обратные друг другу операции."""
    fb = Framebuffer()
    fb.hline(0, 559, 399)
    fb.hline(0, 559, 0)
    fb.point(300, 200)
    top = fb.top_bits(fb.height)
    assert len(top) == fb.width * fb.height // 8
    back = Framebuffer.from_bits(fb.width, fb.height, top)
    assert back._bits == fb._bits
    assert back.count_set() == fb.count_set()
    assert fb.top_bits(10) == fb.top_bits(10)[:700]
    assert len(fb.top_bits(10)) == 700
    assert fb.top_bits(0) == b''


def test_cursor_hidden_while_scrolling_back(monkeypatch):
    """При листании истории рамки курсора на экране нет.

    Окно сдвинуто, а рамка осталась бы на месте прежнего курсора —
    поверх чужой выдачи.  У курсора к истории никакого отношения нет, как
    и при показе кадра из истории `F5`.
    """
    from test_frontend import FakePhoto

    win = make_window(monkeypatch)
    fill(win.console, 80)
    win._touch_frame()
    win._blink_on = True

    def cursor_sides():
        """Сколько сторон рамки курсора выведено — у рамки их четыре."""
        photo = FakePhoto()
        win._cursor_frame_scaled(photo, 0, 0, 1.0)
        return len(photo.puts)

    assert cursor_sides() == 4, 'у живого кадра курсор не виден'
    win.scroll_display(-4)
    assert win.hist.position == 4
    assert cursor_sides() == 0, 'рамка курсора висит поверх истории'
    # И на кадре из истории `F5 ОЧИСТКА` курсор тоже не рисуется.
    win.scroll_display(4)
    win.clear_screen()
    win._hist_at = 0
    assert cursor_sides() == 0, 'рамка курсора на кадре из истории'


def test_wheel_scrolls_three_lines(monkeypatch):
    """Колесо листает по три строки — как на 15ИЭ (`WHEEL_STEP`).

    По одной строке страницу выдачи пришлось бы возвращать сорока
    щелчками, а перерисовка окна стоит десятки миллисекунд.
    """
    from mc7401.window import TerminalWindow

    assert TerminalWindow.WHEEL_STEP == 3, 'шаг колеса разошёлся с 15ИЭ'
    win = make_window(monkeypatch)
    fill(win.console, 200)
    win._touch_frame()
    win._wheel_step(-1)
    assert win.hist.position == 3
    win._wheel_step(-1)
    assert win.hist.position == 6
    win._wheel_step(1)
    assert win.hist.position == 3


def test_page_up_scrolls_a_page(monkeypatch):
    """`ЭКРАН↑` листает на страницу: окно минус одна строка."""
    win = make_window(monkeypatch)
    fill(win.console, 200)
    win._touch_frame()
    assert win.page_step() == ROWS - 1
    win.scroll_display(-1, win.page_step())
    assert win.hist.position == ROWS - 1
    win.scroll_display(-1, win.page_step())
    assert win.hist.position == 2 * (ROWS - 1)
    win.scroll_display(1, win.page_step())
    assert win.hist.position == ROWS - 1


def test_step_does_not_overshoot_the_oldest_line(monkeypatch):
    """Большой шаг не уходит за самую старую строку."""
    win = make_window(monkeypatch)
    fill(win.console, 60)                 # истории всего 21 строка
    win._touch_frame()
    win.scroll_display(-1, win.page_step())
    assert win.hist.position == win.hist.size, 'прокрутка ушла в пустоту'
    assert win.hist.position == 21
