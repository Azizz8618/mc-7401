"""
tests/test_launcher.py — один исполняемый файл запуска (`tools/mc7401`).

Короткая команда `mc7401` — символическая ссылка на этот файл; без
аргументов она должна открывать интерактивный режим терминала.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / 'tools' / 'mc7401'


def test_launcher_is_one_executable_file():
    """Файл запуска существует и исполняем."""
    assert LAUNCHER.is_file()
    assert os.access(LAUNCHER, os.X_OK)
    text = LAUNCHER.read_text(encoding='utf-8')
    assert text.startswith('#!')                 # запускается как программа
    assert 'mc7401.cli' in text                  # ведёт в командную строку


def test_launcher_runs_from_any_directory(tmp_path):
    """Из чужой папки пакет находится по месту самого файла."""
    out = subprocess.run([sys.executable, str(LAUNCHER), '--version'],
                         capture_output=True, text=True, cwd=tmp_path)
    assert out.returncode == 0
    assert 'mc7401' in out.stdout


def test_short_command_starts_interactive_mode(monkeypatch):
    """`mc7401` без аргументов открывает интерактивный режим (TUI)."""
    import io
    sys.path.insert(0, str(ROOT))
    from mc7401 import cli, tui

    called = {}

    def fake_run_tui(*a, **kw):
        called['args'] = (a, kw)
        return 0

    monkeypatch.setattr(tui, 'run_tui', fake_run_tui)
    monkeypatch.setattr(sys, 'stdin', io.StringIO())
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    assert cli.main([]) == 0
    assert 'args' in called                 # диалог с терминалом открыт


def test_short_command_help_mentions_interactive():
    """Справка объясняет поведение без аргументов."""
    out = subprocess.run([sys.executable, str(LAUNCHER), '--help'],
                         capture_output=True, text=True)
    assert out.returncode == 0
    flat = ' '.join(out.stdout.split())        # переносы строк argparse
    assert 'без файла запускается интерактивный режим' in flat
