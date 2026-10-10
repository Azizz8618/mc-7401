#!/usr/bin/env python3
"""
main.py — точка входа эмулятора векторного графического терминала
«Электроника МС 7401».

Использование:
    python main.py <stream.bin>                      # отрисовать в output.svg
    python main.py <stream.bin> -o picture.svg       # указать выходной SVG
    python main.py --backend tk <stream.bin>         # интерактивный Tkinter
    python main.py --backend pygame <stream.bin>     # анимация в Pygame
    python main.py <stream.bin> --png picture.png    # экспорт в PNG
    cat stream.bin | python main.py -                # чтение из stdin

Автор: <ваше имя>
Лицензия: MIT
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

from reader import CommandStreamReader
from parser import CommandParser
from state import TerminalState
from emulator import TerminalEmulator


# ─────────────────────────────────────────────────────────────────────────────
# Константы
# ─────────────────────────────────────────────────────────────────────────────

__version__ = "0.1.0"
__prog__ = "mc7401-emulator"

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def build_arg_parser() -> argparse.ArgumentParser:
    """Собрать парсер аргументов командной строки."""
    p = argparse.ArgumentParser(
        prog=__prog__,
        description="Эмулятор векторного графического терминала «Электроника МС 7401».",
        epilog="Пример: python main.py tests/streams/sun.bin --backend svg -o sun.svg",
    )

    p.add_argument(
        "input",
        nargs="?",
        default="-",
        help="Путь к файлу с потоком команд или '-' для чтения из stdin "
             "(по умолчанию: '-' — stdin).",
    )

    p.add_argument(
        "-b", "--backend",
        choices=["svg", "tk", "pygame"],
        default="svg",
        help="Backend для отрисовки (по умолчанию: svg).",
    )

    p.add_argument(
        "-o", "--output",
        default=None,
        help="Путь к выходному файлу (для backend=svg). "
             "По умолчанию: output.svg.",
    )

    p.add_argument(
        "--png",
        default=None,
        help="Дополнительно экспортировать в PNG (требуется cairosvg).",
    )

    p.add_argument(
        "--encoding",
        choices=["auto", "koi8-r", "koi8-u", "cp1251", "ascii"],
        default="auto",
        help="Кодировка текстового потока (по умолчанию: auto = koi8-r).",
    )

    p.add_argument(
        "--strict",
        action="store_true",
        help="Останавливаться на первой ошибке разбора команды.",
    )

    p.add_argument(
        "--max-steps",
        type=int,
        default=0,
        help="Ограничить число обработанных команд (0 = без ограничения).",
    )

    p.add_argument(
        "--fps",
        type=int,
        default=30,
        help="Частота кадров для backend=pygame (по умолчанию: 30).",
    )

    p.add_argument(
        "--scale",
        type=int,
        default=1,
        help="Масштаб окна для backend=tk (по умолчанию: 1).",
    )

    p.add_argument(
        "-v", "--verbose",
        action="count",
        default=0,
        help="Подробный вывод (-v: INFO, -vv: DEBUG).",
    )

    p.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    return p


# ─────────────────────────────────────────────────────────────────────────────
# Логирование
# ─────────────────────────────────────────────────────────────────────────────

def setup_logging(verbosity: int) -> None:
    """Настроить уровень логирования по количеству -v."""
    if verbosity >= 2:
        level = logging.DEBUG
    elif verbosity == 1:
        level = logging.INFO
    else:
        level = logging.WARNING

    logging.basicConfig(level=level, format=LOG_FORMAT, stream=sys.stderr)


# ─────────────────────────────────────────────────────────────────────────────
# Чтение входного потока
# ─────────────────────────────────────────────────────────────────────────────

def read_input(path: str, encoding: str) -> bytes:
    """
    Прочитать входной поток как байты.

    Если path == "-" — читаем из stdin.
    Если файл оканчивается на .txt — читаем как текст и конвертируем в
    байты в указанной кодировке (с заменой ВК на 0o125).
    """
    log = logging.getLogger("main.read_input")

    if path == "-":
        log.info("Чтение из stdin")
        data = sys.stdin.buffer.read()
    else:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Файл не найден: {p}")
        log.info("Чтение из %s (%d байт)", p, p.stat().st_size)
        data = p.read_bytes()

    # Если файл — текстовый (по расширению), преобразуем
    if path != "-" and path.lower().endswith(".txt"):
        enc = "koi8-r" if encoding == "auto" else encoding
        text = data.decode(enc, errors="replace")
        # В текстовой форме команды разделены переводами строк,
        # а в бинарной — символом ВК = 0o125.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = text.replace("\n", chr(0o125))
        data = text.encode("latin-1", errors="replace")
        log.info("Текстовый поток преобразован в бинарный (%d байт)", len(data))

    return data


# ─────────────────────────────────────────────────────────────────────────────
# Backend'ы
# ─────────────────────────────────────────────────────────────────────────────

def create_backend(name: str, state: TerminalState, args: argparse.Namespace):
    """Создать backend по имени."""
    log = logging.getLogger("main.create_backend")

    if name == "svg":
        from backends.svg_backend import SVGBackend
        backend = SVGBackend(state)
        log.info("Backend: SVG")

    elif name == "tk":
        from backends.tk_backend import TkBackend
        backend = TkBackend(state, scale=args.scale)
        log.info("Backend: Tkinter (scale=%d)", args.scale)

    elif name == "pygame":
        from backends.pygame_backend import PygameBackend
        backend = PygameBackend(state, fps=args.fps)
        log.info("Backend: Pygame (fps=%d)", args.fps)

    else:
        raise ValueError(f"Неизвестный backend: {name}")

    return backend


# ─────────────────────────────────────────────────────────────────────────────
# Основной цикл эмуляции
# ─────────────────────────────────────────────────────────────────────────────

def run_emulation(
    data: bytes,
    backend,
    strict: bool = False,
    max_steps: int = 0,
) -> int:
    """
    Прогнать поток команд через эмулятор.

    Возвращает число успешно обработанных команд.
    """
    log = logging.getLogger("main.run")

    reader = CommandStreamReader(data)
    parser = CommandParser(reader)
    state = backend.state if hasattr(backend, "state") else TerminalState()
    emu = TerminalEmulator(state, backend)

    steps = 0
    errors = 0

    while True:
        if max_steps and steps >= max_steps:
            log.info("Достигнут лимит команд: %d", max_steps)
            break

        try:
            cmd = parser.parse()
        except Exception as exc:  # noqa: BLE001
            errors += 1
            log.error("Ошибка разбора команды #%d: %s", steps, exc)
            if strict:
                raise
            continue

        if cmd is None:
            break

        log.debug("Команда #%d: %r", steps, cmd)

        try:
            emu.execute(cmd)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            log.error("Ошибка выполнения команды #%d (%r): %s", steps, cmd, exc)
            if strict:
                raise

        steps += 1

    log.info("Обработано команд: %d, ошибок: %d", steps, errors)
    return steps


# ─────────────────────────────────────────────────────────────────────────────
# Сохранение результата
# ─────────────────────────────────────────────────────────────────────────────

def save_svg(backend, output: Optional[str]) -> Path:
    """Сохранить результат работы SVG-backend."""
    log = logging.getLogger("main.save_svg")
    out = Path(output) if output else Path("output.svg")
    backend.save(str(out))
    log.info("SVG сохранён: %s", out)
    return out


def export_png(svg_path: Path, png_path: str) -> None:
    """Экспортировать SVG в PNG через cairosvg."""
    log = logging.getLogger("main.export_png")
    try:
        import cairosvg  # type: ignore
    except ImportError:
        log.error("cairosvg не установлен — экспорт в PNG пропущен")
        return

    cairosvg.svg2png(url=str(svg_path), write_to=png_path)
    log.info("PNG сохранён: %s", png_path)


# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    setup_logging(args.verbose)
    log = logging.getLogger("main")

    log.info("%s v%s", __prog__, __version__)

    # 1. Читаем вход
    try:
        data = read_input(args.input, args.encoding)
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return 2

    if not data:
        log.error("Пустой входной поток")
        return 3

    # 2. Создаём состояние и backend
    state = TerminalState()
    try:
        backend = create_backend(args.backend, state, args)
    except ImportError as exc:
        log.error("Не удалось загрузить backend '%s': %s", args.backend, exc)
        return 4

    # 3. Прогоняем эмуляцию
    try:
        steps = run_emulation(
            data,
            backend,
            strict=args.strict,
            max_steps=args.max_steps,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("Критическая ошибка эмуляции: %s", exc)
        return 5

    if steps == 0:
        log.warning("Ни одной команды не обработано")

    # 4. Сохраняем / показываем результат
    if args.backend == "svg":
        svg_path = save_svg(backend, args.output)
        if args.png:
            export_png(svg_path, args.png)

    elif args.backend == "tk":
        log.info("Запуск интерактивного окна Tkinter...")
        backend.run()

    elif args.backend == "pygame":
        log.info("Запуск анимации Pygame...")
        backend.run()

    log.info("Готово.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
