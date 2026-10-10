"""
mc7401_emulator — эмулятор векторного графического терминала
«Электроника МС 7401».

Проект восстанавливает систему команд советского растрового дисплея
по исходникам Pascal-программ (graf.pas, graf1.pas) и по статье:

    А. О. Лацис, Вс. С. Штаркман. Системы команд растрового графического
    дисплея «Электроника МС7401» // Микропроцессорные средства и системы.
    1989. № 5. С. 39–44.

Публичный API пакета:

    ── Ввод/разбор ─────────────────────────────────────────────────
    CommandStreamReader   — разбиение бинарного потока на команды;
    Command               — dataclass разобранной команды;
    CommandParser         — парсер команд;
    ParseError            — исключение разбора;

    ── Состояние и исполнение ──────────────────────────────────────
    TerminalState         — состояние терминала;
    TerminalEmulator      — исполнитель команд;

    ── Backend'ы ───────────────────────────────────────────────────
    SVGBackend            — экспорт в SVG;
    TkBackend             — интерактивный просмотр (Tkinter);
    PygameBackend         — анимации и XOR (Pygame);
    NullBackend           — заглушка (для тестов);
    Backend               — Protocol интерфейса backend'а;
    create_backend        — фабрика backend'ов по имени;

    ── Утилиты ─────────────────────────────────────────────────────
    run_stream            — прогон потока через выбранный backend;
    parse_stream          — разбор потока в список Command;
    parse_text_stream     — разбор текстовой формы команд.

Пример:
    from mc7401_emulator import (
        CommandStreamReader, CommandParser, TerminalState,
        TerminalEmulator, SVGBackend,
    )

    data = open("stream.bin", "rb").read()
    reader = CommandStreamReader(data)
    parser = CommandParser(reader)
    state  = TerminalState()
    backend = SVGBackend(state)
    emu = TerminalEmulator(state, backend)

    for cmd in parser:
        emu.execute(cmd)

    backend.save("output.svg")

Автор: <ваше имя>
Лицензия: MIT
"""

from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────────
# Метаданные пакета
# ─────────────────────────────────────────────────────────────────────────────

__title__       = "mc7401-emulator"
__description__ = "Эмулятор векторного графического терминала «Электроника МС 7401»"
__version__     = "0.1.0"
__author__      = "<ваше имя>"
__license__     = "MIT"
__url__         = "https://github.com/<user>/mc7401-emulator"

# Публичный API — используется IDE, линтерами и `from pkg import *`.
__all__ = [
    # метаданные
    "__version__",

    # ввод/разбор
    "CommandStreamReader",
    "Command",
    "CommandParser",
    "ParseError",

    # состояние и исполнение
    "TerminalState",
    "TerminalEmulator",
    "NullBackend",

    # backend'ы
    "SVGBackend",
    "TkBackend",
    "PygameBackend",
    "Backend",
    "create_backend",
    "available_backends",
    "backend_info",

    # утилиты
    "run_stream",
    "parse_stream",
    "parse_text_stream",
]


# ─────────────────────────────────────────────────────────────────────────────
# Импорты
# ─────────────────────────────────────────────────────────────────────────────

# Ввод/разбор.
from reader import (
    CommandStreamReader,
    VK,
    S0,
    S1,
    LF,
    END,
    decode_text,
    split_by_font,
)

from parser import (
    Command,
    CommandParser,
    ParseError,
    parse_stream,
    parse_text_stream,
)

# Состояние и исполнение.
from state import (
    TerminalState,
    SCREEN_WIDTH,
    SCREEN_HEIGHT,
    MODE_OR,
    MODE_XOR,
    MODE_ERASE,
    FONT_EN,
    FONT_RU,
    LINE_TYPES,
    LINE_DASHES,
)

from emulator import (
    TerminalEmulator,
    NullBackend,
    run_stream,
)

# Backend'ы. Импортируем через пакет backends, который сам умеет
# пропускать те, чьи зависимости недоступны (например, tkinter
# или pygame могут отсутствовать в системе).
from backends import (
    SVGBackend,
    TkBackend,
    PygameBackend,
    Backend,
    create_backend,
    available_backends,
    backend_info,
)


# ─────────────────────────────────────────────────────────────────────────────
# Удобные псевдонимы для быстрого доступа
# ─────────────────────────────────────────────────────────────────────────────

# Размеры экрана как кортеж — удобно передавать в backend'ы.
SCREEN_SIZE = (SCREEN_WIDTH, SCREEN_HEIGHT)


# ─────────────────────────────────────────────────────────────────────────────
# Проверка окружения
# ─────────────────────────────────────────────────────────────────────────────

def _check_environment() -> dict[str, bool]:
    """
    Проверить доступность опциональных зависимостей.

    Возвращает словарь:
        {
            'tkinter': True/False,
            'pygame':  True/False,
            'PIL':     True/False,
            'cairosvg': True/False,
        }
    """
    result: dict[str, bool] = {}

    try:
        import tkinter  # noqa: F401
        result["tkinter"] = True
    except ImportError:
        result["tkinter"] = False

    try:
        import pygame  # noqa: F401
        result["pygame"] = True
    except ImportError:
        result["pygame"] = False

    try:
        import PIL  # noqa: F401
        result["PIL"] = True
    except ImportError:
        result["PIL"] = False

    try:
        import cairosvg  # noqa: F401
        result["cairosvg"] = True
    except ImportError:
        result["cairosvg"] = False

    return result


def _build_env_table() -> str:
    """Человекочитаемая таблица доступности зависимостей."""
    env = _check_environment()
    lines = ["Зависимости:"]
    for key, ok in env.items():
        mark = "✓" if ok else "✗"
        lines.append(f"  {mark} {key}")
    return "\n".join(lines)


def info() -> str:
    """
    Вернуть краткую информацию о пакете и его окружении.

    Полезно для отладки и для CLI (`mc7401-info`).
    """
    lines = [
        f"{__title__} v{__version__}",
        __description__,
        "",
        f"Backend'ы: {', '.join(available_backends()) or '(нет)'}",
        "",
        _build_env_table(),
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Самотест
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    print("=" * 64)
    print(info())
    print("=" * 64)
    print()

    # Минимальный прогон: сформируем поток вручную и отрисуем в SVG.
    def _cmd(s: bytes) -> bytes:
        return s + bytes([VK])

    stream = (
        _cmd(b"J1") +
        _cmd(b"S") +
        _cmd(b"] 8 1 552 398") +
        _cmd(b"4 280 200") +
        _cmd(b"> 1 100") +
        _cmd(b"7 2 50 50") +
        _cmd(b"7 2 500 350") +
        _cmd(b"9 1") +
        _cmd(b";0 HELLO MC 7401")
    )

    state = TerminalState()
    backend = SVGBackend(state)
    emu = TerminalEmulator(state, backend)

    for cmd in CommandParser(CommandStreamReader(stream)):
        emu.execute(cmd)

    print(f"Обработано команд: {sum(emu.stats.values())}")
    print(f"Элементов SVG:     {backend.element_count}")
    print(f"Статистика:        {dict(sorted(emu.stats.items()))}")

    out = backend.save("test_package_init.svg")
    print(f"SVG сохранён: {out}")

    # Проверка публичного API.
    print()
    print("Публичный API:")
    for name in __all__:
        print(f"  • {name}")