"""
tests/conftest.py — общие фикстуры и утилиты для тестов.

Содержит:
    - фабрику потоков команд `make_stream`;
    - хелпер для сборки одной команды `cmd`;
    - фикстуры эталонных потоков: `sun_stream`, `grid_stream`, `mixed_stream`;
    - фикстуры временных каталогов для выходных SVG/PNG;
    - фикстуру с готовым `TerminalState`.

Автор: <ваше имя>
Лицензия: MIT
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable

import pytest

# Добавляем корень проекта в sys.path, чтобы тесты работали
# и при запуске `pytest tests/` из корня, и из IDE.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reader import VK, S0, S1, END  # noqa: E402
from state import TerminalState  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Фабрики
# ─────────────────────────────────────────────────────────────────────────────

def cmd(s: bytes | str) -> bytes:
    """
    Собрать одну команду: добавить ВК в конец.

    Пример:
        >>> cmd(b"J1")
        b'J1\\x55'
    """
    if isinstance(s, str):
        s = s.encode("latin-1")
    return s + bytes([VK])


def make_stream(*commands: bytes | str) -> bytes:
    """
    Собрать поток из отдельных команд.

    Каждая команда автоматически завершается ВК.

    Пример:
        >>> make_stream("J1", "S", b"4 10 20")
        b'J1\\x55S\\x554 10 20\\x55'
    """
    return b"".join(cmd(c) for c in commands)


def make_raw_stream(*parts: bytes) -> bytes:
    """Склеить произвольные байты без добавления ВК."""
    return b"".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Временные каталоги
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_svg(tmp_path: Path) -> Path:
    """Путь к временному SVG-файлу."""
    return tmp_path / "test_output.svg"


@pytest.fixture
def tmp_png(tmp_path: Path) -> Path:
    """Путь к временному PNG-файлу."""
    return tmp_path / "test_output.png"


@pytest.fixture
def streams_dir(tmp_path: Path) -> Path:
    """Каталог для эталонных потоков в рамках одного теста."""
    d = tmp_path / "streams"
    d.mkdir()
    return d


# ─────────────────────────────────────────────────────────────────────────────
# Состояние
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def state() -> TerminalState:
    """Чистое состояние терминала."""
    return TerminalState()


# ─────────────────────────────────────────────────────────────────────────────
# Эталонные потоки
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def sun_stream() -> bytes:
    """
    Поток «Солнце»: рамка + 20 концентрических окружностей.

    Соответствует процедуре СОЛНЦЕ из graf.pas (в миниатюре).
    """
    parts: list[bytes] = [
        cmd(b"J1"),
        cmd(b"S"),
        cmd(b"] 8 1 552 398"),
        cmd(b"4 280 200"),
    ]
    for r in range(1, 21):
        parts.append(cmd(f"> 1 {r}".encode()))
    parts.append(cmd(b"2"))
    return b"".join(parts)


@pytest.fixture
def grid_stream() -> bytes:
    """
    Поток «Сетка»: рамка + вертикальные и горизонтальные линии.
    """
    parts: list[bytes] = [
        cmd(b"J1"),
        cmd(b"S"),
        cmd(b"] 8 1 552 398"),
    ]
    # Вертикальные линии.
    for i in range(1, 10):
        x = i * 50
        parts.append(cmd(f"4 {x} 0".encode()))
        parts.append(cmd(f"7 1 {x} 400".encode()))
    # Горизонтальные линии.
    for i in range(1, 8):
        y = i * 50
        parts.append(cmd(f"4 0 {y}".encode()))
        parts.append(cmd(f"7 1 560 {y}".encode()))
    parts.append(cmd(b"2"))
    return b"".join(parts)


@pytest.fixture
def mixed_stream() -> bytes:
    """
    Смешанный поток: текст, кириллица, XOR, смена шрифтов.

    Проверяет:
        - команду 9 (текст);
        - переключение S0/S1;
        - режимы OR/XOR;
        - смещение окна.
    """
    parts: list[bytes] = [
        cmd(b"J1"),
        cmd(b"S"),
        # ASCII-текст
        cmd(b"9 1"),
        cmd(b";0 HELLO"),
        # Переключение на русский шрифт + кириллица КОИ-8Р
        cmd(b"9 1"),
        b";0 " + bytes([S0]) + b"\xc1\xc2\xc3" + bytes([S1]) + bytes([VK]),
        # XOR-линия
        cmd(b"4 100 100"),
        cmd(b"7 2 400 300"),
        cmd(b"\x81"),  # ’≠’ в KOI-8 — здесь как байт 0x81
        # Смещение окна
        cmd(b"} 100 50"),
        cmd(b"4 0 0"),
        cmd(b"> 1 30"),
        # Конец
        cmd(b"2"),
    ]
    return b"".join(parts)


@pytest.fixture
def binary_stream() -> bytes:
    """
    Поток с двоичным кодированием параметров.

    Пример:
        7 <T> <X> <Y>  — три 7-битных байта.
    """
    return b"".join([
        cmd(b"J1"),
        cmd(b"4 0 0"),
        # команда 7, T=1, X=100, Y=100 (в двоичном)
        cmd(bytes([ord("7"), 1, 100, 100])),
        cmd(b"2"),
    ])


@pytest.fixture
def truncated_stream() -> bytes:
    """Оборванный поток: последняя команда без завершающего ВК."""
    return cmd(b"J1") + b"4 100 200"  # нет ВК в конце