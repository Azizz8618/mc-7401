"""
parser.py — синтаксический разбор команд терминала «Электроника МС 7401».

Запись (`ВК`-терминированная) начинается с кода функции — символа,
определяющего действие.  Параметры разделены пробелом `П1` и заданы
десятично, без знака (`Х:0`), либо как символ (`СНР(МСШ)`).

    J0 ВК                 текстовый режим
    J1 ВК                 графический режим
    0 C ВК                установить перо C          (C = '0'|'1'|'2')
    1 T ВК                установить тип линии T     (T = '0'…'7')
    2 ВК                  конец кадра
    3 ВК                  конец обновления
    4 X Y ВК              перемещение (абсолютное)
    5 dX dY ВК            перемещение (относительное)
    7 T X Y ВК            отрезок до точки (абсолютной)
    8 T dX dY ВК          отрезок относительно текущей точки
    9 S ВК ;O <текст> ВК  текст: размер S, направление O
    9 S ВК :N ВК          установка размера текста
    ] X1 Y1 X2 Y2 ВК      прямоугольник
    > T R ВК              окружность радиуса R
    + ВК                  рисующее перо (OR)
    ≠ ВК                  инвертирующее перо (XOR)
    * C ВК                стирающее перо
    K ВК                  сброс терминала
    S ВК                  очистка экрана
    LF П1 X П1 Y ВК       смещение окна
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from .constants import CMD_NOT, CMD_PLUS, P1, VK

log = logging.getLogger(__name__)


class Op(Enum):
    """Код операции терминала."""

    # управление режимом
    TEXT_MODE = auto()        # J0
    GRAPHICS_MODE = auto()    # J1
    SET_PEN = auto()          # 0 C
    SET_LINE_TYPE = auto()    # 1 T
    FRAME_END = auto()        # 2
    UPDATE_END = auto()       # 3
    RESET = auto()            # K
    CLEAR = auto()            # S
    # перемещение
    MOVE_ABS = auto()         # 4 X Y
    MOVE_REL = auto()         # 5 dX dY
    # рисование
    LINE_ABS = auto()         # 7 T X Y
    LINE_REL = auto()         # 8 T dX dY
    RECT = auto()             # ] X1 Y1 X2 Y2
    CIRCLE = auto()           # > T R
    # текст
    TEXT = auto()             # 9 S ; O <payload>
    SET_TEXT_SIZE = auto()    # 9 S : N
    # перья
    PEN_OR = auto()           # +
    PEN_XOR = auto()          # ≠
    PEN_ERASE = auto()        # * C
    # окно
    SHIFT_WINDOW = auto()     # LF X Y
    # нераспознанное
    UNKNOWN = auto()


@dataclass
class Command:
    """Разобранная команда терминала."""

    op: Op
    #: Параметры команды (числа или байты — в зависимости от операции).
    args: list = field(default_factory=list)
    #: Текстовые данные (команда `TEXT`).
    payload: bytes = b''
    #: Исходная запись целиком.
    raw: bytes = b''
    #: Человекочитаемое имя.
    name: str = ''

    def __repr__(self) -> str:  # pragma: no cover
        return (f'Command({self.op.name} {self.name!r} '
                f'args={self.args} payload={self.payload!r})')


#: Мнемоника для каждой операции (для отчётов и отладки).
OP_NAME = {
    Op.TEXT_MODE: 'J0',
    Op.GRAPHICS_MODE: 'J1',
    Op.SET_PEN: '0',
    Op.SET_LINE_TYPE: '1',
    Op.FRAME_END: '2',
    Op.UPDATE_END: '3',
    Op.RESET: 'K',
    Op.CLEAR: 'S',
    Op.MOVE_ABS: '4',
    Op.MOVE_REL: '5',
    Op.LINE_ABS: '7',
    Op.LINE_REL: '8',
    Op.RECT: ']',
    Op.CIRCLE: '>',
    Op.TEXT: '9',
    Op.SET_TEXT_SIZE: '9:',
    Op.PEN_OR: '+',
    Op.PEN_XOR: '≠',
    Op.PEN_ERASE: '*',
    Op.SHIFT_WINDOW: '}',
}


def _to_int(tok: bytes, signed: bool = True) -> Optional[int]:
    """Разобрать десятичное число; вернуть None при ошибке."""
    s = tok.decode('ascii', 'replace').strip()
    if not s:
        return None
    try:
        return int(s, 10)
    except ValueError:
        return None


class CommandParser:
    """Преобразует записи `ВК` в объекты `Command`."""

    def __init__(self, xor_code: int = CMD_NOT):
        self.xor_code = xor_code
        #: Размер текста из последней записи `9 S`.
        self.text_size = b'\x00'
        #: Статистика нераспознанных записей (для отчётов).
        self.unknown: dict[bytes, int] = {}

    # ── основной вход ────────────────────────────────────────────────────────

    def parse(self, rec: bytes) -> Command:
        body = rec[:-1] if rec.endswith(bytes((VK,))) else rec
        if not body:
            return Command(Op.UNKNOWN, raw=rec, name='')

        fn = body[0]
        rest = body[1:]

        # ── команды без параметров
        if body == b'J0':
            return Command(Op.TEXT_MODE, raw=rec, name='J0')
        if body == b'J1':
            return Command(Op.GRAPHICS_MODE, raw=rec, name='J1')
        if body == b'2':
            return Command(Op.FRAME_END, raw=rec, name='2')
        if body == b'3':
            return Command(Op.UPDATE_END, raw=rec, name='3')
        if body == b'K':
            return Command(Op.RESET, raw=rec, name='K')
        if body == b'S':
            return Command(Op.CLEAR, raw=rec, name='S')
        if body == bytes((CMD_PLUS,)):
            return Command(Op.PEN_OR, raw=rec, name='+')
        if body == bytes((self.xor_code,)):
            return Command(Op.PEN_XOR, raw=rec, name='≠')

        # ── команды с параметрами, первый символ — код функции
        if rest[:1] == bytes((P1,)):
            rest = rest[1:]
        parts = rest.split(bytes((P1,))) if rest else []

        def num(i: int) -> Optional[int]:
            return _to_int(parts[i]) if i < len(parts) else None

        if fn == ord('0'):
            return Command(Op.SET_PEN, args=[parts[0] if parts else b''],
                           raw=rec, name='0')
        if fn == ord('1'):
            return Command(Op.SET_LINE_TYPE, args=[parts[0] if parts else b''],
                           raw=rec, name='1')
        if fn == ord('*'):
            return Command(Op.PEN_ERASE,
                           args=[parts[0] if parts else b''], raw=rec, name='*')
        if fn == ord('4'):
            return Command(Op.MOVE_ABS, args=[num(0), num(1)], raw=rec, name='4')
        if fn == ord('5'):
            return Command(Op.MOVE_REL, args=[num(0), num(1)], raw=rec, name='5')
        if fn == ord('7'):
            return Command(Op.LINE_ABS,
                           args=[num(0), num(1), num(2)], raw=rec, name='7')
        if fn == ord('8'):
            return Command(Op.LINE_REL,
                           args=[num(0), num(1), num(2)], raw=rec, name='8')
        if fn == ord(']'):
            return Command(Op.RECT,
                           args=[num(0), num(1), num(2), num(3)], raw=rec, name=']')
        if fn == ord('>'):
            return Command(Op.CIRCLE, args=[num(0), num(1)], raw=rec, name='>')

        # ── команда смещения окна: `LF П1 X П1 Y ВК`
        if fn == 0o175:  # '}'
            return Command(Op.SHIFT_WINDOW,
                           args=[num(0), num(1)], raw=rec, name='}')

        # ── команда `9` — текст
        if fn == ord('9'):
            return self._parse_text(rec, rest)

        if fn == ord(';'):
            return self._parse_text_data(rec, rest)

        if fn == ord(':'):
            # `9 : N` — установка размера текста (процедура `СТРОКИ`:
            # `WRIТЕ('91', ВК, ':0 ', ВК, '; ')`).  Размер действует до
            # следующей установки и влияет на все последующие строки.
            spec = rest.strip()
            try:
                self.text_size = bytes((int(spec),))
            except ValueError:
                self.text_size = b'\x00'
            return Command(Op.SET_TEXT_SIZE, args=[self.text_size],
                           raw=rec, name='9:')

        self.unknown[body] = self.unknown.get(body, 0) + 1
        return Command(Op.UNKNOWN, args=[body], raw=rec,
                       name=body.decode('koi8-r', 'replace'))

    # ── текст ────────────────────────────────────────────────────────────────

    def _parse_text_data(self, rec: bytes, rest: bytes) -> Command:
        """Разобрать запись данных текста `;O` П1 <строка> ВК.

        Программа выводит размер и данные двумя записями
        (`WRIТЕ('9', СНР(МСШ), ВК, ';', СНР(ОРН), П1)`), поэтому размер
        берётся из предыдущей записи `9 S`, а `rest` содержит ориентацию
        и саму строку.
        """
        orient = rest[:1]
        payload = rest[1:]
        # Разделитель параметров — пробел; он часть заголовка.
        if payload[:1] == bytes((P1,)):
            payload = payload[1:]
        return Command(Op.TEXT, args=[self.text_size, orient],
                       payload=payload, raw=rec, name='9')

    def _parse_text(self, rec: bytes, rest: bytes) -> Command:
        """Разобрать команду `9`.

        Две формы:

        * `9<S>` ВК `;O` П1 <текст> ВК — вывод строки текста размером `S`
          в направлении `O`;
        * `9<S>` ВК `:N` ВК — установка размера текста (`N` после `:`).
        """
        # `rest` начинается с кода размера (одного символа) и далее
        # заканчивается первым `ВК`, который отделяет «шапку» от данных.
        head, _, tail = rest.partition(bytes((VK,)))
        # Размер запоминается сразу: запись `9 S` идёт отдельно от данных.
        self.text_size = head[:1] or self.text_size
        if not _:
            # Нет разделителя — считаем, что данных нет.
            return Command(Op.TEXT, args=[head[:1]], payload=b'',
                           raw=rec, name='9')
        size = head[:1]
        self.text_size = size

        # Форма `:N` — установка размера.
        if tail[:1] == b':':
            spec = tail[1:].strip()
            return Command(Op.SET_TEXT_SIZE, args=[size, spec], raw=rec, name='9:')

        # Форма `;O` — собственно текст.
        if tail[:1] != b';':
            return Command(Op.UNKNOWN, args=[head], raw=rec, name='9?')
        rest2 = tail[1:]
        orient = rest2[:1]
        payload = rest2[1:]
        # Внутри `;O <текст>` разделитель параметров — пробел;
        # он является частью заголовка, а не текста.
        if payload[:1] == bytes((P1,)):
            payload = payload[1:]
        return Command(Op.TEXT, args=[size, orient], payload=payload,
                       raw=rec, name='9')