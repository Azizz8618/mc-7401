"""
backends/__init__.py — пакет backend'ов отрисовки эмулятора
терминала «Электроника МС 7401».

Экспортирует:
    SVGBackend     — генерация SVG-файла;
    TkBackend      — интерактивный просмотр в Tkinter;
    PygameBackend  — анимации и XOR в Pygame;
    NullBackend    — заглушка (только логирование вызовов);
    Backend        — Protocol-интерфейс backend'а;
    create_backend — фабрика backend'ов по имени.

Пример:
    from backends import SVGBackend, create_backend

    backend = create_backend("svg", state)
    backend = create_backend("tk", state, scale=2)
    backend = create_backend("pygame", state, fps=60)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from state import TerminalState


# ─────────────────────────────────────────────────────────────────────────────
# Импорты backend'ов
# ─────────────────────────────────────────────────────────────────────────────

# Импортируем все backend'ы, кроме тех, чьи зависимости недоступны.
# Например, tkinter или pygame могут быть не установлены — их __init__
# сами выбросят ImportError при попытке использования, но импорт модуля
# должен быть безопасным.

from backends.svg_backend import SVGBackend

try:
    from backends.tk_backend import TkBackend
except ImportError as _exc:  # noqa: F841
    TkBackend = None  # type: ignore[assignment]

try:
    from backends.pygame_backend import PygameBackend
except ImportError as _exc:  # noqa: F841
    PygameBackend = None  # type: ignore[assignment]

# NullBackend живёт в emulator.py — импортируем оттуда.
try:
    from emulator import NullBackend
except ImportError:
    NullBackend = None  # type: ignore[assignment]


# ─────────────────────────────────────────────────────────────────────────────
# Protocol backend'а
# ─────────────────────────────────────────────────────────────────────────────

@runtime_checkable
class Backend(Protocol):
    """
    Минимальный интерфейс backend'а отрисовки.

    Любой backend должен реализовать:
        draw_line(x1, y1, x2, y2, line_type)
        draw_circle(cx, cy, r, line_type)
        draw_rect(x1, y1, x2, y2, line_type)
        draw_text(x, y, text, size, orient, font)
        clear()

    Опционально:
        set_mode(mode)     — переключение OR/XOR/ERASE;
        present()          — отображение кадра;
        save(path)         — сохранение в файл;
        run()              — главный цикл (для интерактивных backend'ов);
    """

    state: "TerminalState"

    def draw_line(
        self,
        x1: int, y1: int,
        x2: int, y2: int,
        line_type: int,
    ) -> None: ...

    def draw_circle(
        self,
        cx: int, cy: int,
        r: int,
        line_type: int,
    ) -> None: ...

    def draw_rect(
        self,
        x1: int, y1: int,
        x2: int, y2: int,
        line_type: int,
    ) -> None: ...

    def draw_text(
        self,
        x: int, y: int,
        text: str,
        size: int,
        orient: int,
        font: str,
    ) -> None: ...

    def clear(self) -> None: ...


# ─────────────────────────────────────────────────────────────────────────────
# Фабрика
# ─────────────────────────────────────────────────────────────────────────────

# Реестр доступных backend'ов.
# Ключ — имя, значение — класс (или None, если зависимость недоступна).
_BACKENDS: dict[str, Any] = {
    "svg":    SVGBackend,
    "tk":     TkBackend,
    "pygame": PygameBackend,
    "null":   NullBackend,
}


def create_backend(
    name: str,
    state: "TerminalState",
    **kwargs: Any,
) -> Any:
    """
    Создать backend по имени.

    Параметры:
        name   — 'svg', 'tk', 'pygame' или 'null';
        state  — объект TerminalState;
        kwargs — дополнительные параметры, передаются в конструктор.

    Возвращает:
        Экземпляр соответствующего backend'а.

    Бросает:
        ValueError     — если имя неизвестно;
        ImportError    — если у backend'а недоступна зависимость
                         (например, не установлен pygame или Pillow);
        TypeError      — если переданы неподходящие параметры.
    """
    key = name.lower().strip()
    if key not in _BACKENDS:
        available = ", ".join(sorted(k for k, v in _BACKENDS.items() if v))
        raise ValueError(
            f"Неизвестный backend: {name!r}. Доступные: {available}"
        )

    cls = _BACKENDS[key]
    if cls is None:
        raise ImportError(
            f"Backend {name!r} недоступен: не установлены зависимости. "
            f"См. README.md (раздел «Установка»)."
        )

    # Разные backend'ы принимают разные kwargs.
    # Передаём всё как есть — конструктор сам разберётся.
    return cls(state, **kwargs)


def available_backends() -> list[str]:
    """
    Вернуть список доступных backend'ов (тех, чьи зависимости
    установлены в текущем окружении).
    """
    return sorted(k for k, v in _BACKENDS.items() if v is not None)


def backend_info() -> dict[str, dict[str, Any]]:
    """
    Вернуть информацию о всех backend'ах: имя, доступность,
    имя класса, краткое описание.
    """
    descriptions = {
        "svg":    "Генерация SVG-файла (статичные изображения, отладка).",
        "tk":     "Интерактивный просмотр в окне Tkinter.",
        "pygame": "Анимации и полноценный XOR-режим в Pygame.",
        "null":   "Заглушка: только запись вызовов (для тестов).",
    }
    info: dict[str, dict[str, Any]] = {}
    for key, cls in _BACKENDS.items():
        info[key] = {
            "available": cls is not None,
            "class": cls.__name__ if cls else None,
            "description": descriptions.get(key, ""),
        }
    return info


# ─────────────────────────────────────────────────────────────────────────────
# Публичный API
# ─────────────────────────────────────────────────────────────────────────────

__all__ = [
    "SVGBackend",
    "TkBackend",
    "PygameBackend",
    "NullBackend",
    "Backend",
    "create_backend",
    "available_backends",
    "backend_info",
]


# ─────────────────────────────────────────────────────────────────────────────
# Самотест
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    print("Доступные backend'ы:")
    for name, info in backend_info().items():
        mark = "✓" if info["available"] else "✗"
        cls = info["class"] or "—"
        print(f"  {mark} {name:8s} [{cls:15s}] {info['description']}")

    print()
    print("Список:", available_backends())

    # Проверим фабрику.
    from state import TerminalState
    st = TerminalState()

    print()
    print("Создание backend'ов через create_backend:")
    for name in available_backends():
        try:
            b = create_backend(name, st)
            print(f"  {name:8s} → {b!r}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {name:8s} → ошибка: {exc}")

    # Проверим, что неизвестное имя даёт ValueError.
    print()
    try:
        create_backend("unknown", st)
    except ValueError as exc:
        print(f"Ожидаемое исключение: {exc}")