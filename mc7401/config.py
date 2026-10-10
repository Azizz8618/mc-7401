"""
config.py — файл конфигурации эмулятора МС 7401.

Формат повторяет конфигурацию терминала 15ИЭ (`ie15/ie15.conf`):
секции в стиле INI, ключ `знак = значение`, комментарии отдельными
строками.  Аргументы командной строки имеют приоритет над файлом.

    [line]      подключение к БЭСМ-6 по ДКС
    [terminal]  режим сеанса, набор знаков, эхо, размер текста
    [display]   вывод в терминал хоста: цвет, масштаб, сетка, перо
    [keys]      переназначение клавиш: «пришло = набирается»

Порядок поиска файла: `--conf`, `$XDG_CONFIG_HOME/mc7401/mc7401.conf`
(или `~/.config/mc7401/mc7401.conf`), `./mc7401.conf`, затем
`mc7401.conf` рядом с пакетом.
"""

from __future__ import annotations

import configparser
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger('mc7401.config')

CONFIG_NAME = 'mc7401.conf'

SAMPLE = f"""\
# Конфигурация эмулятора терминала «Электроника МС 7401».
# Аргументы командной строки имеют приоритет над этим файлом.

[line]
# Линия ДКС БЭСМ-6. ЭВМ слушает порт, терминал к нему подключается
# (`set ttyN dks,raw` в SIMH); можно перечислить несколько линий:
# «4202», «4202,4210», «4202-4223» — занятые пропускаются.
endpoint = 127.0.0.1:4202-4223
# Молча подменять связь эталонной картинкой нельзя: при недоступной
# линии печатается объяснение и возвращается код 2.

[terminal]
# Режим сеанса: host (С ЭВМ) или local (АВТОНОМНО).
mode = host
# Набор знаков: n2 — Н2 (КОИ-7), набор команд №2 VT-52 (по умолчанию);
#              koi8r — КОИ-8Р (кириллица в 0x60…0x7F).
# Клавиша `z` переключает набор по кругу.
charset = n2
# Эхо набираемого на экране (клавиша `e`). Снято — пароль не печатается.
echo = true
# Показывать перекрестье светового пера и рисовать им сразу.
pen = false
# Показывать координатную сетку поверх картинки.
grid = false

[display]
# Способ вывода: color (половинные блоки с ANSI-цветом) или mono
# (один знак на знакомест, работает везде).
mode = color
# Масштаб показа: 1 — пиксель в пиксель, больше — мельче.
scale = 2
# Показывать верхнюю строку с названием модели и временем.
title = true

[keys]
# Переназначение клавиш: «что нажато = что делается».
# Значение — действие: режим, передать, эхо, очистить, набор, перо.
# Пустое значение гасит клавишу. Примеры:
# L = mode
# e = echo
# q = quit
"""


@dataclass
class Config:
    """Настройки эмулятора, прочитанные из файла."""

    # [line]
    endpoint: str = '127.0.0.1:4202-4223'
    # [terminal]
    mode: str = 'host'
    charset: str = 'n2'
    echo: bool = True
    pen: bool = False
    grid: bool = False
    # [display]
    display_mode: str = 'color'
    scale: int = 2
    title: bool = True
    # [keys]
    keys: dict[str, str] = field(default_factory=dict)
    #: Откуда прочитали (для сообщения при запуске).
    path: Optional[Path] = None

    # ── загрузка ────────────────────────────────────────────────────────────

    @classmethod
    def default(cls) -> 'Config':
        """Конфигурация по умолчанию (без файла)."""
        return cls()

    @classmethod
    def load(cls, path: Optional[str | Path] = None) -> 'Config':
        """Прочитать конфигурацию; ненайденный файл — значения по умолчанию."""
        found = find_config(path)
        cfg = cls()
        if found is None:
            if path:
                log.warning('файл конфигурации не найден: %s', path)
            return cfg
        parser = configparser.ConfigParser()
        # Регистр клавиш значим (`L` — режим, `l` — другая клавиша),
        # поэтому стандартное приведение имён к нижнему регистру отключено.
        parser.optionxform = str
        try:
            parser.read(found, encoding='utf-8')
        except configparser.Error as exc:
            log.warning('не разобран %s: %s', found, exc)
            return cfg
        cfg.path = found
        cfg._apply(parser)
        return cfg

    def _apply(self, p: configparser.ConfigParser) -> None:
        if p.has_section('line'):
            self.endpoint = p.get('line', 'endpoint', fallback=self.endpoint)
        if p.has_section('terminal'):
            self.mode = p.get('terminal', 'mode', fallback=self.mode)
            self.charset = p.get('terminal', 'charset', fallback=self.charset)
            self.echo = p.getboolean('terminal', 'echo', fallback=self.echo)
            self.pen = p.getboolean('terminal', 'pen', fallback=self.pen)
            self.grid = p.getboolean('terminal', 'grid', fallback=self.grid)
        if p.has_section('display'):
            self.display_mode = p.get('display', 'mode',
                                      fallback=self.display_mode)
            self.scale = p.getint('display', 'scale', fallback=self.scale)
            self.title = p.getboolean('display', 'title', fallback=self.title)
        if p.has_section('keys'):
            self.keys = {k.strip(): v.strip() for k, v in p.items('keys')
                         if k.strip() and not k.strip().startswith('#')}

    # ── сохранение ──────────────────────────────────────────────────────────

    def write(self, path: str | Path) -> Path:
        """Записать образец конфигурации (для правки пользователем)."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(SAMPLE, encoding='utf-8')
        return p

    # ── применение ──────────────────────────────────────────────────────────

    def key_action(self, key: str) -> Optional[str]:
        """Действие, назначенное клавише; `None` — по умолчанию."""
        return self.keys.get(key)

    def summary(self) -> str:
        """Краткая сводка для сообщения при запуске."""
        bits = [f'набор {self.charset}',
                f'режим {self.mode}',
                'эхо вкл' if self.echo else 'эхо выкл',
                f'линия {self.endpoint}']
        return ', '.join(bits)


def user_config_path() -> Path:
    """Путь пользовательского файла конфигурации."""
    base = os.environ.get('XDG_CONFIG_HOME')
    root = Path(base) if base else Path.home() / '.config'
    return root / 'mc7401' / CONFIG_NAME


def find_config(explicit: Optional[str | Path] = None) -> Optional[Path]:
    """Найти файл конфигурации; `None` — не найден."""
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    for cand in (user_config_path(), Path.cwd() / CONFIG_NAME,
                 Path(__file__).resolve().parent.parent / CONFIG_NAME):
        if cand.is_file():
            return cand
    return None


def load(path: Optional[str | Path] = None) -> Config:
    return Config.load(path)


__all__ = ['Config', 'load', 'find_config', 'user_config_path', 'SAMPLE',
           'CONFIG_NAME']