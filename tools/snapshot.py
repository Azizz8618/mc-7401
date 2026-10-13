#!/usr/bin/env python3
"""
snapshot.py — снимок экрана терминала для разбора.

    python3 tools/snapshot.py --link 127.0.0.1:4203 --send краб --wait 3
    python3 tools/snapshot.py --link 127.0.0.1:4203 --keys --out снимки
    python3 tools/snapshot.py --stream golden/streams/КОТ.bin --out снимки
    python3 tools/snapshot.py снимки/снимок-1          # разбор снимка

Снимок снимается **без окна**: терминал собирается из тех же кирпичиков,
что и окно (`TerminalWindow`), но вместо `PhotoImage` кадр и служебные
полосы складываются в один буфер и уходят на диск.  Это и даёт разбору
то, чего не видно глазами, и то, что не нужно живую линию:

* `экран.png`   — экран целиком: кадр ЭВМ, строка состояния и подвал;
* `экран.txt`   — текст рядов с линейкой знакомест и отметкой пустых;
* `кадр.bin`    — биты кадра 560×400 (70 байт на ряд, сверху вниз) —
                 ровно то, что лежит в памяти терминала;
* `линия.bin`   — сырые байты, пришедшие от линии, как есть;
* `байты.txt`   — журнал: каждый байт линии, что он сделал и **в какой
                 знакомест попал**.  Именно по нему видно задвоение букв
                 (байт один, а знаков на экране два) и пропавший знак;
* `снимок.json` — состояние терминала (история, курсор, набор) и разбор.

Живой режим (`--keys`) читает клавиатуру как есть, без `Enter` на каждый
знак: `Ctrl-\\` снимает снимок, `Ctrl-q` выходит.  Всё, что не `Ctrl-\\`
и не `Ctrl-q`, уходит на линию — можно набрать команду ОС и снять экран
с её ответом.

Разбор (`снимок` вместо источника) печатает:

* текст рядов с линейкой;
* **пустые ряды между непустыми** — так виден лишний отступ между
  строками выдачи (например каталога КТЛ);
* **удвоенные знаки** — соседние одинаковые буквы и повторы слов;
* журнал байтов последних строк линии.

    $ python3 tools/snapshot.py снимки/снимок-1 | head -40
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mc7401 import __version__, chrome                        # noqa: E402
from mc7401.backends import PNGBackend                        # noqa: E402
from mc7401.chars import display_dks                          # noqa: E402
from mc7401.config import Config                              # noqa: E402
from mc7401.console import (CELL_H, COLS, CR, ETX, LF, ROWS,      # noqa: E402
                            Console, read_row)
from mc7401.constants import SCREEN_HEIGHT, SCREEN_WIDTH      # noqa: E402
from mc7401.framebuffer import Framebuffer                    # noqa: E402
from mc7401.history import History                            # noqa: E402
from mc7401.undo import Undo                                  # noqa: E402

#: Ширина линейки знакомест над текстом рядов.
RULER_TICK = 10


# ── консоль с журналом ──────────────────────────────────────────────────────

class TraceConsole(Console):
    """Консоль, которая помнит, что сделал каждый байт линии.

    Обычная `Console.write` перебирает байты молча, и по кадру нельзя
    понять, откуда взялся знак: байт был один, а на экране их два —
    или байт был, а знака нет.  Здесь каждый байт записывается **до**
    обработки (знакоместо, где он встанет) и **после** (что изменилось),
    поэтому журнал отвечает на вопрос «почему на экране именно это»
    без обращения к живой линии.
    """

    def __init__(self, fb: Framebuffer) -> None:
        super().__init__(fb)
        #: Журнал: по записи на байт — словарь полей ниже.
        self.log: list[dict] = []
        #: Сколько записей хранить (кольцо): журнал растёт вместе с
        #: сеансом, а на разбор нужны последние.
        self.log_limit = 20000

    def write(self, data: bytes) -> None:                       # noqa: D102
        for b in data:
            row, col, points = self.row, self.col, self.fb.count_set()
            super().write(bytes((b,)))
            self.log.append({
                'b': b,
                'code': display_dks(b) if b >= 0x20 else '',
                'row': row,
                'col': col,
                'new_row': self.row,
                'new_col': self.col,
                'points': self.fb.count_set() - points,
            })
            if len(self.log) > self.log_limit:
                del self.log[:len(self.log) // 2]

    # ── разбор журнала ──────────────────────────────────────────────────────

    def tail_text(self, count: int = 40) -> list[str]:
        """Журнал последних записей как строки «байт → что сделал»."""
        out = []
        for rec in self.log[-count:]:
            b, code = rec['b'], rec['code']
            if b in (CR, LF):
                what = 'перевод строки'
            elif b == ETX:
                what = 'конец текста'
            elif b < 0x20:
                what = 'управляющий'
            elif code:
                what = f'знак «{code}»'
            else:
                what = 'без знака'
            moved = ''
            if (rec['row'], rec['col']) != (rec['new_row'], rec['new_col']):
                moved = (f' → курсор {rec["new_row"]}:{rec["new_col"]}')
            out.append(f'0x{b:02X} {chr(b) if 0x20 <= b < 0x7F else "."} '
                       f'@ {rec["row"]}:{rec["col"]:<2} {what}{moved}')
        return out

    def duplicated(self, count: int = 400) -> list[str]:
        """Байты, которые на экране дали два знака подряд.

        Каждый байт линии должен дать ровно один знак в своём знакоместе.
        Если в одном знакоместе оказалось два байта — на экране задвоение;
        если знакоместо затёрто (`points == 0` у одного из них) — знак
        пропал или его затерли.  Журнал разбирается здесь же, без кадра.
        """
        seen: dict[tuple[int, int], int] = {}
        out = []
        for rec in self.log[-count:]:
            if not rec['code'] or rec['points'] < 0:
                continue
            key = (rec['row'], rec['col'])
            prev = seen.get(key)
            if prev is not None:
                out.append(f'знакомест {key[0]}:{key[1]}: байты '
                           f'0x{prev:02X} и 0x{rec["b"]:02X} → '
                           f'«{display_dks(prev)}» и «{rec["code"]}»')
            seen[key] = rec['b']
        return out


# ── сборка терминала без окна ───────────────────────────────────────────────

def build(config: Config, driver=None) -> object:
    """Терминал как в окне, но без Tk.

    `TerminalWindow` не трогает `tkinter`, пока не вызвана `paint`, так
    что снимок снимается на машине без дисплея: тот же путь данных
    (`feed`), тот же кадр, та же история, те же служебные полосы.
    """
    from mc7401.window import TerminalWindow
    win = TerminalWindow(driver, config=config)
    console = TraceConsole(win.emu.fb)
    console.history = History(limit=config.history)
    console.charset = getattr(config, 'charset', 'n2')
    win.console = console
    win.hist = console.history
    win.undo = Undo(limit=config.undo)
    win.dismiss_splash()
    # `paint()` тянет `tkinter` и рисует окно, а снимку нужен только
    # результат — состояние и кадр.  Набор, вывод ЭВМ и прокрутка вызывают
    # `paint()` на каждом шаге, поэтому здесь она гасится: иначе снимок
    # требовал бы дисплея.  Само состояние (`_dirty`, кадр, подвал)
    # `paint` не меняет — он только рисует.
    win.paint = lambda: setattr(win, '_dirty', True)
    return win


def screen_framebuffer(win) -> Framebuffer:
    """Экран целиком: строка состояния, кадр ЭВМ и подвал.

    Кадр ровно 560×400, служебные полосы — тем же знаком ПЗУ, что и в
    окне: снимок должен совпадать с тем, что видит оператор, иначе он
    бесполезен для разбора.
    """
    top_rows, bottom_rows = win.chrome_parts()
    top_px = chrome.top_height(top_rows)
    foot_px = len(bottom_rows) * chrome.ROW_H
    fb = Framebuffer(SCREEN_WIDTH, top_px + SCREEN_HEIGHT + foot_px)
    chrome.render(fb, top_rows, bottom_rows, frame_height=SCREEN_HEIGHT)
    shown = win.shown_frame()
    if shown is not None:
        for y in range(SCREEN_HEIGHT):
            fb.put_row_bytes(foot_px + y, shown.row_bytes(y))
    return fb


def frame_text(fb: Framebuffer) -> list[str]:
    """Ряды кадра как текст — ровно как их видит оператор."""
    out = []
    for row in range(ROWS):
        y0 = (ROWS - 1 - row) * CELL_H
        out.append(read_row(fb, y0))
    return out


def ruler() -> str:
    """Линейка знакомест: десятки и единицы по краю."""
    tens = ''.join(str((c // 10) % 10) if c % 10 == 0 else ' ' for c in range(COLS))
    ones = ''.join(str(c % 10) for c in range(COLS))
    return tens + '\n' + ones


def chrome_text(win) -> list[str]:
    """Служебные полосы как текст — подвал и строка состояния.

    Ряд — список ячеек, ячейка — пары «текст, стиль»; собираем их тем
    же знаком, каким они и рисуются, а черту помечаем словом, иначе в
    разборе не отличить её от пустого ряда.
    """
    from mc7401.panel import RULE, cell_text
    top_rows, bottom_rows = win.chrome_parts()
    out = []
    for name, rows in (('строка состояния', top_rows), ('подвал', bottom_rows)):
        out.append(f'--- {name} ---')
        for row in rows:
            if row == [RULE]:
                out.append('─' * COLS)
                continue
            out.append(' | '.join(cell_text(cell) for cell in row))
    return out


# ── разбор экрана ───────────────────────────────────────────────────────────

def find_gaps(rows: list[str]) -> list[str]:
    """Пустые ряды между непустыми — «лишний отступ» в выдаче."""
    busy = [bool(r.strip()) for r in rows]
    gaps = []
    for n in range(1, len(rows) - 1):
        if not busy[n] and busy[n - 1] and busy[n + 1]:
            gaps.append(f'пустой ряд {n} между {n - 1} и {n + 1}')
    return gaps


def find_doubles(rows: list[str]) -> list[str]:
    """Задвоение знаков: одинаковые буквы подряд и повтор слов."""
    out = []
    for n, row in enumerate(rows):
        squeezed = re.sub(r'\s+', ' ', row)
        for m in re.finditer(r'(\S)\1', squeezed):
            # Одна и та же буква дважды подряд — норма только для `ОО`
            # и подобных, но в выдаче ОС её не бывает.
            out.append(f'ряд {n}: подряд «{m.group(0)}»')
        words = squeezed.split(' ')
        for a, b in zip(words, words[1:]):
            if a and a == b:
                out.append(f'ряд {n}: слово «{a}» повторено дважды')
    return out


def analyse_screen(rows: list[str]) -> dict:
    """Разбор экрана: пустые ряды, удвоения, длины строк."""
    lens = [len(r.rstrip()) for r in rows]
    wide = [f'ряд {n}: {ln} знаков (сетка {COLS})'
            for n, ln in enumerate(lens) if ln > COLS]
    return {
        'rows': rows,
        'lengths': lens,
        'empty_rows': [n for n, r in enumerate(rows) if not r.strip()],
        'gaps': find_gaps(rows),
        'doubles': find_doubles(rows),
        'too_wide': wide,
    }


# ── снимок ──────────────────────────────────────────────────────────────────

def take(win, path: Path, raw: bytearray, *, scale: int = 2,
         tag: str = '') -> Path:
    """Снять и записать снимок в каталог `path`."""
    path.mkdir(parents=True, exist_ok=True)
    fb = screen_framebuffer(win)
    shown = win.shown_frame()
    # Текст — по самому кадру: в собранном экране ряды подвала и строки
    # состоянии попали бы в нумерацию кадра и сбили бы разбор.
    rows = frame_text(shown) if shown is not None else [''] * ROWS
    PNGBackend(fb, scale=scale).save(path / 'экран.png')

    if shown is not None:
        stride = SCREEN_WIDTH // 8
        data = b''.join(shown.row_bytes(SCREEN_HEIGHT - 1 - y)
                        for y in range(SCREEN_HEIGHT))
        assert len(data) == stride * SCREEN_HEIGHT
        (path / 'кадр.bin').write_bytes(data)
    (path / 'линия.bin').write_bytes(bytes(raw))

    lines = ['снимок экрана МС 7401',
             f'время {time.strftime("%Y-%m-%d %H:%M:%S")}',
             f'метка {tag or "—"}',
             '', '--- кадр ЭВМ ---',
             '    ' + ruler()]
    for n, row in enumerate(rows):
        mark = '' if row.strip() else '   ← пустой'
        lines.append(f'{n:>3} {row.rstrip()}{mark}')
    lines += ['', '--- служебные строки ---'] + chrome_text(win)
    lines += ['', '--- разбор ---']
    report = analyse_screen(rows)
    lines += report['gaps'] or ['пустых рядов между строками нет']
    lines += report['doubles'] or ['задвоенных знаков не найдено']
    lines += report['too_wide'] or [f'строки короче сетки ({COLS} знаков)']
    tail = getattr(win.console, 'tail_text', lambda n=0: [])()
    if tail:
        lines += ['', '--- байты линии (последние) ---'] + tail
    dups = getattr(win.console, 'duplicated', lambda n=0: [])()
    if dups:
        lines += ['', '--- два байта в одном знакоместе ---'] + dups
    (path / 'экран.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    (path / 'байты.txt').write_text('\n'.join(tail) + '\n', encoding='utf-8')

    meta = {
        'version': __version__,
        'time': time.strftime('%Y-%m-%d %H:%M:%S'),
        'tag': tag,
        'cursor': {'row': win.console.row, 'col': win.console.col},
        'history': {'size': win.hist.size, 'position': win.hist.position,
                    'dropped': win.hist.dropped, 'limit': win.hist.limit},
        'undo': len(win.undo),
        'buffer': len(win.session.buffer),
        'mode': win.session.mode,
        'points': shown.count_set() if shown else 0,
        'line_bytes': len(raw),
        'ruler': ruler(),
        'report': report,
    }
    (path / 'снимок.json').write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
    return path


class Key:
    """Событие клавиатуры для `TerminalWindow._on_key`.

    Окно принимает любое событие с `keysym` и `char` — от настоящего Tk
    отличается только тип.  Набор через `_on_key` (а не прямо в линию)
    принципиален: только так видно и локальное эхо, и эхо ЭВМ, а вместе
    с ними — задвоение набора.
    """

    def __init__(self, keysym: str, char: str = ''):
        self.keysym = keysym
        self.char = char


KEYSYM = {' ': 'space', '\n': 'Return', '\r': 'Return', '\x08': 'BackSpace',
          '\x1c': 'F14', '\x11': 'F13'}


# ── источники ───────────────────────────────────────────────────────────────

class LineSource:
    """Живая линия ДКС: набрать текст, подождать, снять снимок."""

    def __init__(self, endpoint: str, *, prompt: bool = False):
        from mc7401.tui import LiveDriver
        self.driver = LiveDriver(endpoint, prompt=prompt)
        self.raw = bytearray()

    def pump(self, win, seconds: float) -> None:
        """Читать линию `seconds` и отдавать в терминал."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            data = self.driver.poll(0.2)
            if data:
                self.raw.extend(data)
                win.feed(data)

    def type_keys(self, win, text: str, *, pause: float = 0.25,
                  enter: bool = True) -> None:
        """Набрать `text` клавишами окна — как это делает оператор.

        `\n` внутри текста — клавиша ПРПС.  По умолчанию (`enter=True`)
        она жмётся в конце строки: без неё ОС не выполняет команду и на
        экране нечего разбирать.
        """
        for ch in text:
            if ch == '\n' and not enter:
                break
            win._on_key(Key(KEYSYM.get(ch, ch), '\r' if ch == '\n' else ch))
            self.pump(win, pause)
        if enter and '\n' not in text:
            win._on_key(Key('Return', '\r'))
            self.pump(win, pause)

    def send(self, text: str) -> None:
        """Напечатать строку на линии — тем же путём, что и окно.

        `LiveDriver.send_keys` ждёт строку и кодирует её в КОИ-8Р сам,
        а знаки набора Н2 лежат в диапазоне 0x40…0x7E, где КОИ-8Р
        совпадает с КОИ-7: так передаётся ровно то, что нажал оператор.
        """
        from mc7401.chars import encode_input
        wire = encode_input(text, set_name='n2') + b'\r'
        self.driver.send_keys(wire.decode('latin-1'))

    def keys(self, win, out: Path, *, scale: int) -> None:
        """Клавиатура как есть: `Ctrl-\\` — снимок, `Ctrl-q` — выход."""
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        n = 0
        try:
            tty.setraw(fd)
            print(f'набор идёт на линию, снимок — Ctrl-\\ (0x1C), выход — Ctrl-q; '
                  f'снимки: {out}', file=sys.stderr)
            while True:
                ch = os.read(fd, 1)
                if not ch:
                    break
                if ch == b'\x11':                  # Ctrl-q
                    break
                if ch == b'\x1c':                  # Ctrl-\
                    n += 1
                    self.pump(win, 0.4)
                    p = take(win, out / f'снимок-{n}', self.raw,
                             scale=scale, tag=f'Ctrl-\\{n}')
                    print(f'\nснимок: {p}', file=sys.stderr)
                    continue
                self.driver.send_keys(ch)
                self.pump(win, 0.15)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


class StreamSource:
    """Поток команд из файла: без линии и без окна."""

    def __init__(self, path: Path):
        self.data = path.read_bytes()

    def run(self, win) -> None:
        """Прогнать поток через терминал тем же путём, что и окно."""
        from mc7401.reader import EventKind
        win.reader.feed(self.data)
        for ev in win.reader.pop():
            if ev.kind == EventKind.COMMAND:
                win.emu.execute(win.emu.parser.parse(ev.data))
            else:
                win.console.write(ev.data)
        win._touch_frame()


# ── разбор готового снимка ──────────────────────────────────────────────────

def show(path: Path) -> int:
    """Напечатать снимок: экран, разбор и журнал байтов."""
    text = path / 'экран.txt'
    if not text.exists():
        print(f'в каталоге {path} нет снимка (экран.txt)', file=sys.stderr)
        return 1
    print(text.read_text(encoding='utf-8'))
    return 0


# ── командная строка ────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog='snapshot.py', description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='примеры:\n'
               '  python3 tools/snapshot.py --link 127.0.0.1:4203 '
               '--send краб --wait 3\n'
               '  python3 tools/snapshot.py --link 127.0.0.1:4203 --keys\n'
               '  python3 tools/snapshot.py --stream golden/streams/КОТ.bin\n'
               '  python3 tools/snapshot.py снимки/снимок-1')
    ap.add_argument('source', nargs='?',
                    help='каталог снимка: напечатать его разбор')
    ap.add_argument('--link', metavar='ENDPOINT',
                    help='живая линия ДКС, например 127.0.0.1:4203')
    ap.add_argument('--stream', metavar='FILE', help='поток команд из файла')
    ap.add_argument('--send', action='append', default=[], metavar='ТЕКСТ',
                    help='напечатать строку на линии (можно несколько)')
    ap.add_argument('--type', action='append', default=[], metavar='ТЕКСТ',
                    help='набрать клавишами окна, как оператор: видно и '
                         'локальное эхо, и эхо ЭВМ')
    ap.add_argument('--pause', type=float, default=0.25, metavar='SEC',
                    help='пауза между знаками при наборе (--type)')
    ap.add_argument('--wait', type=float, default=1.5, metavar='SEC',
                    help='сколько ждать ответ линии перед снимком')
    ap.add_argument('--keys', action='store_true',
                    help='клавиатура напрямую: Ctrl-\\ — снимок, Ctrl-q — выход')
    ap.add_argument('--out', default='снимки', metavar='ДИР',
                    help='каталог для снимков (по умолчанию снимки/)')
    ap.add_argument('--name', default='', help='имя каталога снимка')
    ap.add_argument('--scale', type=int, default=2,
                    help='увеличение PNG (по умолчанию 2)')
    ap.add_argument('--conf', metavar='FILE', help='файл конфигурации')
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.source:
        return show(Path(args.source))

    cfg = Config.load(args.conf) if args.conf else Config.default()
    out = Path(args.out)
    raw = bytearray()          # для потока из файла: там линии нет

    if args.stream:
        win = build(cfg)
        StreamSource(Path(args.stream)).run(win)
        path = take(win, out / (args.name or 'снимок'), raw,
                    scale=args.scale, tag=args.stream)
        print(f'снимок: {path}')
        print((path / 'экран.txt').read_text(encoding='utf-8'))
        return 0

    if not args.link:
        build_parser().print_help()
        return 2

    src = LineSource(args.link)
    win = build(cfg, src.driver)
    win.feed(src.driver.greeting or b'')
    src.pump(win, 1.0)
    for text in args.send:
        src.send(text)
        src.pump(win, args.wait)
    for text in args.type:
        src.type_keys(win, text, pause=args.pause)
        src.pump(win, args.wait)
    if args.keys:
        src.keys(win, out, scale=args.scale)
        return 0
    path = take(win, out / (args.name or 'снимок'), src.raw, scale=args.scale,
                tag=' '.join(args.send + [f'--type {t}' for t in args.type]))
    print(f'снимок: {path}')
    print((path / 'экран.txt').read_text(encoding='utf-8'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())