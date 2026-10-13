"""
cli.py — командная строка эмулятора «Электроника МС 7401».

    python -m mc7401 поток.bin -o кадр.png        # отрисовать поток
    python -m mc7401 --listen 127.0.0.1:4202      # ждать БЭСМ-6 (ДКС)
    python -m mc7401 --list-backends
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .backends import BACKENDS, PNGBackend, SVGBackend, TkBackend, PygameBackend
from .emulator import TerminalEmulator
from .reader import EventKind

log = logging.getLogger('mc7401')


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog='mc7401',
        description='Эмулятор растрового графического терминала '
                    '«Электроника МС 7401» (560×400).',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
примеры:
  python -m mc7401 golden/streams/СЕТ.bin -o сетка.png
  python -m mc7401 golden/streams/СЕТ.bin -b svg -o сетка.svg
  python -m mc7401 --listen 127.0.0.1:4202 --png кадр.png
  cat поток.bin | python -m mc7401 - --dump-ascii
''')
    ap.add_argument('input', nargs='?', default=None,
                    help="файл с потоком команд или '-' для stdin; "
                         "без файла запускается интерактивный режим")
    ap.add_argument('-b', '--backend', default='png',
                    choices=sorted(BACKENDS), help="способ вывода")
    ap.add_argument('-o', '--output', default='mc7401.png',
                    help='выходной файл (для svg/png)')
    ap.add_argument('--png', metavar='FILE',
                    help='дополнительно сохранить PNG')
    ap.add_argument('--scale', type=int, default=None,
                    help='масштаб растрового вывода; окно берёт его из '
                         'конфигурации, где по умолчанию 1 — знак ПЗУ '
                         'на экране ровно 14×16 точек')
    ap.add_argument('--xor-code', type=lambda v: int(v, 8),
                    default=None, metavar='OCT',
                    help='код команды инвертирующего пера (восьмеричный)')
    ap.add_argument('--frames', type=int, default=0, metavar='N',
                    help='сохранить N кадров анимации (pygame)')
    ap.add_argument('--fps', type=int, default=25, help='частота кадров')
    ap.add_argument('--dump-ascii', action='store_true',
                    help='вывести кадр текстом (# и .)')
    ap.add_argument('--peak', action='store_true',
                    help='показать самый «наполненный» кадр (программы '
                         'стирают экран в начале каждой картины)')
    ap.add_argument('--stats', action='store_true',
                    help='печатать статистику разбора')
    ap.add_argument('--link', metavar='HOST:PORTS',
                    help='подключиться к линии ДКС БЭСМ-6 (ЭВМ слушает '
                         'порт, терминал подключается; 4202-4223 по умолчанию)')
    ap.add_argument('--listen', metavar='HOST:PORT',
                    help='слушать порт и обслуживать ЭВМ (обратный режим)')
    ap.add_argument('--link-timeout', type=float, default=0.0,
                    help='таймаут линии ДКС, с (0 — 10 с)')
    ap.add_argument('--gui', '--window', action='store_true', dest='gui',
                    help='графическое окно: кадр 560×400 вместе с текстом '
                         '(по умолчанию без файла)')
    ap.add_argument('--tui', action='store_true',
                    help='текстовый интерфейс в консоли (TUI)')
    ap.add_argument('--pen', action='store_true',
                    help='световое перо: мышь рисует линию (с --tui)')
    ap.add_argument('--graphics', choices=('auto', 'kitty', 'blocks', 'mono'),
                    default=None,
                    help='показ графики: протокол графики терминала, '
                         'половинные блоки или один знак на знакомест '
                         '(по умолчанию из конфигурации — auto)')
    ap.add_argument('--demo', action='store_true',
                    help='демонстрация без ЭВМ (сценарий нажатий)')
    ap.add_argument('--conf', metavar='FILE',
                    help='файл конфигурации (по умолчанию ищется сам)')
    ap.add_argument('--init-config', nargs='?', const='-', metavar='FILE',
                    help='записать образец конфигурации и выйти')
    ap.add_argument('--list-backends', action='store_true',
                    help='показать доступные backend-ы и выйти')
    ap.add_argument('-v', '--verbose', action='count', default=0,
                    help='подробный журнал (повторяется)')
    ap.add_argument('--version', action='version',
                    version=f'mc7401 {__version__}')
    return ap


def setup_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format='%(levelname)s: %(message)s')


def _gui_usable() -> bool:
    """Доступно ли графическое окно: есть tkinter и дисплей."""
    try:
        import tkinter
        root = tkinter.Tk()
        root.withdraw()
        root.destroy()
        return True
    except Exception:
        return False


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)

    if args.init_config is not None:
        from .config import Config, user_config_path
        dest = args.init_config
        if dest == '-':
            dest = str(user_config_path())
        path = Config.default().write(dest)
        print(f'записан образец конфигурации: {path}')
        print('правьте значения; файл подхватывается автоматически.')
        return 0

    from .config import load as load_config
    cfg = load_config(args.conf)
    # Масштаб выгрузки кадра: у окна он из конфигурации, где 1 — знак
    # ровно 14×16 точек, а картинку удобнее писать вдвое крупнее.
    export_scale = args.scale if args.scale else 2

    if args.list_backends:
        for name in sorted(BACKENDS):
            print(f'{name:8s} {BACKENDS[name].__doc__.splitlines()[0]}'
                  if BACKENDS[name].__doc__ else name)
        return 0

    if args.listen:
        from .link import run_listener
        return run_listener(args.listen, backend=args.backend,
                            output=args.output, scale=export_scale,
                            timeout=args.link_timeout,
                            verbose=args.verbose)

    # Интерактивный режим — по умолчанию.  Без файла на входе и при
    # терминале на stdin открываем диалог с терминалом.  Любой явно
    # заданный параметр вывода отключает его.
    action_flags = (args.link or args.listen or args.dump_ascii or args.stats
                    or args.png or args.frames or args.list_backends)
    interactive = (args.gui or args.tui
                   or (args.input is None and not action_flags
                       and sys.stdin.isatty()))

    # `--link` без указания выходного файла тоже открывает окно: иначе
    # связь с ЭВМ выглядит как команда, которая «ничего не делает».
    if (args.link and args.input is None and not args.png
            and not args.dump_ascii and not args.stats):
        interactive = True

    if interactive:
        # По умолчанию — графическое окно (полноценный графический
        # терминал); `--tui` выбирает текстовый интерфейс.  Без графики
        # (нет tkinter или дисплея) откатываемся на TUI.
        if not args.tui and _gui_usable():
            from .window import run_window
            # Ограничение увеличения кадра: из конфигурации, если его не
            # задали в командной строке.  0 — кадр ровно по размеру окна.
            scale = args.scale if args.scale else int(getattr(cfg, 'scale', 0))
            code = run_window(endpoint=args.link, scale=scale,
                              demo=args.demo, config=cfg)
            if code != 3:                 # 3 — графика недоступна
                return code
            print('графическое окно недоступно — текстовый режим',
                  file=sys.stderr)
        from .tui import run_tui
        path = Path(args.input) if args.input not in ('-', None) else None
        mode = 'mono' if args.dump_ascii else None
        return run_tui(path, endpoint=args.link, demo=args.demo,
                       mode=mode, pen=args.pen, config=cfg,
                       graphics=args.graphics)

    if args.link:
        from .link import run_link
        return run_link(args.link, backend=args.backend,
                        output=args.output, scale=export_scale,
                        timeout=args.link_timeout,
                        verbose=args.verbose)

    data = sys.stdin.buffer.read() if args.input in ('-', None) \
        else Path(args.input).read_bytes()
    if not data:
        print('пустой поток команд', file=sys.stderr)
        return 1

    emu = TerminalEmulator(xor_code=args.xor_code)

    frames: list = []
    if args.frames or args.peak:
        frames, fb = emu.run(data, collect_frames=True)
    else:
        fb = emu.run(data)

    if args.peak and frames:
        fb = max(frames, key=lambda f: f.count_set())

    if args.stats:
        print(f'команд: {emu.stats["commands"]}, '
              f'не распознано: {emu.stats["unknown"]}, '
              f'символов: {emu.stats["chars"]}, '
              f'кадров: {emu.frames}', file=sys.stderr)

    if args.dump_ascii:
        from .backends import dump_text
        sys.stdout.write(dump_text(fb))
        return 0

    if args.backend == 'png':
        PNGBackend(fb, scale=export_scale).save(args.output)
        print(args.output)
    elif args.backend == 'svg':
        SVGBackend(fb).save(args.output)
        print(args.output)
    elif args.backend == 'tk':                       # pragma: no cover
        TkBackend(fb, scale=export_scale).show()
    elif args.backend == 'pygame':                   # pragma: no cover
        PygameBackend(fb, scale=export_scale, fps=args.fps).show(frames or None)

    if args.png:
        PNGBackend(fb, scale=export_scale).save(args.png)
        print(args.png)
    return 0


if __name__ == '__main__':                            # pragma: no cover
    raise SystemExit(main())