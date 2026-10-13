"""Проверки фронтенда: формы клавиш, окно просмотра, перо, kitty, окно Tk.

Собрано по дефектам ревью: `read_key` и `handle_key` расходились по
форме нажатий (ВК, F1…F4, Backspace), стрелки ←/→ шли в обратную
сторону, перо зеркалилось, ряды мыши считались не от кадра, чанки
kitty несли ключи не в том куске, ошибки протокола не ловились,
моно-вывод терял ряды, окно Tk заливало промежутки и удваивало ввод.

GUI-места проверяются через фейки, без реального дисплея.
"""

from __future__ import annotations

import io
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mc7401.config import Config                                   # noqa: E402
from mc7401.console import CELL_H, CELL_W                          # noqa: E402
from mc7401.screen import MONO_OFF, MONO_ON, ScreenRenderer, Viewport  # noqa: E402
from mc7401.framebuffer import Framebuffer                         # noqa: E402
from mc7401.tui import TerminalApp                                 # noqa: E402
from mc7401 import window                                          # noqa: E402


class FakeDriver:
    """Драйвер без линии: отправка собирается."""

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
    return TerminalApp(FakeDriver(), config=Config.default())


# ── term.py: канонические формы нажатий ─────────────────────────────────────

class FakeStdin:
    """Вход с заранее заданными нажатиями."""

    def __init__(self, text: str) -> None:
        self.buf = text

    def read(self, n: int = 1) -> str:
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def fileno(self) -> int:
        return 0


def type_keys(monkeypatch, text: str) -> list:
    """Подсунуть `read_key` нажатия и вернуть всё, что он прочитал."""
    from mc7401 import term
    import select as _select
    fake = FakeStdin(text)
    monkeypatch.setattr(sys, 'stdin', fake)
    monkeypatch.setattr(
        _select, 'select',
        lambda readers, writers, errs, timeout=None:
            (readers if fake.buf else [], [], []))
    out: list = []
    while True:
        key = term.read_key(timeout=0.0)
        if key is None:
            return out
        out.append(key)


class FakeBufferStdin:
    """Stdin, отдающий все байты одной операцией (read-ahead в буфере)."""

    class _Buf:
        def __init__(self, data: bytes) -> None:
            self.data = data

        def read1(self, n: int) -> bytes:
            out, self.data = self.data[:n], self.data[n:]
            return out

    def __init__(self, data: bytes) -> None:
        self.buffer = self._Buf(data)

    def read(self, n: int = 1) -> str:
        out, self.buffer.data = (self.buffer.data[:n], self.buffer.data[n:])
        return out.decode('utf-8', 'replace')

    def fileno(self) -> int:
        return 0


def test_read_key_survives_read_ahead(monkeypatch):
    """Escape-последовательность не рвётся при read-ahead буфера stdin.

    `read1` отдаёт все байты одной операцией, после чего `select` данных
    больше не видит: раньше остаток терялся, стрелка приходила как
    `ESC`, и клавиши «не работали».
    """
    from mc7401 import term
    import select as _select
    fake = FakeBufferStdin(b'\x1b[A\x1b[15~\xd0\xb4')
    monkeypatch.setattr(sys, 'stdin', fake)
    monkeypatch.setattr(
        _select, 'select',
        lambda readers, writers, errs, timeout=None:
            (readers if fake.buffer.data else [], [], []))
    keys: list = []
    while True:
        key = term.read_key(timeout=0.0)
        if key is None:
            break
        keys.append(key)
    assert keys == ['KEY_UP', 'KEY_CLEAR', 'д']


def test_read_key_canonical_forms(monkeypatch):
    """`ВК`/`ПС` — как есть, Backspace — именем, `^X` — каретой."""
    keys = type_keys(monkeypatch, '\r\n\x7f\x08\x03a')
    assert keys == ['\r', '\n', 'KEY_BACKSPACE', 'KEY_BACKSPACE', '^C', 'a']


def test_read_key_function_keys(monkeypatch):
    """F1…F4 приходят именами, а не `ESC` (раньше стирали строку)."""
    from mc7401.term import KEY_CLEAR
    keys = type_keys(monkeypatch, '\x1bOP\x1bOQ\x1bOR\x1bOS'
                                  '\x1b[A\x1b[B\x1b[15~')
    assert keys == ['KEY_F1', 'KEY_F2', 'KEY_F3', 'KEY_F4',
                    'KEY_UP', 'KEY_DOWN', KEY_CLEAR]


def test_read_key_passes_mouse_sequence(monkeypatch):
    """SGR-мышь доходит целиком — это световое перо."""
    seq = '\x1b[<0;20;10M'
    assert type_keys(monkeypatch, seq) == [seq]


def test_read_key_lone_escape(monkeypatch):
    assert type_keys(monkeypatch, '\x1b') == ['ESC']


def test_read_key_enter_reaches_submit(monkeypatch):
    """Формы сходятся: `read_key` отдаёт то, что `handle_key` понимает.

    Раньше `read_key` превращал `ВК` в `'^M'`, и ветка отправки строки
    была мертва в реальной работе.
    """
    app = TerminalApp(FakeDriver(), config=Config.default())
    keys = type_keys(monkeypatch, 'С\r')
    assert keys == ['С', '\r']
    for key in keys:
        app.handle_key(key)
    assert app.driver.sent == ['s', '\r']


# ── tui.handle_key: формы нажатий ───────────────────────────────────────────

def test_carriage_notation_submits(app):
    """`^M`/`^J` — это `ВК`, а не текст."""
    for key in 'СЕТ ':
        app.handle_key(key)
    app.handle_key('^M')
    assert app.driver.sent == ['s', 'e', 't', ' ', '\r']
    app.handle_key('^J')
    assert app.driver.sent[-1] == '\r'


def test_backspace_deletes_in_all_forms(app):
    """Backspace удаляет знак: и именем, и в обеих нотациях."""
    for key in ('KEY_BACKSPACE', '^H', '\x7f'):
        app.handle_key('а')
        app.handle_key(key)
        assert app.input_line == '', key


def test_backspace_does_not_lock_ctrl(app):
    """`^H` — удаление знака, а не переключатель `Ctrl`."""
    app.handle_key('^H')
    assert app.ctrl is False, '^H переключил Ctrl вместо удаления знака'


def test_ctrl_lock_has_own_combination(app):
    """Замок `Ctrl` — на своей комбинации (`Ctrl-]`)."""
    app.handle_key('^]')
    assert app.ctrl is True
    app.handle_key('^]')
    assert app.ctrl is False


def test_ctrl_codes_are_ctrl_combinations(app):
    """`Ctrl-q` приходит управляющим кодом и работает как ВЫХОД."""
    app.handle_key('^Q')
    assert app.running is False


def test_function_keys_do_not_erase_input(app):
    """F1…F4 не считаются вводом и не стирают набранную строку."""
    from mc7401.term import _arrow_name
    assert _arrow_name('OP') == 'KEY_F1'
    assert _arrow_name('OQ') == 'KEY_F2'
    assert _arrow_name('OR') == 'KEY_F3'
    assert _arrow_name('OS') == 'KEY_F4'
    for key in 'ЛИН':
        app.handle_key(key)
    for key in ('KEY_F1', 'KEY_F2', 'KEY_F3', 'KEY_F4'):
        app.handle_key(key)
    assert app.input_line == 'ЛИН', 'F1…F4 стирают набранную строку'


# ── tui: окно просмотра ─────────────────────────────────────────────────────

def test_arrows_move_frame_in_arrow_direction(app):
    """Стрелка указывает, куда движется кадр; ←/→ не инвертированы."""
    app.emu.fb.point(100, 100)
    vp = app.renderer.vp
    vp.scale = 1
    vp.x, vp.y = 100, 100
    app.handle_key('KEY_RIGHT')
    assert vp.x < 100, '→ должен двигать кадр вправо, а не влево'
    app.handle_key('KEY_LEFT')
    assert vp.x == 100
    app.handle_key('KEY_UP')
    assert vp.y > 100
    app.handle_key('KEY_DOWN')
    assert vp.y == 100


# ── tui: световое перо ──────────────────────────────────────────────────────

def test_pen_overlay_is_not_mirrored(app):
    """Перо показывается на своём ряду кадра, а не зеркально.

    `pen.y` — координата кадра «Y вверх», `vp.y` — смещение сверху:
    без переворота Y перо уходило вниз.
    """
    fb = app.emu.fb
    fb.point(20, 399)                       # верх кадра
    app.renderer.mode = 'mono'
    vp = app.renderer.vp
    vp.scale, vp.x, vp.y = 1, 0, 0
    app.pen.enabled = True
    app.pen.x, app.pen.y = 20, 399
    body = app.renderer.render(40, 8)
    rows = app._overlay_pen(body, 40, 8).split('\n')
    assert '\x1b[7m' in rows[0], 'перо должно быть на первом ряду кадра'
    assert all('\x1b[7m' not in ln for ln in rows[1:])


def test_mouse_rows_are_relative_to_frame(app):
    """Ряд мыши пересчитывается от начала кадра.

    Кадр ниже заголовка и текстовых строк ЭВМ, а `feed_mouse` считает,
    что он начинается с первого ряда.
    """
    app.feed(b'\x54\x30\x31\x37\r\n')       # «Т017» — строка текста ЭВМ
    app.emu.fb.point(100, 399)
    app.pen.enabled = True
    assert app.config.title is True
    vp = app.renderer.vp
    vp.scale, vp.x, vp.y = 1, 0, 0
    # Первый проход подбирает масштаб под геометрию окна; дальше смена
    # любой части ключа `(кадр, геометрия)` вписывает кадр заново, поэтому
    # второй проход идёт при заданном вручную масштабе 1.
    app._body_lines(80, 20)
    vp.scale, vp.x, vp.y = 1, 0, 0
    app._body_lines(80, 20)                 # считает смещение кадра
    assert app._body_offset == 1            # одна строка текста над кадром
    # Клик в ряд 3 окна: заголовок + текст + первый ряд кадра.
    app.handle_key('\x1b[<0;101;3M')
    assert (app.pen.x, app.pen.y) == (100, 399)


# ── kitty: чанки, ошибки, размещение ────────────────────────────────────────

def test_kitty_error_response_is_not_ok():
    """Ошибки протокола (`ENOTSUP`, `ENOENT:…`) не считаются успехом."""
    from mc7401.backends.kitty import KittyGraphics, parse_response
    g = KittyGraphics()
    assert not g.apply_response('\x1b_Gi=31;ENOTSUP\x1b\\')
    assert not g.active
    assert not g.apply_response('\x1b_Gi=31;ENOENT:нет образа\x1b\\')
    fields = parse_response('\x1b_Gi=31;ENOTSUP\x1b\\')
    assert fields and fields.get('error') == 'ENOTSUP'


def test_kitty_blit_uses_source_rect_and_cursor():
    """`x,y` — прямоугольник источника, место на экране — позиция курсора.

    Раньше `x,y` получали координаты знакомест на экране, и картинка
    обрезалась по верхнему краю исходного изображения.
    """
    from mc7401.backends.kitty import KittyGraphics
    buf = io.StringIO()
    g = KittyGraphics(buf)
    g.cell = (9, 16)
    g.active = True
    fb = Framebuffer()
    fb.point(0, 0)
    assert g.blit(fb, row=3, col=5, cols=70, rows=25, force=True)
    out = buf.getvalue()
    # Картинка кладётся туда, где стоит курсор: ряд 4, столбец 6 (с нуля).
    assert out.startswith('\x1b[4;6H')
    keys = out.split('\x1b_G', 1)[1].split(';', 1)[0]
    assert 'x=0' in keys and 'y=0' in keys     # кадр передаётся целиком
    assert 'w=560' in keys and 'h=400' in keys
    assert 'c=70' in keys and 'r=25' in keys   # размер размещения
    assert 'X=3' in keys                       # смещение внутри клетки
    assert 'x=5' not in keys and 'y=3' not in keys


# ── screen.py: моно и цвет ──────────────────────────────────────────────────

def test_mono_cells_are_on_or_off():
    """Нижний пиксель ячейки зажигает её; пустая — «выключено».

    Раньше ячейка с нижним пикселем рисовалась символом «выключено»,
    а пустая — пробелом: горизонтальные линии на нечётных рядах
    исчезали.
    """
    fb = Framebuffer()
    fb.point(0, 398)                        # нижний пиксель верхней ячейки
    r = ScreenRenderer(fb, mode='mono', vp=Viewport(scale=1, x=0, y=0))
    line = r.render(2, 1).split('\n')[0]
    assert line == MONO_ON + MONO_OFF


def test_color_keeps_one_char_per_cell():
    """Цветной вывод — по знаку на ячейку, координаты не схлопываются."""
    fb = Framebuffer()
    fb.point(2, 399)                        # верхний пиксель ячейки
    r = ScreenRenderer(fb, mode='color', vp=Viewport(scale=1, x=0, y=0))
    line = r.render(5, 1).split('\n')[0]
    import re
    visible = re.sub(r'\x1b\[[0-9;]*m', '', line)
    assert len(visible) == 5, visible
    assert visible == '  ▀  '


# ── window.py: окно Tk через фейки ──────────────────────────────────────────
# Окно — только кадр 560×400: текст БЭСМ-6 и ввод рисуются на кадре
# глифами эталона СИМ (`mc7401.console`) в позиции курсора; поле ввода
# и текстовый буфер под кадром ликвидированы.

class FakePhoto:

    def __init__(self, master=None, width=0, height=0):
        self.width, self.height = width, height
        self.puts: list = []
        self.zoomed = 0
        self.subsampled = 0

    def put(self, data, to=None):
        self.puts.append((to, data))

    def zoom(self, factor):
        self.zoomed = factor
        return self

    def subsample(self, factor):
        self.subsampled = factor
        return self


class FakeCanvas:

    def __init__(self):
        self.configs: list = []

    def itemconfig(self, *args, **kwargs):
        self.configs.append((args, kwargs))


class FakeLabel:

    def configure(self, **kwargs):
        pass


class FakeText:
    """Виджет панели `tk.Text`: собирает текст и теги по диапазонам.

    Строка состояния и подвал — `tk.Text`, а не `Label`: цвет задаётся
    тегами на диапазонах (`panel.render_spans`), иначе оформление 15ИЭ
    (зелёное `ВКЛ`, красное `ВЫКЛ`, жёлтые F-клавиши) не выразить.
    """

    def __init__(self):
        self.text = ''
        self.tags: list = []

    def configure(self, **kwargs):
        pass

    def tag_configure(self, name, **kwargs):
        pass

    def delete(self, *args):
        self.text, self.tags = '', []

    def index(self, where):
        # `end-1c` — позиция перед последним знаком; `1.0` — начало.
        return '1.0' if where == '1.0' else '1.%d' % len(self.text)

    def insert(self, where, chunk):
        self.text += chunk

    def tag_add(self, name, start, end):
        self.tags.append((name, start, end))


class FakeRoot:

    def __init__(self, focus=None):
        self._focus = focus

    def focus_get(self):
        return self._focus


class FakeEvent:

    def __init__(self, keysym, char='', state=0):
        self.keysym = keysym
        self.char = char
        self.state = state


class FakeClick:

    def __init__(self, x, y):
        self.x, self.y = x, y


class FakeRoot:

    def __init__(self, focus=None):
        self._focus = focus
        self._width = 1200

    def focus_get(self):
        return self._focus

    def winfo_width(self):
        return self._width


def make_window(monkeypatch, *, scale=1):
    monkeypatch.setitem(sys.modules, 'tkinter',
                        types.SimpleNamespace(PhotoImage=FakePhoto))
    win = window.TerminalWindow(None, scale=scale)
    win._canvas = FakeCanvas()
    win._image_id = 1
    win.lbl_status = FakeText()
    win.lbl_panel = FakeText()
    win._tk = FakeRoot()
    return win


def test_window_paint_draws_point_runs(monkeypatch):
    """`PhotoImage.put` получает строку цвета и прямоугольник серии:
    точки (0,0) и (2,0) — в левом нижнем углу кадра, это ряд y=0.

    Кадр смещён вниз на строку состояния: она рисуется над кадром, как
    на 15ИЭ, и кадр терминала от неё не страдает.
    """
    win = make_window(monkeypatch)
    y = win.footer_px() + 5          # выше подвала — точка видна
    win.emu.fb.point(0, y)           # левый край кадра
    win.emu.fb.point(2, y)
    win.paint()
    row = win.top_px() + window.SCREEN_HEIGHT - 1 - y
    height = window.SCREEN_HEIGHT + win.top_px()
    bg_fill = (0, 0, window.SCREEN_WIDTH, height)
    runs = [to for to, color in win._photo.puts
            if to != bg_fill and color == window.FG
            and to[1] == row and to[3] == row + 1]
    lit = [(x0, x1) for x0, _, x1, _ in runs]
    assert any(x0 <= 0 < x1 for x0, x1 in lit), runs
    assert any(x0 <= 2 < x1 for x0, x1 in lit), runs
    assert all(isinstance(color, str) and color.startswith('#')
               for _, color in win._photo.puts)


def test_window_paint_zooms_up(monkeypatch):
    win = make_window(monkeypatch, scale=3)
    win.paint()
    assert win._photo.zoomed == 3
    assert win._photo.subsampled == 0


def test_window_paint_shows_frame_plus_status(monkeypatch):
    """Холст — кадр 560×400 и строка состояния над ним.

    Строка состояния в окне **вне** кадра, как на 15ИЭ, поэтому она
    добавляет высоту; подвал поверх нижней кромки кадра и высоты не
    добавляет — иначе кадр не влезет в экран целиком.  Нарисованы обе
    глифами ПЗУ того же размера, что и знак в кадре.
    """
    win = make_window(monkeypatch)
    win.paint()
    assert win._photo.width == window.SCREEN_WIDTH
    assert win._photo.height == window.SCREEN_HEIGHT + win.top_px()


def test_window_host_text_is_drawn_on_frame(monkeypatch):
    """Текст из БЭСМ-6 рисуется на кадре глифами СИМ в позиции курсора."""
    win = make_window(monkeypatch)
    win.console.goto(0, 0)
    win.session_text(b'ABC')
    # Знаки нарисованы: в кадре появились точки в верхних рядах.
    assert win.emu.fb.count_set() > 0
    top = window.SCREEN_HEIGHT - 1
    assert any(win.emu.fb.get(x, y)
               for y in range(top - 7, top + 1)
               for x in range(0, 21))
    # Курсор прошёл три знакомёста.
    assert win.console.col == 3 and win.console.row == 0


def test_window_host_text_moves_with_cursor(monkeypatch):
    """Текст из БЭСМ-6 выводится в позиции курсора, а не в углу."""
    win = make_window(monkeypatch)
    win.console.goto(10, 5)
    win.session_text(b'A')
    x0, y0, x1, y1 = win.console.cell_rect(10, 5)
    assert any(win.emu.fb.get(x, y)
               for y in range(y0, y1 + 1) for x in range(x0, x0 + 7))
    assert win.console.col == 11 and win.console.row == 5


def test_window_letters_type_at_cursor(monkeypatch):
    """Набор: знак уходит в линию и рисуется глифом в позиции курсора."""
    win = make_window(monkeypatch)
    win.driver = FakeDriver()
    win.console.goto(0, 0)
    win._on_key(FakeEvent('e', 'e'))
    assert win.session.mode == 'host'
    assert win.session.echo is True, 'e переключила ЭХО'
    assert win.driver.sent == ['e'], 'e не ушла в линию'
    assert win.emu.fb.count_set() > 0, 'знак не нарисован на кадре'
    assert win.console.col == 1, 'курсор не сдвинулся'
    win._on_key(FakeEvent('F6', ''))        # ЭХО — служебной клавишей
    assert win.session.echo is False


def test_window_echo_off_skips_glyph(monkeypatch):
    """Без эхо набранный знак не рисуется (например, пароль)."""
    win = make_window(monkeypatch)
    win.driver = FakeDriver()
    win.session.toggle_echo()
    win._on_key(FakeEvent('e', 'e'))
    assert win.driver.sent == ['e'], 'e не ушла в линию'
    assert win.emu.fb.count_set() == 0, 'эхо выключено — знака быть не должно'


def test_window_arrows_move_cursor_by_cells(monkeypatch):
    win = make_window(monkeypatch)
    win.console.goto(5, 5)
    win._on_key(FakeEvent('Right', ''))
    assert (win.console.col, win.console.row) == (6, 5)
    win._on_key(FakeEvent('Up', ''))
    assert (win.console.col, win.console.row) == (6, 4)
    win._on_key(FakeEvent('Left', ''))
    win._on_key(FakeEvent('Down', ''))
    assert (win.console.col, win.console.row) == (5, 5)


def test_window_mouse_click_puts_cursor(monkeypatch):
    """Щелчок мышью ставит курсор в знакомёст под указателем.

    Знакомест — шаг `CELL_W × CELL_H` (16×20), а не размер растра знака
    14×16: растр уже с зазором (см. `console.CELL_W/CELL_H`).  Щёлкать
    надо в свободной полосе кадра — подвал лежит поверх и указателя
    не принимает.
    """
    win = make_window(monkeypatch)
    top, foot = win.top_px(), win.footer_px()
    y = top + (window.SCREEN_HEIGHT - foot) // 2        # середина кадра
    win._on_click(FakeClick(3 * CELL_W, y))
    assert (win.console.col, win.console.row) == (3, (y - top) // CELL_H)


def test_window_chrome_ignores_clicks_in_footer(monkeypatch):
    """Клик по подвалу не двигает курсор: подвал лежит поверх кадра."""
    win = make_window(monkeypatch)
    top, foot = win.top_px(), win.footer_px()
    y = top + (window.SCREEN_HEIGHT - foot) // 2
    win._on_click(FakeClick(3 * CELL_W, y))
    before = (win.console.col, win.console.row)
    # Последний ряд подвала — у нижней кромки кадра.
    foot_y = window.SCREEN_HEIGHT + top - CELL_H // 2
    assert win._in_chrome(foot_y), 'подвал должен распознаваться'
    win._on_click(FakeClick(10 * CELL_W, foot_y))
    assert (win.console.col, win.console.row) == before, \
        'клик по подвалу не должен двигать курсор'


def test_window_click_above_frame_clamps_to_top(monkeypatch):
    """Клик над кадром (в строке состояния) прижимается к верхнему ряду.

    Строка состояния в окне **вне** кадра, и указателя кадра там нет —
    координата всё равно обязана остаться в кадре.
    """
    win = make_window(monkeypatch)
    top, foot = win.top_px(), win.footer_px()
    assert not win._in_chrome(1), 'строка состояния не кадр'
    win._on_click(FakeClick(3 * CELL_W, 1))
    assert win.console.row == 0, 'указатель ушёл выше кадра'
    assert 0 <= win.console.col < window.SCREEN_WIDTH // CELL_W


def test_window_backspace_erases_glyph(monkeypatch):
    """ВК стирает знакомёст слева от курсора."""
    win = make_window(monkeypatch)
    win.driver = FakeDriver()
    win.console.goto(0, 0)
    win._on_key(FakeEvent('e', 'e'))
    assert win.emu.fb.count_set() > 0
    win._on_key(FakeEvent('BackSpace', '\x08'))
    assert win.emu.fb.count_set() == 0, 'знак не стёрт'
    assert win.console.col == 0
    assert win.driver.sent == ['e', '\x08'], 'в линию не ушёл код ВК'


def test_window_return_sends_line_end(monkeypatch):
    """ПРПС в С ЭВМ: знаки уже ушли по одному, посылается конец строки."""
    win = make_window(monkeypatch)
    win.driver = FakeDriver()
    for ch in 'set':
        win._on_key(FakeEvent(ch, ch))
    assert win._on_return() == 'break'
    assert win.driver.sent == ['s', 'e', 't', '\r']
    assert win.console.col == 0, 'ПРПС переводит на новую строку'
    assert win.console.row == 1


def test_window_return_sends_buffer_in_local(monkeypatch):
    """В АВТОНОМНО набор копится в буфер и уходит по ПЕРЕДАЧА (Enter)."""
    win = make_window(monkeypatch)
    win.driver = FakeDriver()
    win.session.toggle_mode()               # С ЭВМ → АВТОНОМНО
    for ch in 'лин3':
        win._on_key(FakeEvent(ch, ch))
    assert win.driver.sent == [], 'АВТОНОМНО: знаки не должны уходить сразу'
    assert win.session.buffer == ['л', 'и', 'н', '3']
    win._on_return()
    assert win.driver.sent == ['lin3\r']


def test_window_escape_clears_input_not_closes(monkeypatch):
    win = make_window(monkeypatch)
    win.driver = FakeDriver()
    win._on_key(FakeEvent('e', 'e'))
    win._on_key(FakeEvent('x', 'x'))
    assert win._on_escape() == 'break'
    assert win.console.col == 0, 'Escape стёр набор до начала строки'
    assert win.emu.fb.count_set() == 0
    assert win.running is True
    assert win._on_key(FakeEvent('Escape', '\x1b')) == 'break'
    assert win.running is True


def test_window_function_keys_do_not_type(monkeypatch):
    """F5…F10 — служебные: на кадр ничего не попадает, буфер пуст."""
    win = make_window(monkeypatch)
    win.driver = FakeDriver()
    for key in ('F5', 'F6', 'F8', 'F9', 'F10'):
        win._on_key(FakeEvent(key, ''))
    assert win.emu.fb.count_set() == 0
    assert win.session.buffer == []
    assert win.driver.sent == []


def test_window_panel_keeps_session_modes(monkeypatch):
    """Панель показывает режимы АВТОНОМНО/С ЭВМ — они сохранены."""
    win = make_window(monkeypatch)
    win.driver = FakeDriver()
    text = '\n'.join(win._panel_lines())
    assert 'СЭВМ' in text
    win.session.toggle_mode()               # F9 — СЕТЬ
    text = '\n'.join(win._panel_lines())
    assert 'АВТО' in text
    assert win.session.mode == 'local'


# ── tui.py: кэш выгрузки картинки ──────────────────────────────────────────

class FakeGfx:
    """Протокол графики, считающий выгрузки кадра."""

    active = True
    cell = (8, 16)

    def __init__(self) -> None:
        self.uploads = 0

    def blit(self, *a, **kw) -> None:
        self.uploads += 1


def test_gfx_upload_cached_until_frame_changes(app):
    """Картинка не перекодируется на каждую отрисовку.

    Раньше `_paint_gfx` выгружал кадр протоколом графики при каждом
    обороте цикла — набор клавиш был очень медленным.
    """
    app.gfx = FakeGfx()
    app.splash = False                     # показывать кадр, а не заставку
    app.emu.fb.point(10, 10)
    app._paint_gfx(1, 80, 24)
    app._paint_gfx(1, 80, 24)
    assert app.gfx.uploads == 1            # второй раз — из кэша
    app.emu.fb.point(20, 20)               # кадр изменился
    app._paint_gfx(1, 80, 24)
    assert app.gfx.uploads == 2


def test_last_label_shows_code_and_symbol(app):
    """«ПОСЛ» — код выданного знака и сам знак (`≡144 'd'`).

    Оператору нужен и код (восьмеричный КОИ-8Р), и знак: один и тот же
    код в разных наборах означает разные буквы.
    """
    app.handle_key('d')
    assert app.last_key == "≡144 'd'"
    app.handle_key('KEY_CLEAR')            # F5 — ОЧИСТКА
    assert app.last_key == 'ОЧИСТКА'
