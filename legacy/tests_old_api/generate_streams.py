"""
tests/generate_streams.py — генератор эталонных бинарных потоков.

Запускается вручную для пересоздания tests/streams/*.bin:

    python -m tests.generate_streams

Используется, чтобы не хранить бинарные файлы в репозитории
в виде «загадочных» артефактов — они воспроизводимы.

Автор: <ваше имя>
Лицензия: MIT
"""

from __future__ import annotations

from pathlib import Path

from reader import VK, S0, S1


HERE = Path(__file__).resolve().parent
STREAMS = HERE / "streams"


def _cmd(s: bytes) -> bytes:
    return s + bytes([VK])


def make_sun() -> bytes:
    """Поток «Солнце»: рамка + 150 концентрических окружностей."""
    parts = [
        _cmd(b"J1"),
        _cmd(b"S"),
        _cmd(b"] 8 1 552 398"),
        _cmd(b"4 280 200"),
    ]
    for r in range(1, 151):
        parts.append(_cmd(f"> 1 {r}".encode()))
    parts.append(_cmd(b"2"))
    return b"".join(parts)


def make_grid() -> bytes:
    """Поток «Сетка»: рамка + регулярные линии."""
    parts = [
        _cmd(b"J1"),
        _cmd(b"S"),
        _cmd(b"] 8 1 552 398"),
    ]
    for i in range(1, 79):
        x = i * 7
        parts.append(_cmd(f"4 {x} 0".encode()))
        parts.append(_cmd(f"7 1 {x} 400".encode()))
    for i in range(1, 57):
        y = i * 7
        parts.append(_cmd(f"4 0 {y}".encode()))
        parts.append(_cmd(f"7 1 560 {y}".encode()))
    parts.append(_cmd(b"2"))
    return b"".join(parts)


def make_function() -> bytes:
    """
    Поток «Функция» — затухающая синусоида, как в процедуре ФУНК.

    Упрощённо: N отрезков от (50, 200) до (520, ...).
    """
    import math
    parts = [
        _cmd(b"J1"),
        _cmd(b"S"),
        _cmd(b"] 8 1 552 398"),
    ]
    n = 200
    prev = None
    for i in range(n + 1):
        t = i / n
        x = int(50 + t * 470)
        y = int(200 + 100 * math.exp(-3 * t) * math.sin(20 * t))
        if prev is None:
            parts.append(_cmd(f"4 {x} {y}".encode()))
        else:
            parts.append(_cmd(f"7 1 {x} {y}".encode()))
        prev = (x, y)
    parts.append(_cmd(b"2"))
    return b"".join(parts)


def make_mixed() -> bytes:
    """Поток со смешанным содержимым."""
    return b"".join([
        _cmd(b"J1"),
        _cmd(b"S"),
        _cmd(b"9 1"),
        _cmd(b";0 HELLO"),
        _cmd(b"9 1"),
        b";0 " + bytes([S0]) + b"\xc1\xc2\xc3" + bytes([S1]) + bytes([VK]),
        _cmd(b"4 100 100"),
        _cmd(b"7 2 400 300"),
        _cmd(b"4 0 0"),
        _cmd(b"} 100 50"),
        _cmd(b"> 1 30"),
        _cmd(b"2"),
    ])


def main() -> None:
    STREAMS.mkdir(exist_ok=True)

    builders = {
        "sun.bin":      make_sun,
        "grid.bin":     make_grid,
        "function.bin": make_function,
        "mixed.bin":    make_mixed,
    }

    for name, fn in builders.items():
        data = fn()
        path = STREAMS / name
        path.write_bytes(data)
        print(f"  {name:15s} {len(data):6d} байт  → {path}")


if __name__ == "__main__":
    main()