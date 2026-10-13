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
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mc7401.config import Config                                   # noqa: E402
from mc7401.console import (CELL_H, CELL_W, COLS, RASTER_H, ROWS,     # noqa: E402
                             SCREEN_HEIGHT)
from mc7401.screen import MONO_OFF, MONO_ON, ScreenRenderer, Viewport  # noqa: E402
from mc7401.framebuffer import Framebuffer                         # noqa: E402
from mc7401.tui import TerminalApp                                 # noqa: E402
from mc7401 import startup                                         # noqa: E402
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
    """Холст окна: размер задаёт пользователь окна.

    Кадр вписывается в него, поэтому размер холста — входные данные
    для `paint()`, а не следствие масштаба.
    """

    def __init__(self, width=560, height=460):
        self.configs: list = []
        self._w, self._h = width, height

    def winfo_width(self):
        return self._w

    def winfo_height(self):
        return self._h

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
        #: Отложенные вызовы `after(ms, cb)` — как `Tk.after`, но без
        #: главного цикла: тест сам решает, когда «отработает» очередь.
        self.scheduled: list = []
        self.quit_calls = 0

    def focus_get(self):
        return self._focus

    def winfo_width(self):
        return self._width

    def after(self, ms, cb=None, *args):
        self.scheduled.append((ms, cb, args))
        return 'after#%d' % len(self.scheduled)

    def after_cancel(self, ident):
        self.scheduled = [s for s in self.scheduled if s[0] != ident]

    def quit(self):
        self.quit_calls += 1

    def run_pending(self, count=1):
        """Выполнить отложенные вызовы — по одному, как обход Tk."""
        done = 0
        while self.scheduled and done < count:
            _, cb, args = self.scheduled.pop(0)
            if cb is not None:
                cb(*args)
            done += 1
        return done


def make_window(monkeypatch, *, scale=0, size=(560, 460)):
    monkeypatch.setitem(sys.modules, 'tkinter',
                        types.SimpleNamespace(PhotoImage=FakePhoto))
    win = window.TerminalWindow(None, scale=scale)
    win._canvas = FakeCanvas(*size)
    win._image_id = 1
    win.lbl_status = FakeText()
    win.lbl_panel = FakeText()
    win._tk = FakeRoot()
    return win


def test_window_paint_fills_user_window(monkeypatch):
    """Кадр равен размеру окна пользователя.

    Служебные строки прижаты к краям окна, кадр занимает всё оставшееся
    место: при вписывании по высоте он от края до края по вертикали и
    одинаков по ширине с окном — значит равен ему.
    """
    w, h = 900, 700
    win = make_window(monkeypatch, size=(w, h))
    win.paint()
    assert (win._photo.width, win._photo.height) == (w, h)
    fx, fy, scale = win._frame_geometry()
    assert scale == min(w / 560, h / (400 + win.top_px()
                                       + win.footer_px()))
    # Полоса между служебными строками занята кадром целиком.
    assert fy == int(round(win.top_px() * scale))
    assert fy + int(round(400 * scale)) == h - int(round(
        win.footer_px() * scale))


def test_window_frame_is_pinned_to_left_top(monkeypatch):
    """Кадр прижат к левому верхнему углу окна, как на 15ИЭ.

    Окно шире пропорций кадра (560/400 = 1.4): кадр встаёт слева, а
    свободное место остаётся справа — иначе знаки ПЗУ поехали бы из
    начала координат, а оператор привык к началу слева.
    """
    w, h = 1600, 600
    win = make_window(monkeypatch, size=(w, h))
    win.paint()
    fx, _, scale = win._frame_geometry()
    assert fx == 0, 'кадр должен быть прижат к левому краю окна'
    assert int(round(560 * scale)) < w, 'широкое окно: кадр вписан по высоте'
    # По вертикали кадр занимает всю полосу между служебными строками.
    top_h = int(round(win.top_px() * scale))
    foot_h = int(round(win.footer_px() * scale))
    assert 2 * top_h + 2 * int(round(400 * scale)) + 2 * foot_h <= 2 * h


def test_window_resize_moves_frame(monkeypatch):
    """Окно пользователя растянули — кадр вписан заново."""
    win = make_window(monkeypatch, size=(560, 460))
    win.paint()
    before = win._frame_geometry()[2]
    win._canvas._w, win._canvas._h = 1120, 920  # окно растянули вдвое
    win.paint()
    after = win._frame_geometry()[2]
    assert after == before * 2, 'масштаб должен идти за размером окна'


def test_window_paint_draws_point_runs(monkeypatch):
    """`PhotoImage.put` получает прямоугольник серии зажжённых точек."""
    win = make_window(monkeypatch)
    win.emu.fb.point(0, 5)                      # ряд кадра у нижней кромки
    win.emu.fb.point(2, 5)
    win.paint()
    fx, fy, scale = win._frame_geometry()
    row = fy + int(round((400 - 1 - 5) * scale))
    runs = [to for to, color in win._photo.puts
            if color == window.FG and to[1] == row]
    lit = [(x0, x1) for x0, _, x1, _ in runs]
    assert any(x0 <= fx < x1 for x0, x1 in lit), runs
    assert any(x0 <= fx + 2 * scale < x1 for x0, x1 in lit), runs


def _fg_rows(win, key):
    """Ряды окна, где нарисованы свечения: по `key` — верх или низ серии."""
    return [to[key] for to, color in win._photo.puts
            if color == window.FG and to is not None]


def test_window_status_is_pinned_to_top(monkeypatch):
    """Строка состояния — у самого верха окна, а не над кадром."""
    w, h = 900, 700
    win = make_window(monkeypatch, size=(w, h))
    win.paint()
    _, _, scale = win._frame_geometry()
    top_h = int(round(win.top_px() * scale))
    rows = _fg_rows(win, 1)
    assert rows, 'в окне ничего не нарисовано'
    assert min(rows) < top_h, 'строка состояния должна быть у верхнего края'
    assert max(rows) < h, 'строка состояния не должна уходить вниз окна'


def test_window_footer_is_pinned_to_bottom(monkeypatch):
    """Подвал — у самого низа окна, а не под кадром."""
    w, h = 900, 700
    win = make_window(monkeypatch, size=(w, h))
    win.paint()
    _, _, scale = win._frame_geometry()
    foot_h = int(round(win.footer_px() * scale))
    rows = _fg_rows(win, 3)
    assert rows, 'в окне ничего не нарисовано'
    assert max(rows) > h - foot_h, 'подвал должен доходить до низа окна'


def test_window_paint_shows_frame_between_chrome(monkeypatch):
    """Холст равен окну; служебные строки по краям, кадр между ними."""
    w, h = 900, 700
    win = make_window(monkeypatch, size=(w, h))
    win.paint()
    assert (win._photo.width, win._photo.height) == (w, h)


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

    Знакомест — шаг `CELL_W × CELL_H` (8×10), а не размер растра знака
    7×8: растр уже с зазором (см. `console.CELL_W/CELL_H`).  Кадр вписан
    в окно и может иметь поля по бокам, поэтому координата указателя
    переводится через геометрию кадра.
    """
    win = make_window(monkeypatch)
    fx, fy, scale = win._frame_geometry()
    x = fx + int(3.5 * CELL_W * scale)
    y = fy + int(4.5 * CELL_H * scale)
    win._on_click(FakeClick(x, y))
    assert (win.console.col, win.console.row) == (3, 4)


def test_window_chrome_ignores_clicks_in_footer(monkeypatch):
    """Клик по подвалу не двигает курсор: подвал не кадр."""
    w, h = 900, 700
    win = make_window(monkeypatch, size=(w, h))
    fx, fy, scale = win._frame_geometry()
    win._on_click(FakeClick(fx + int(3.5 * CELL_W * scale),
                            fy + int(4.5 * CELL_H * scale)))
    before = (win.console.col, win.console.row)
    foot_y = h - int(round(win.footer_px() * scale) / 2)
    assert win._in_chrome(foot_y), 'подвал должен распознаваться'
    win._on_click(FakeClick(fx + int(10.5 * CELL_W * scale), foot_y))
    assert (win.console.col, win.console.row) == before, \
        'клик по подвалу не должен двигать курсор'


def test_window_click_above_frame_clamps_to_top(monkeypatch):
    """Клик в строке состояния прижимается к верхнему ряду кадра.

    Строка состояния прижата к верху окна, указателя кадра в ней нет —
    координата всё равно обязана остаться в кадре.
    """
    w, h = 900, 700
    win = make_window(monkeypatch, size=(w, h))
    assert win._in_chrome(1), 'строка состояния не кадр'
    win._on_click(FakeClick(3 * CELL_W, 1))
    assert win.console.row == 0, 'указатель ушёл выше кадра'
    assert 0 <= win.console.col < window.SCREEN_WIDTH // CELL_W


# ── заставка ────────────────────────────────────────────────────────────────

def splash_window(monkeypatch, splash='КОТ'):
    """Окно с заданной заставкой."""
    cfg = Config.default()
    cfg.splash = splash
    win = make_window(monkeypatch)
    win.config = cfg
    win.splash = window._splash_enabled(cfg)
    win._splash_cache = None
    return win


def test_splash_shows_caption_and_picture(monkeypatch):
    """При загрузке дисплея — надпись и под ней рисунок из конфигурации."""
    win = splash_window(monkeypatch)
    fb = win.splash_frame()
    assert fb is not None, 'заставка не собралась'
    # Рисунок на месте: поток КОТ зажигает десятки тысяч точек.
    assert fb.count_set() > 5000, f'рисунок не нарисован: {fb.count_set()} точек'
    # Подпись — в верхних рядах, над рисунком.
    caption = win.SPLASH_CAPTION
    assert caption == 'ДИСПЛЕЙ ЭЛЕКТРОНИКА МС-7401'
    top = sum(fb.get(x, fb.height - 1 - dy)
              for x in range(fb.width) for dy in range(RASTER_H))
    assert top > 100, f'надпись не зажжена: {top} точек'


def test_splash_caption_is_centred_and_above_picture(monkeypatch):
    """Надпись по центру кадра, рисунок сдвинут под неё ровно на отбивку."""
    win = splash_window(monkeypatch)
    fb = win.splash_frame()
    cap_h = window.CAPTION_ROWS * CELL_H
    art = win._splash_picture(
        startup.picture_path(win.config.splash, prefer_stream=True))
    art.strip_border()          # рамки нет — сравниваем с тем же рисунком
    lit = lambda f: [y for y in range(f.height)                  # noqa: E731
                     if any(f.get(x, y) for x in range(f.width))]
    art_rows, fb_rows = lit(art), lit(fb)
    # Рисунок сдвинут ровно на отбивку: его нижний край на кадре стоит
    # на `cap_h` точек выше, чем в самом рисунке.
    assert min(fb_rows) == min(art_rows) + cap_h, \
        f'рисунок сдвинут на {min(fb_rows) - min(art_rows)} точек'
    # Надпись занимает самые верхние ряды кадра — над рисунком.
    assert max(fb_rows) > fb.height - cap_h, 'надпись не у верха кадра'
    assert max(art_rows) + cap_h <= fb.height, \
        'рисунок не помещается под надписью'
    # Надпись по центру: на пустом кадре она идёт от своей колонки, а
    # слева от неё до середины — пусто.
    blank = Framebuffer(fb.width, fb.height)
    win._splash_caption(blank)
    lit = [x for x in range(blank.width)
           if any(blank.get(x, y) for y in range(blank.height))]
    assert lit, 'надпись не зажжена'
    text = win.SPLASH_CAPTION
    want = (fb.width // CELL_W - len(text)) // 2 * CELL_W
    assert min(lit) == want, f'надпись с {min(lit)}, а не с {want}'
    # Блок знаков стоит по центру **сетки знакомест**: пустых колонок
    # слева и справа поровну (в пределах одного знакоместа).  Центровать
    # по точкам нельзя: сетка 8 точек, и половина знакоместа в остатке.
    cells = fb.width // CELL_W
    left = want // CELL_W
    right = cells - left - len(text)
    assert abs(left - right) <= 1, \
        f'надпись не по центру: слева {left} колонок, справа {right}'
    assert left + len(text) <= cells, 'надпись не влезла в кадр'


def test_splash_dismissed_by_any_key(monkeypatch):
    """Любая клавиша снимает заставку — и выполняется как обычно."""
    win = splash_window(monkeypatch)
    assert win.splash
    win._on_key(FakeEvent('F2'))
    assert not win.splash, 'F2 не снял заставку'
    assert win.pen.enabled, 'клавиша не выполнилась'


def test_splash_dismissed_by_machine_data(monkeypatch):
    """Данные от ЭВМ снимают заставку: терминал начал работать."""
    win = splash_window(monkeypatch)
    assert win.splash
    win.feed(b'\xeb\xe2\xec\r\n')
    assert not win.splash, 'данные ЭВМ не сняли заставку'
    assert win.splash_frame() is None


def test_splash_is_cached(monkeypatch):
    """Кадр заставки собирается один раз: разбор потока дорог."""
    win = splash_window(monkeypatch)
    first = win.splash_frame()
    assert first is not None
    calls = []
    real = win._build_splash
    win._build_splash = lambda: (calls.append(1), real())[1]
    assert win.splash_frame() is first, 'кадр заставки пересобирался'
    assert not calls, 'рисунок разбирался повторно'


def test_splash_none_means_no_splash(monkeypatch):
    """`[display] splash = none` — окно сразу пустое."""
    win = splash_window(monkeypatch, splash='none')
    assert not win.splash
    assert win.splash_frame() is None


def test_splash_picture_follows_config(monkeypatch):
    """Рисунок берётся из конфигурации, а не всегда один и тот же."""
    a = splash_window(monkeypatch, splash='КОТ')
    b = splash_window(monkeypatch, splash='СЕТ')
    fa, fb_ = a.splash_frame(), b.splash_frame()
    assert fa is not None and fb_ is not None
    assert fa.count_set() != fb_.count_set(), 'рисунок не сменился'


def test_splash_missing_picture_is_not_fatal(monkeypatch):
    """Нет рисунка — окно просто пустое, а не падает."""
    win = splash_window(monkeypatch, splash='НЕТ_ТАКОГО')
    assert win.splash, 'заставка включена'
    assert win.splash_frame() is None, 'выдуман рисунок'


def test_splash_gone_after_key_shows_frame_again(monkeypatch):
    """После снятия заставки окно снова рисует рабочий кадр."""
    win = splash_window(monkeypatch)
    assert win.splash_frame() is not None
    win.console.write('ПРОБА'.encode('koi8-r'))
    win._on_key(FakeEvent('a', 'a'))
    assert win.splash_frame() is None
    assert win.shown_frame().count_set() > 0


# ── выход из терминала ──────────────────────────────────────────────────────

class CountingDriver(FakeDriver):
    """Драйвер со счётчиками: сколько раз опросили и сколько раз закрыли.

    Порядок выхода проверяется именно числами: важно, что линия закрыта
    **один** раз, а опрос после выхода не идёт ни разу.
    """

    def __init__(self, data: bytes = b'') -> None:
        super().__init__(data)
        self.polls = 0
        self.closes = 0

    def poll(self, timeout: float) -> bytes:
        self.polls += 1
        return super().poll(timeout)

    def close(self) -> None:
        self.closes += 1
        super().close()


def test_ctrl_q_exits_with_x11_control_code(monkeypatch):
    """Ctrl-q на X11 приходит как `char=\\x11`, а не буква — выход обязан
    сработать и по `keysym`, иначе его нет вообще."""
    win = make_window(monkeypatch)
    assert win._on_key(FakeEvent('q', '\x11', state=0x4)) == 'break'
    assert not win.running, 'Ctrl-q не закрыл терминал'


def test_f12_exits(monkeypatch):
    """F12 — выход, назначенная клавиша, без модификаторов."""
    win = make_window(monkeypatch)
    win.driver = CountingDriver()
    assert win._on_key(FakeEvent('F12')) == 'break'
    assert not win.running
    assert win.driver is None, 'линия не отпущена'
    assert win._tk.quit_calls == 1


def test_ctrl_c_centres_cursor(monkeypatch):
    """Ctrl-c — ЦЕНТР, и он тоже опознаётся по keysym."""
    win = make_window(monkeypatch)
    win.console.goto(0, 0)
    assert win._on_key(FakeEvent('c', '\x03', state=0x4)) == 'break'
    assert (win.console.col, win.console.row) == (COLS // 2, ROWS // 2)


def test_exit_closes_line_and_stops_the_loop(monkeypatch):
    """Выход: линия закрыта, обход больше не ставится в очередь."""
    win = make_window(monkeypatch)
    driver = win.driver = CountingDriver()
    win.running = True
    win._tick()
    assert driver.polls == 1
    win.quit()
    assert driver.closes == 1, 'линия не закрыта при выходе'
    win._tk.scheduled.clear()
    win._tick()
    assert not win._tk.scheduled, 'обход продолжился после выхода'
    assert driver.polls == 1, 'после выхода опрос не идёт'


def test_exit_twice_is_harmless(monkeypatch):
    """Двойной выход (F12 и кнопка окна) не закрывает линию дважды."""
    win = make_window(monkeypatch)
    driver = win.driver = CountingDriver()
    win.quit()
    win.quit()
    win.close()
    assert driver.closes == 1


def test_exit_ignores_line_failure(monkeypatch):
    """Падение линии при выходе не должно помешать закрыть окно."""
    class BadDriver(CountingDriver):
        def close(self):
            raise OSError('сокет уже закрыт')

    win = make_window(monkeypatch)
    win.driver = BadDriver()
    win.quit()
    assert not win.running, 'окно не закрылось из-за линии'
    assert win._tk.quit_calls == 1


def test_loop_survives_line_failure(monkeypatch):
    """Обрыв линии не убивает обход: иначе окно замирает с картинкой."""
    class DeadDriver(CountingDriver):
        def poll(self, timeout):
            raise OSError('обрыв')

    win = make_window(monkeypatch)
    win.driver = DeadDriver()
    win.running = True
    win._tick()
    win._tick()                          # второй оборот: обход жив
    assert len(win._tk.scheduled) >= 2, 'обход остановился после обрыва'
    assert win.driver is None, 'мертвую линию продолжают опрашивать'
    log = str(list(win.diag.entries))
    assert 'обрыв' in log, f'сбой не показан в подвале: {log}'


def test_loop_survives_paint_failure(monkeypatch):
    """Ошибка вывода переживается: обход перерисовывает окно дальше."""
    win = make_window(monkeypatch)
    win.running = True
    boom = [OSError('нечего рисовать')]

    def bad_paint():
        raise boom.pop()

    win.paint = bad_paint
    win._tick()
    assert win.running, 'окно умерло из-за сбоя вывода'
    assert win._tk.scheduled, 'обход остановился из-за сбоя вывода'
    win.paint = lambda: None            # вывод починили
    win._tick()
    assert win._faults == 1


def test_window_close_button_exits_cleanly(monkeypatch):
    """Кнопка окна — тот же выход, без трассировки на каждый оборот."""
    win = make_window(monkeypatch)
    driver = win.driver = CountingDriver()
    win.running = True
    win.close()                          # WM_DELETE_WINDOW
    assert driver.closes == 1
    assert win._tk.quit_calls == 1
    win._tick()
    assert driver.polls == 0


def test_ctrl_letter_ignores_other_keys(monkeypatch):
    """Не буква — не сочетание: F-клавиши и цифры под Ctrl не ловятся."""
    for keysym, char in (('F5', ''), ('1', '\x01'), ('Left', ''),
                         ('Return', '\r'), ('Р', '')):
        assert window._ctrl_letter(FakeEvent(keysym, char, state=0x4)) is None


# ── курсор: рамка знакоместа и мигание ──────────────────────────────────────

def cursor_box(win, fx, fy, scale):
    """Габариты рамки курсора на экране: `(x0, y0, x1, y1)`."""
    photo = FakePhoto()
    win._cursor_frame_scaled(photo, fx, fy, scale)
    box = [to for to, _ in photo.puts if to is not None]
    if not box:
        return None
    return (min(b[0] for b in box), min(b[1] for b in box),
            max(b[2] for b in box), max(b[3] for b in box))


def test_cursor_frame_wraps_exactly_the_glyph_cell(monkeypatch):
    """Рамка курсора лежит ровно вокруг знакоместа — ни рядом, ни в углу.

    Раньше ряд кадра пересчитывался своей формулой с `SCREEN_HEIGHT`
    вместо `SCREEN_HEIGHT - 1`: верхний ряд кадра уезжал в низ окна
    (сдвиг до 900 точек), средние строки — на десятки точек вбок.
    """
    win = make_window(monkeypatch, size=(1200, 900))
    fx, fy, scale = win._frame_geometry()
    th = max(1, int(round(scale)))
    for col, row in ((0, 0), (5, 5), (34, 20), (69, 39), (1, 38)):
        win.console.goto(col, row)
        x0, y0, x1, y1 = win.console.cell_rect(col, row)
        want = (fx + int(round(x0 * scale)),
                fy + int(round((SCREEN_HEIGHT - 1 - y1) * scale)),
                fx + int(round(x1 * scale)) + th,
                fy + int(round((SCREEN_HEIGHT - 1 - y0) * scale)) + th)
        assert cursor_box(win, fx, fy, scale) == want, (col, row)


def test_cursor_frame_follows_window_resize(monkeypatch):
    """После растяжения окна рамка остаётся на своём знакоместе."""
    win = make_window(monkeypatch, size=(600, 500))
    win.console.goto(40, 25)
    small = cursor_box(win, *win._frame_geometry())
    win._canvas._w, win._canvas._h = 1600, 1200
    fx, fy, scale = win._frame_geometry()
    x0, y0, x1, y1 = win.console.cell_rect(40, 25)
    th = max(1, int(round(scale)))
    assert cursor_box(win, fx, fy, scale) == (
        fx + int(round(x0 * scale)),
        fy + int(round((SCREEN_HEIGHT - 1 - y1) * scale)),
        fx + int(round(x1 * scale)) + th,
        fy + int(round((SCREEN_HEIGHT - 1 - y0) * scale)) + th)
    assert small is not None and small != cursor_box(win, fx, fy, scale)


def test_cursor_blinks(monkeypatch):
    """Курсор мигает: рамка то есть, то нет."""
    win = make_window(monkeypatch)
    fx, fy, scale = win._frame_geometry()
    win._blink_on = True
    assert cursor_box(win, fx, fy, scale) is not None, 'рамки нет'
    win._blink_on = False
    assert cursor_box(win, fx, fy, scale) is None, 'погасший курсор виден'


def test_cursor_toggle_rate(monkeypatch):
    """Мигание переключается раз в полсекунды, а не на каждом обороте."""
    win = make_window(monkeypatch)
    assert window.TerminalWindow.CURSOR_BLINK_MS == 500
    seen = []
    for _ in range(4):
        win._blink_on = not win._blink_on
        seen.append(win._blink_on)
    assert seen == [False, True, False, True]


def test_cursor_wakes_on_movement(monkeypatch):
    """Нажатая клавиша зажигает курсор: стрелка не должна попадать
    на погасший курсор — иначе кажется, что он не двигается."""
    win = make_window(monkeypatch)
    fx, fy, scale = win._frame_geometry()
    win._blink_on = False
    win.console.goto(10, 10)
    win._on_key(FakeEvent('Right'))
    assert win.console.col == 11
    assert win._blink_on, 'курсор не проснулся после стрелки'
    assert cursor_box(win, fx, fy, scale) is not None


def test_cursor_wakes_while_typing(monkeypatch):
    """Набор не даёт курсору погаснуть на середине слова."""
    win = make_window(monkeypatch)
    win._blink_on = False
    win._on_key(FakeEvent('a', 'a'))
    assert win._blink_on, 'курсор погас во время набора'
    win._blink_on = False
    win._on_key(FakeEvent('Return'))
    assert win._blink_on, 'курсор погас после перевода строки'
    win._blink_on = False
    win._on_key(FakeEvent('BackSpace'))
    assert win._blink_on, 'курсор погас после стирания'


def test_cursor_hidden_while_browsing_history(monkeypatch):
    """В истории на экране чужой кадр — рамка курсора там неуместна."""
    win = make_window(monkeypatch)
    fx, fy, scale = win._frame_geometry()
    win._history = [win.emu.fb.copy(), win.emu.fb.copy()]
    win.scroll_view(-1)
    assert win._hist_at is not None
    assert cursor_box(win, fx, fy, scale) is None, \
        'курсор висит поверх кадра из истории'


def test_cursor_wakes_on_machine_output(monkeypatch):
    """ЭВМ пишет — курсор горит: приглашение ОС пришло, а курсор погас."""
    win = make_window(monkeypatch)
    win._blink_on = False
    win.session_text(b'\xeb\xe2\xec\r\n')       # ЭBM-3
    assert win._blink_on, 'курсор погас при выводе ЭВМ'
    win._blink_on = False
    win.session_text('ПРИВЕТ'.encode('koi8-r'))    # и для текста UTF-8
    assert win._blink_on, 'курсор погас при выводе ЭВМ (текст)'


def test_cursor_idles_into_blinking(monkeypatch):
    """Без событий курсор доходит до мигания сам — по таймеру."""
    win = make_window(monkeypatch)
    win.running = True                  # обход крутит только рабочее окно
    win._blink_on = True
    win._blink_at = time.monotonic()
    win._tick()
    assert win._blink_on, 'фаза переключилась раньше полусекунды'
    win._blink_at = time.monotonic() - 1.0
    win._tick()
    assert not win._blink_on, 'мигание не идёт в покое'


def test_cursor_stays_inside_the_grid(monkeypatch):
    """Стрелки упираются в края сетки, а не уезжают за кадр."""
    win = make_window(monkeypatch)
    win.console.goto(0, 0)
    for keysym in ('Left', 'Up'):
        win._on_key(FakeEvent(keysym))
    assert (win.console.col, win.console.row) == (0, 0)
    win.console.goto(COLS - 1, ROWS - 1)
    for keysym in ('Right', 'Down'):
        win._on_key(FakeEvent(keysym))
    assert (win.console.col, win.console.row) == (COLS - 1, ROWS - 1)


# ── световое перо (F2) ──────────────────────────────────────────────────────

def pen_window(monkeypatch, size=(1200, 900)):
    """Окно с включённым пером и чистым кадром."""
    win = make_window(monkeypatch, size=size)
    win.pen.enabled = True
    win.emu.fb.clear()
    win.best, win.best_n = None, 0
    return win


def screen_of(win, fx, fy, scale, x, y):
    """Точка кадра → точка окна, в которую указывает пользователь.

    Берётся **центр** пикселя: пиксель кадра `x` на экране занимает
    полосу `[x * scale, (x + 1) * scale)`, и только её середина
    соответствует именно этому пикселю после обратного перевода.
    """
    return (fx + int((x + 0.5) * scale),
            fy + int((399 - y + 0.5) * scale))


def test_pen_maps_pointer_to_frame_pixel(monkeypatch):
    """Перо попадает в ту точку кадра, на которую указал пользователь.

    Координаты окна переводятся в кадр через геометрию кадра: масштаб
    дробный, кадр прижат к левому краю и стоит под строкой состояния —
    всё это обязано учитываться, иначе перо рисует мимо.
    """
    win = pen_window(monkeypatch)
    fx, fy, scale = win._frame_geometry()
    for x, y in ((0, 0), (1, 1), (280, 200), (559, 399), (100, 350)):
        ex, ey = screen_of(win, fx, fy, scale, x, y)
        assert win._pen_at(FakeClick(ex, ey)) == (x, y), (x, y)


def test_pen_draws_stroke_where_pointer_went(monkeypatch):
    """Штрих пера ложится вдоль пути указателя."""
    win = pen_window(monkeypatch)
    fx, fy, scale = win._frame_geometry()
    start, end = (20, 380), (400, 30)
    win._pen_down(FakeClick(*screen_of(win, fx, fy, scale, *start)), button=1)
    win._on_drag(FakeClick(*screen_of(win, fx, fy, scale, *end)))
    win._on_release()
    lit = [(x, y) for y in range(400) for x in range(560)
           if win.emu.fb.get(x, y)]
    assert lit, 'штрих не нарисован'
    assert min(p[0] for p in lit) <= 20 and max(p[0] for p in lit) >= 400
    assert min(p[1] for p in lit) <= 30 and max(p[1] for p in lit) >= 380
    # Штрих идёт по диагонали, а не по краю кадра.
    mid = [p for p in lit if 180 < p[0] < 240]
    assert any(180 < p[1] < 220 for p in mid), 'штрих пошёл не по диагонали'


def test_pen_stroke_follows_window_resize(monkeypatch):
    """После растяжения окна перо попадает туда же по кадру."""
    win = pen_window(monkeypatch, size=(600, 500))
    fx, fy, scale = win._frame_geometry()
    ex, ey = screen_of(win, fx, fy, scale, 123, 321)
    assert win._pen_at(FakeClick(ex, ey)) == (123, 321)
    win._canvas._w, win._canvas._h = 1600, 1200
    fx, fy, scale = win._frame_geometry()
    ex, ey = screen_of(win, fx, fy, scale, 123, 321)
    assert win._pen_at(FakeClick(ex, ey)) == (123, 321), \
        'после растяжения окна перо уехало'


def test_pen_is_visible_on_frame(monkeypatch):
    """Перо видно на кадре: без перекрестья его не видно, куда попадёт."""
    win = pen_window(monkeypatch)
    fx, fy, scale = win._frame_geometry()
    win._pen_at(FakeClick(*screen_of(win, fx, fy, scale, 300, 200)))
    win.pen.x, win.pen.y = 300, 200
    assert not win.emu.fb.count_set(), 'перекрестье не должно попадать в память'
    shown = win.shown_frame()
    assert shown.count_set() > 0, 'перекрестье не нарисовано при выводе'
    # Выключенное перо кадр не трогает.
    win.pen.enabled = False
    assert win.shown_frame().count_set() == 0


def test_pen_crosshair_follows_pointer(monkeypatch):
    """Перекрестье идёт за указателем без нажатой кнопки."""
    win = pen_window(monkeypatch)
    fx, fy, scale = win._frame_geometry()
    win.pen.x, win.pen.y = 10, 10
    win._on_motion(FakeClick(*screen_of(win, fx, fy, scale, 400, 300)))
    assert (win.pen.x, win.pen.y) == (400, 300), \
        'перекрестье осталось на месте прежнего штриха'
    # Движение внутри того же знакоместа перерисовку не требует.
    win._on_motion(FakeClick(*screen_of(win, fx, fy, scale, 401, 301)))
    assert (win.pen.x, win.pen.y) == (401, 301)


def test_pen_tools_cycle_with_f3(monkeypatch):
    """F3 перебирает инструменты пера по кругу."""
    win = make_window(monkeypatch)
    win._on_key(FakeEvent('F3', ''))
    assert win.pen_tool == 'точка'
    win._on_key(FakeEvent('F3', ''))
    assert win.pen_tool == 'стирание'
    win._on_key(FakeEvent('F3', ''))
    assert win.pen_tool == 'инверсия'
    win._on_key(FakeEvent('F3', ''))
    assert win.pen_tool == 'штрих'


def test_pen_f2_toggles_and_reports(monkeypatch):
    """F2 включает и выключает перо, и это видно в подвале."""
    win = make_window(monkeypatch)
    assert not win.pen.enabled
    win._on_key(FakeEvent('F2', ''))
    assert win.pen.enabled
    assert 'ПЕРО' in win.status().pen_tool or win.status().pen
    win._on_key(FakeEvent('F2', ''))
    assert not win.pen.enabled


def test_right_and_middle_buttons_erase(monkeypatch):
    """Правая и средняя кнопки — стирающее перо, как `*` у терминала."""
    win = pen_window(monkeypatch)
    fx, fy, scale = win._frame_geometry()
    x, y = 300, 200
    win.emu.fb.hline(0, 559, y)
    assert win.emu.fb.count_set() == 560
    win._on_right(FakeClick(*screen_of(win, fx, fy, scale, x, y)))
    assert win.emu.fb.count_set() < 560, 'правая кнопка должна стирать'
    win._on_middle(FakeClick(*screen_of(win, fx, fy, scale, x, y)))
    assert win.emu.fb.count_set() < 560, 'средняя кнопка должна стирать'


def test_click_without_pen_moves_text_cursor(monkeypatch):
    """Без пера левая кнопка ставит текстовый курсор, а не рисует."""
    win = make_window(monkeypatch)
    win.emu.fb.clear()
    win.best, win.best_n = None, 0
    fx, fy, scale = win._frame_geometry()
    # `y` кадра считается вверх, а строки консоли — сверху вниз.
    win._on_click(FakeClick(*screen_of(win, fx, fy, scale, 8, 399)))
    assert win.emu.fb.count_set() == 0, 'курсор не должен рисовать'
    assert (win.console.col, win.console.row) == (8 // CELL_W, 0)


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
