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

from .history import DEFAULT_LIMIT as DEFAULT_HISTORY
from .undo import LIMIT as DEFAULT_UNDO

import configparser
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger('mc7401.config')

CONFIG_NAME = 'mc7401.conf'

#: Имена символов, которыми может заканчиваться введённая строка.
#: ВК = возврат каретки = ПР (CR = 0o015); ПС = перевод строки (LF);
#: КЗ = конец записи (ETX) — для линии ДКС это просто знак.
EOL_NAMES = {'ETX': 0o003, 'LF': 0o012, 'CR': 0o015, 'ВК': 0o015,
              'ПС': 0o012, 'ПР': 0o015,
              'КР': 0o015, 'КЗ': 0o003}


def parse_eol(value: Optional[str], default: int = 0o015) -> int:
    """Разобрать `[line] eol`: имя символа, восьмеричный код или число.

    Возвращает код символа; неразборчивое значение — значение по умолчанию
    с предупреждением, а не молчаливая подмена.  По эталону 15ИЭ
    (`ie15emu/charset.to_line`) окончание строки на ДКС — ПР (CR).
    """
    if value is None:
        return default
    text = value.strip()
    if not text:
        return default
    upper = text.upper()
    if upper in EOL_NAMES:
        return EOL_NAMES[upper]
    try:
        code = int(text, 8) if text.lower().startswith(('0o', '0х', '0x')) \
            else int(text, 8)
    except ValueError:
        try:
            code = int(text, 10)
        except ValueError:
            log.warning('не разобран конец строки %r, беру 0o%03o',
                        text, default)
            return default
    if not 0 <= code <= 0o377:
        log.warning('конец строки %r вне диапазона, беру 0o%03o',
                    text, default)
        return default
    return code


def clamp_glyph(value: int) -> int:
    """Размер знака текстового слоя кадра — целое 1…4.

    1 — растр ПЗУ 7×8 как есть (знакомест 8×10, сетка 70×40),
    2 — вдвое крупнее (14×16, знакомест 16×20, сетка 35×20), и так
    далее.  Слишком крупный знак не делит кадр на равные знакоместа,
    поэтому значения вне 1…4 приводятся к крайнему.
    """
    return max(GLYPH_MIN, min(GLYPH_MAX, int(value)))


#: Допустимые размеры знака текстового слоя кадра.
GLYPH_MIN, GLYPH_MAX = 1, 4


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
# Чем заканчивается введённая строка, когда терминал передаёт её на ЭВМ.
# По эталону 15ИЭ (`ie15emu/charset.to_line`, ветка dks) окончание
# строки на линии ДКС — ПР (0o015); ETX для неё — просто знак.  Живая
# машина подтверждает: команда с ETX не выполняется, с ПР — выполняется.
# Значение — имя символа: CR (по умолчанию), LF, ETX или код
# в восьмеричной записи (0o015).
eol = CR
# Запрашивать ли приглашение ОС при подключении к линии: терминал
# сам отправляет ПРПС и ждёт ответа.  Нужно было для проверки
# работоспособности на начальном этапе; по умолчанию выключено —
# терминал молчит, пока оператор не нажмёт ПРПС (Enter) сам.
prompt = false

[terminal]
# Режим сеанса: host (С ЭВМ) или local (АВТОНОМНО).
mode = host
# Набор знаков: n2 — Н2 (КОИ-7), набор команд №2 VT-52 (по умолчанию);
#              koi8r — КОИ-8Р (кириллица в 0x60…0x7F).
# Клавиша `z` переключает набор по кругу.
charset = n2
# Эхо набираемого на экране (клавиша `e`). Снято — пароль не печатается.
echo = true
# Размер знака текстового слоя кадра: во сколько раз растр знакогенератора
# (7×8 точек) увеличен на кадре.  1 — как в ПЗУ: знакомест 8×10,
# сетка 70×40 (**по умолчанию**).  2 — вдвое крупнее: растр 14×16,
# знакомест 16×20, сетка 35×20.  3 и 4 — ещё крупнее; полоса служебных
# строк (строка состояния и подвал) выводится тем же знаком, поэтому она
# растёт вместе с текстом.  Допустимо 1…4, иное значение приводится
# к крайнему.
#
# Размер знака **на экране** задаётся двумя настройками сразу: этим и
# `[display] scale`.  При `glyph = 1, scale = 2` растр ПЗУ 7×8 выводится
# на экране теми же 14×16 точками, что и при `glyph = 2, scale = 1`, но
# знакомест на кадре вдвое больше — 70×40 вместо 35×20.
glyph = 1
# Показывать перекрестье светового пера и рисовать им сразу.
pen = false
# Показывать координатную сетку поверх картинки.
grid = false
# Легенда клавиш в подвале (ряды-справки под чертой).  Как на 15ИЭ:
# `help = false` убирает их — остаются ряды состояний и строка
# состояния, а освободившиеся ряды достаются кадру.
help = true

[display]
# Способ вывода: color (половинные блоки с ANSI-цветом) или mono
# (один знак на знакомест, работает везде).
mode = color
# Ограничение увеличения кадра.  `0` (**по умолчанию**) — кадр ровно
# по размеру окна пользователя, какой бы оно ни было: строка состояния
# прижата к верху окна, подвал к низу, кадр занимает всё между ними.
# Положительное число запрещает кадру стать крупнее (например, `2` —
# не больше чем знак 14×16 при растре ПЗУ 7×8).
scale = 0
# Окно во весь экран, кадр вписан в него вместе со служебными строками.
fullscreen = true
# Показывать верхнюю строку с названием модели и временем.
title = true
# Показ графики:
#   auto   — если терминал умеет протокол графики (kitty, WezTerm,
#            Konsole, Ghostty, foot), кадр 560×400 показывается в окне
#            как настоящая картинка; иначе — псевдографика;
#   kitty  — требовать протокол, иначе сообщение об ошибке;
#   blocks — всегда половинные блоки;
#   mono   — один знак на знакомест, работает в любом терминале.
graphics = auto
# Заставка: имя рисунка без расширения из `golden/render`.
# По умолчанию КОТ — криволинейный рисунок из `graf.pas`.
# Можно указать свой (`СЕТ`, `ЧАС`, `ПТИ`), без расширения.
# Значение `none` — заставка без картинки, только текст.
# Заставка закрывается при первом вводе или первом байте от ЭВМ.
splash = КОТ
# Глубина истории выданных на экран строк: сколько строк, сошедших с
# кадра, помнится и доступно колесом мыши и клавишами ЭКРАН↑/ЭКРАН↓.
# По умолчанию 1000 — как в эталоне 15ИЭ.  0 — история не ведётся.
history = 1000
# Сколько шагов правки помнит Ctrl-Z в режиме АВТОНОМНО: каждый шаг —
# кадр целиком, так что глубина стоит памяти.  0 — отмена выключена.
undo = 20

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
    #: Символ конца введённой строки на линии ДКС (по умолчанию ПР = CR):
    #: по эталону 15ИЭ окончание строки на ДКС — ПР, ETX лишь знак.
    eol: int = 0o015
    #: Запрашивать ли приглашение ОС самому при подключении.  Выключено:
    #: линия должна молчать, пока оператор не нажмёт ПРПС.
    prompt: bool = False
    # [terminal]
    mode: str = 'host'
    charset: str = 'n2'
    echo: bool = True
    pen: bool = False
    grid: bool = False
    #: Ряды-легенды подвала (справка по клавишам) — как `--no-help` в 15ИЭ.
    help: bool = True
    #: Во сколько раз увеличен знак текстового слоя кадра (7×8 ПЗУ).
    #: 2 → растр 14×16, знакомест 16×20, сетка 35×20; 1 → как в ПЗУ,
    #: знакомест 8×10, сетка 70×40.  Служебные строки окна (строка
    #: состояния и подвал) выводятся тем же знаком, поэтому полоса
    #: растёт вместе с текстом.  Допустимо 1…4.
    glyph: int = 1
    # [display]
    display_mode: str = 'color'
    #: Ограничение увеличения кадра; 0 — кадр ровно по размеру окна.
    scale: int = 0
    title: bool = True
    #: Окно во весь экран, кадр вписан вместе со служебными строками.
    fullscreen: bool = True
    # Показ графики: auto — протокол графики терминала, если он есть,
    # иначе псевдографика; kitty — требовать протокол; blocks — всегда
    # половинные блоки; mono — один знак на знакомест.
    graphics: str = 'auto'
    # Заставка: имя рисунка без расширения из `golden/render`
    # (по умолчанию КОТ).  `none` — без картинки.
    # Закрывается при первом вводе или первом байте от ЭВМ.
    splash: str = 'КОТ'
    # Глубина истории выданных строк (`[display] history`), как в 15ИЭ.
    history: int = DEFAULT_HISTORY
    # Сколько шагов правки помнит `Ctrl-Z` (`[display] undo`).
    undo: int = DEFAULT_UNDO
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
            self.eol = parse_eol(p.get('line', 'eol', fallback=None),
                                 self.eol)
            self.prompt = p.getboolean('line', 'prompt',
                                       fallback=self.prompt)
        if p.has_section('terminal'):
            self.mode = p.get('terminal', 'mode', fallback=self.mode)
            self.charset = p.get('terminal', 'charset', fallback=self.charset)
            self.echo = p.getboolean('terminal', 'echo', fallback=self.echo)
            self.pen = p.getboolean('terminal', 'pen', fallback=self.pen)
            self.grid = p.getboolean('terminal', 'grid', fallback=self.grid)
            self.glyph = clamp_glyph(p.getint('terminal', 'glyph',
                                              fallback=self.glyph))
            self.help = p.getboolean('terminal', 'help', fallback=self.help)
        if p.has_section('display'):
            self.display_mode = p.get('display', 'mode',
                                      fallback=self.display_mode)
            self.scale = p.getint('display', 'scale', fallback=self.scale)
            self.title = p.getboolean('display', 'title', fallback=self.title)
            self.fullscreen = p.getboolean('display', 'fullscreen',
                                           fallback=self.fullscreen)
            self.splash = p.get('display', 'splash', fallback=self.splash)
            self.history = p.getint('display', 'history',
                                    fallback=self.history)
            self.undo = p.getint('display', 'undo', fallback=self.undo)
            self.graphics = p.get('display', 'graphics', fallback=self.graphics)
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