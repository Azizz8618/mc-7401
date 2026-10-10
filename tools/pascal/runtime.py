"""
runtime.py — исполняющая среда для транслированных программ.

Модуль предоставляет всё, что диалект Паскаль-Монитор ожидает от
окружения, в котором выполняется программа терминала «Электроника
МС 7401»:

* файловые переменные `INР` (ввод с клавиатуры терминала) и `OUTRUT`
  (вывод в терминал) с операциями `GЕТ`, `RЕSЕТ`, `RЕWРИТЕ`, `ВIND`;
* строки диалекта — списки символов (тип `ALFA`), символ — `Char`;
* стандартные функции: `СНR`, `ОRD`, `САRD`, `SЕL`, `INS`, `UPRSK` и пр.;
* макрос `СОDЕ` — экстракоды компилятора: все, кроме влияющих на поток
  команд терминала, превращаются в пустые операции;
* формирование потока в линию: буфер `OUTRUT` и кадрирование по
  протоколу ВУК (`ВЫД`/`РИС`).

Подробности восстановления протокола — в `docs/COMMANDS.md`.
"""

from __future__ import annotations

import math
import random
from typing import Any

# ─────────────────────────────────────────────────────────────────────────────
# Константы диалекта
# ─────────────────────────────────────────────────────────────────────────────

END = 255                     # ≡377 — маркер конца ввода/текста
VK = 0o125                    # ВК — конец команды
AR2 = 0o124                   # АР2 — префикс специальной функции
S0 = 0o160
S1 = 0o161
LF = 0o175

TRUE = True
FALSE = False

#: Фиксированная дата для экстракода `Э050114` («дата, 6 знаков»).
DATE_TEXT = '050185'

#: Порог кадрирования ВУК: при `ВЫД(FL)` блок уходит в линию, только
#: если в буфере не меньше указанного числа байт (`М<300В` в оригинале).
FLUSH_LIMIT = 0o300           # 192

#: Признак кадра и длина служебной записи канала ВУК.
VUK_MARK = 0o172              # 'z'
VUK_HEADER_LEN = 20


# ─────────────────────────────────────────────────────────────────────────────
# Символы и множества
# ─────────────────────────────────────────────────────────────────────────────

#: Порядок русского алфавита — для множеств вида `РУСЛИТ='А'..'Я'`.
RU_ALPHABET = 'АБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ'

#: Все буквы (латинские и русские) — множество `LIТ='А'..'Z'` в оригинале
#: задаёт именно «буквы»; один из концов диапазона при распознавании
#: оригинала оказался кириллическим.
ALL_LETTERS = frozenset(range(0x41, 0x5B)) | frozenset(
    ord(c) for c in RU_ALPHABET)

#: Отображение символов, отсутствующих в семибитном алфавите терминала.
CHAR_MAP = {
    '≠': 0o256,      # `НЕР` — инвертирующее перо
    '~': 0o176,      # ‘~’ — знак «тильда» КОИ-8
}


def to_wire_char(c: str) -> int:
    """Код символа в алфавите терминала (КОИ-8).

    Терминал — растровое табло с КОИ-8 (`mc7401/chars.py`), поэтому
    кириллица исходника должна попадать в линию как КОИ-8, а не как
    младшие восемь бит кода Unicode.  Латиница, цифры и знаки
    препинания в КОИ-8 совпадают с ASCII, поэтому командами потока
    ничего не портится.
    """
    if c in CHAR_MAP:
        return CHAR_MAP[c]
    try:
        return c.encode('koi8-r')[0]
    except UnicodeEncodeError:
        return ord(c) & 0xFF


def _is_cyrillic(code: int) -> bool:
    return 0x410 <= code <= 0x44F


def _is_letter_char(ch: str) -> bool:
    return len(ch) == 1 and (('A' <= ch <= 'Z') or ch in RU_ALPHABET)


class Char(str):
    """Символ диалекта.

    В исходниках символ — односимвольная строка (`INР↑`), а строковая
    константа — массив символов (`ALFA`).  Тип сравнивается и со
    строкой, и с одноэлементным списком, и с кодом символа.
    """

    __slots__ = ()

    def __eq__(self, other) -> bool:
        if isinstance(other, str):
            return str.__eq__(self, other)
        if isinstance(other, (list, tuple)):
            return len(other) == 1 and str.__eq__(self, other[0])
        if isinstance(other, int):
            return len(self) == 1 and ord(self) == other
        return NotImplemented

    def __ne__(self, other) -> bool:
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self) -> int:
        return str.__hash__(self)


def _one(x):
    """Привести значение к одному символу/числу."""
    if isinstance(x, (list, tuple)):
        return x[0] if x else 0
    return x


def _set(x):
    """Привести значение к множеству (`SET OF …`)."""
    if isinstance(x, set):
        return x
    if isinstance(x, int):
        return set(range(0, 0x100))
    if isinstance(x, (list, tuple)):
        out: set = set()
        for item in x:
            if isinstance(item, (list, tuple, set, frozenset)):
                out.update(_set(item))
            elif item:
                out.add(ord(item) if isinstance(item, str) else item)
        return out
    return {x}


def _range(lo, hi):
    """Диапазон множества: `[1..5]`, `'А'..'Я'`, `'0'..'9'`.

    Диапазон букв распознаётся по обоим концам.  Если один конец —
    кириллическая буква, а другой латинская (`LIТ='А'..'Z'`), берётся
    множество всех букв.
    """
    lo, hi = _one(lo), _one(hi)
    a = ord(lo) if isinstance(lo, str) else lo
    b = ord(hi) if isinstance(hi, str) else hi
    if b < a:                      # `LIТ='А'..'Z'`: концы переставлены
        a, b = b, a
    if (isinstance(lo, str) and isinstance(hi, str)
            and _is_letter_char(lo) and _is_letter_char(hi)):
        if _is_cyrillic(a) and _is_cyrillic(b):
            i = RU_ALPHABET.index(lo)
            j = RU_ALPHABET.index(hi)
            return {ord(c) for c in RU_ALPHABET[i:j + 1]}
        if not _is_cyrillic(a) and not _is_cyrillic(b):
            return set(range(a, b + 1))
        return set(ALL_LETTERS)
    return set(range(a, b + 1))


def _in(item, container) -> bool:
    """Операция `IN` диалекта."""
    item = _one(item)
    if isinstance(container, set):
        if isinstance(item, str):
            return ord(item) in container
        return item in container
    return item in _set(container)


class _InputExhausted(Exception):
    """Оператор не ввёл ни одного символа.

    Настоящий Монитор-80 в этой точке ждал бы нажатия клавиши; при
    эмуляции программа прерывается на последнем выведенном кадре.
    """


def hchar(ch):
    """Символ как есть (заглушка для будущих раскладок)."""
    return ch


# ─────────────────────────────────────────────────────────────────────────────
# Файловые переменные
# ─────────────────────────────────────────────────────────────────────────────

class PasFile:
    """Текстовый файл диалекта: поток символов и указатель."""

    def __init__(self, name: str = ''):
        self.name = name
        self.data: list[str] = []
        self.pos = 0

    @property
    def cur(self) -> Char:
        """Текущий символ (`f^`); за концом потока — `ЕND`."""
        if self.pos < len(self.data):
            return Char(self.data[self.pos])
        return Char(chr(END))

    def get(self) -> Char:
        """`GЕТ(f)` — перейти к следующему символу, вернуть прежний.

        За концом входного потока настоящий терминал блокируется на
        ожидании клавиатуры оператора.  В эмуляции блокировку моделировать
        нечем, поэтому выдаётся :class:`_InputExhausted`: программа
        прерывается на том кадре, который был выведен последним.
        """
        old = self.cur
        if self.pos >= len(self.data):
            raise _InputExhausted()
        self.pos += 1
        return old

    def reset(self) -> None:
        self.pos = 0

    def put(self, data) -> None:
        write_raw(self, data)


def to_chars(value) -> list[str]:
    """Привести значение к списку символов по правилам `WRITЕ`."""
    if value is None:
        return []
    if isinstance(value, bool):
        return [chr(END)]
    if isinstance(value, int):
        # `WRITЕ(Х:0)` выводит число десятичной записью; символы
        # передаются в исходнике строковыми константами.
        return list(str(value))
    if isinstance(value, float):
        return list(str(value))
    if isinstance(value, str):
        # Символы хранятся как есть: кодирование в алфавит терминала
        # выполняется при выдаче в линию (`Terminal.flush`).  Иначе
        # рабочий файл программы (`INР` в `ЗАГОЛ`) содержал бы байты
        # КОИ-8, и проверка `INР↑_IN РУСЛИТ` перестала бы работать.
        return list(value)
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            out.extend(to_chars(item))
        return out
    return list(str(value))


def write_raw(f: PasFile, data) -> None:
    """Записать значение в файл (для `OUTRUT` — в буфер терминала)."""
    buf = _output.data if f is _output else f.data
    buf.extend(to_chars(data))


# ─────────────────────────────────────────────────────────────────────────────
# Состояние терминала на стороне программы
# ─────────────────────────────────────────────────────────────────────────────

class Terminal:
    """Буфер `OUTRUT` и формирование потока в линию."""

    def __init__(self):
        self.data: list[str] = []            # текущий буфер OUTRUT
        self.wire = bytearray()              # то, что уходит в терминал
        self.byte_count = 0

    def clear(self) -> None:
        self.data.clear()

    def __len__(self) -> int:
        return len(self.data)

    def flush(self, final: bool) -> bool:
        """Аналог `ВЫД(S)` / `РИС(S)`.

        Пустой буфер игнорируется; при `S = FALSE` блок ждёт, пока
        наберётся не менее `FLUSH_LIMIT` байт.  Иначе кадр уходит в
        линию с обрамлением канала ВУК.
        """
        n = len(self.data)
        if n == 0:
            return False
        if not final and n < FLUSH_LIMIT:
            return False
        self.byte_count += n
        self.wire += ord('/')
        self.wire += VK
        self.wire += VUK_MARK
        self.wire += bytes(VUK_HEADER_LEN - 1)     # хвост служебной записи
        for ch in self.data:
            self.wire.append(to_wire_char(ch))
        self.clear()
        return True

    def push(self) -> int:
        """Передать накопленный буфер в линию (экстракод `ЛСVУК`).

        В оригинале содержимое `OUTRUT` копируется в терминал набором
        экстракодов; здесь — тем же потоком байтов.
        """
        n = len(self.data)
        for ch in self.data:
            self.wire.append(to_wire_char(ch))
        return n

    def frame_end(self) -> None:
        """Маркер `АР2 'F‾' ВК` в начало следующего блока (см. `ВЫД`)."""
        self.wire += bytes((AR2, ord('F'), 0x7E, VK))


_terminal = Terminal()
_output = PasFile('OUTRUT')
# Буфер файла `ОUТРUТ` и буфер терминала — один и тот же объект:
# `ВЫД`/`РИС` выдают в линию накопленное содержимое `OUTRUT`.
_output.data = _terminal.data
_input = PasFile('INP')

#: Гласные для `ФИМЯ` (см. `graf.pas`).
_VOWELS = (
    'ОБУЧЕНИИОПЫТНЫХПРОГРАММИСТОВТАКЖЕИМЕЮТСЯСИНТАКСИЧЕСКИЕ'
    'СЕМАНТИЧЕСКИЕАСПЕКТЫКУРСЯРАЗРАБОТКЕАЛГОРИТМОВСОСРЕДОТОЧЕНЫ'
    'ЗНАНИЯХПОДРОБНОСТИНИХСТАРАЮТСЯВЫДЕЛИТЬОТДЕЛЬНЫЕОБСУЖДЕНИЯВО'
    'ОБЩЕОПУСТИТЬКУРСАХОБУЧЕНИЯВТОРОМУТРЕТЬЕМУЯЗЫКУПРОГРАММИРОВ'
    'АНИЯОСНОВНОЕВНИМАНИЕУДЕЛЯЕТСЯЭКВИВАЛЕНТАМИЗВЕСТНЫХСЛУШАТЕЛЯ'
    'МПОНЯТИЙСОЧЛИТРЕБОВАНИЕИСПЫТУЕМЫМЗАДАВАТЬВОПРОСЫТАКОМПОГРАН'
    'ИЧНОМФОРМАТЕПОЗВОЛЯЕТИЗМЕРИТЬСПОСОБНОСТЬРЕШАТЬЗАДАЧИФОРМУ'
    'ЛИРУЯЗАПРОСЫИССЛЕДОВАТЬСИСТЕМЫЕСТЕСТВЕННЫМЯЗЫКОМСЛЕДУЮЩ'
    'ЕТЩАТЕЛЬНОУЧИТЫВАЯПСИХОЛОГИЧЕСКИЕПРИНЦИПЫПОЛЬЗОВАТЕ')

_rand = random.Random(19850523)


# ─────────────────────────────────────────────────────────────────────────────
# Экстракоды `СОDЕ`
# ─────────────────────────────────────────────────────────────────────────────

def code_value(text: str) -> int:
    """Значение, подставляемое макросом `СОDЕ(<text>)` вместо `V:=;`.

    Распознаются макросы, задающие границы буфера вывода (`СЧ75224` —
    конец, `СЧ75205` — начало) и текущее время; остальные дают 0.
    """
    t = text.upper()
    if 'Ч75224' in t or 'У75224' in t:
        return len(_terminal)
    if 'Ч75205' in t or 'У75205' in t:
        return 0
    if 'Э05310' in t:
        return 36000                      # фиксированное «текущее время»
    return 0


def code_random() -> float:
    """Макрос `RANDОM` — псевдослучайное число в (0, 1)."""
    return _rand.random()


def _code(text: str) -> None:
    """Макрос `СОDЕ(…)`.

    Большинство экстракодов к потоку команд терминала отношения не имеют
    и превращаются в пустые операции.  Исключение — `СОDЕ(ПБ76002=)`:
    возврат из программы по команде «`ВЫХ`».
    """
    if '76002' in text:
        raise SystemExit(0)
    # `ЛСVУК` — запись накопленного буфера `OUTRUT` в терминал.
    if 'ЛСVУК' in text or 'ЛСУК' in text:
        _terminal.push()


# ─────────────────────────────────────────────────────────────────────────────
# Управление потоком выполнения
# ─────────────────────────────────────────────────────────────────────────────

class _ExitRoutine(Exception):
    """`ЕХIТ` — выход из текущей подпрограммы.

    Исключение несёт номер подпрограммы: `ЕХIТ` завершает только её,
    но не внешние вызовы (как в оригинале).
    """

    def __init__(self, rid: int = 0):
        super().__init__(rid)
        self.rid = rid


class _Goto(Exception):
    """Переход `GОТО` к метке."""

    def __init__(self, n: int = 0):
        super().__init__(n)
        self.n = n


def EXIT(rid: int = 0) -> None:
    raise _ExitRoutine(rid)


def HALT(*args) -> None:
    raise SystemExit(0)


# ─────────────────────────────────────────────────────────────────────────────
# Стандартные функции диалекта
# ─────────────────────────────────────────────────────────────────────────────

def CHR(n) -> str:
    return chr(int(n) & 0xFF)


def ORD(c) -> int:
    c = _one(c)
    return ord(c) if c else 0


def CARD(x) -> int:
    """`САRD` — мощность (размер строки, множества) или модуль числа."""
    if isinstance(x, (list, tuple, str, set, frozenset, dict)):
        return len(x)
    return abs(int(x))


LENGTH = CARD


def SEL(value, pos, count) -> int:
    """`SЕL(X, I, N)` — выделить N битов числа X, начиная с I-го."""
    return (int(value) >> int(pos)) & ((1 << int(count)) - 1)


def INS(s, index, count, ch) -> list:
    """`INS(S, I, N, C)` — вставить N символов C на позицию I (с 1)."""
    s = list(s)
    pad = to_chars(ch)[:1] or [' ']
    idx = max(0, int(index) - 1)
    for _ in range(int(count)):
        s.insert(idx, pad[0])
    return s


def DELETE(s, index, count) -> list:
    s = list(s)
    idx = max(0, int(index) - 1)
    del s[idx:idx + int(count)]
    return s


def POS(sub, s) -> int:
    a = ''.join(to_chars(sub))
    b = ''.join(to_chars(s))
    return b.find(a) + 1


def CONCAT(*args) -> list:
    out: list[str] = []
    for a in args:
        out.extend(to_chars(a))
    return out


def UPRSK(s) -> list:
    """Привести строку к верхнему регистру (`UNРСК`)."""
    return [c.upper() for c in to_chars(s)]


def ABS(x):
    return abs(x)


def SQR(x):
    return x * x


def SQRT(x) -> float:
    return math.sqrt(x)


def EXP(x) -> float:
    return math.exp(x)


def SIN(x) -> float:
    return math.sin(x)


def COS(x) -> float:
    return math.cos(x)


def ARCSIN(x) -> float:
    return math.asin(x)


def ARCCOS(x) -> float:
    return math.acos(x)


def ARCTAN(x) -> float:
    return math.atan(x)


def LN(x) -> float:
    return math.log(x)


def ROUND(x) -> int:
    """Округление к ближайшему целому (как в оригинале)."""
    return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))


def TRUNC(x) -> int:
    return int(x)


def ODD(n) -> bool:
    return int(n) % 2 != 0


def SUCC(n):
    return n + 1


def PRED(n):
    return n - 1


def int_trunc(x):
    """Целочисленное деление и остаток как в Паскале (`DIV`, `MOD`)."""
    return int(x)


# ─────────────────────────────────────────────────────────────────────────────
# Операции над вводом-выводом
# ─────────────────────────────────────────────────────────────────────────────

def WRITE(*args) -> None:
    """`WRITЕ(…)` — записать в текущий файл (по умолчанию `OUTRUT`)."""
    f = _output
    if args and isinstance(args[0], PasFile):
        f, args = args[0], args[1:]
    for a in args:
        write_raw(f, a)


def WRITELN(*args) -> None:
    WRITE(*args)
    write_raw(_output, '\n')


def PRINT(*args) -> None:
    print(*(''.join(to_chars(a)) for a in args))


def GET(f: PasFile) -> Char:
    return f.get()


def RESET(f: PasFile) -> None:
    f.reset()


def REWRITE(f: PasFile) -> None:
    if f is _output:
        _terminal.clear()
    else:
        f.data.clear()
        f.pos = 0


def CLOSE(f: PasFile) -> None:
    pass


def BIND(s) -> None:
    """`ВIND` — вывести строку-подсказку в терминал."""
    WRITE(s)


def FLUSH(*args) -> None:
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Пространство имён для транслированной программы
# ─────────────────────────────────────────────────────────────────────────────

NAMESPACE: dict[str, Any] = {
    'TRUE': True, 'FALSE': False, 'END': END, 'VK': VK, 'AR2': AR2,
    'S0': S0, 'S1': S1, 'LF': LF, 'DATE_TEXT': DATE_TEXT,
    'Char': Char, 'PasFile': PasFile,
    'CHR': CHR, 'ORD': ORD, 'CARD': CARD, 'LENGTH': LENGTH, 'SEL': SEL,
    'INS': INS, 'DELETE': DELETE, 'POS': POS, 'CONCAT': CONCAT,
    'UPRSK': UPRSK, 'UPPERCASE': UPRSK,
    'ABS': ABS, 'SQR': SQR, 'SQRT': SQRT, 'EXP': EXP, 'SIN': SIN,
    'COS': COS, 'ARCSIN': ARCSIN, 'ARCCOS': ARCCOS, 'ARCTAN': ARCTAN,
    'LN': LN, 'ROUND': ROUND, 'TRUNC': TRUNC, 'ODD': ODD,
    'SUCC': SUCC, 'PRED': PRED, 'int_trunc': int_trunc,
    'WRITE': WRITE, 'WRITELN': WRITELN, 'PRINT': PRINT, 'PRINTLN': PRINT,
    'GET': GET, 'RESET': RESET, 'REWRITE': REWRITE, 'CLOSE': CLOSE,
    'BIND': BIND, 'FLUSH': FLUSH, 'HALT': HALT, 'EXIT': EXIT,
    'code_value': code_value, 'code_random': code_random, '_code': _code,
    '_set': _set, '_range': _range, '_in': _in, '_one': _one,
    'hchar': hchar, 'ALL_LETTERS': ALL_LETTERS, 'RU_ALPHABET': RU_ALPHABET,
    '_Goto': _Goto, '_ExitRoutine': _ExitRoutine,
    '_input': _input, '_output': _output, '_terminal': _terminal,
}


def reset() -> None:
    """Сбросить состояние терминала между прогонами."""
    _terminal.clear()
    del _terminal.wire[:]
    _terminal.byte_count = 0
    _input.data.clear()
    _input.pos = 0


def make_program(source: str, input_text: str = '',
                 entry: str = '__main__') -> dict:
    """Выполнить транслированную программу.

    `source` — текст на Python, полученный компилятором `compiler`.
    Возвращает словарь с потоком в линию и статистикой.
    """
    reset()
    ns = dict(NAMESPACE)
    exec(compile(source, '<graf.pas>', 'exec'), ns)
    _input.data = list(input_text)
    _input.pos = 0
    func = ns.get(entry)
    if func is None:
        raise KeyError(f'точка входа {entry!r} не найдена')
    try:
        func()
    except _ExitRoutine:
        pass
    except _Goto as g:
        log_unreached(g)
    except _InputExhausted:
        # Оператор не ввёл команду: программа останавливается на кадре,
        # который был выведен последним (см. _InputExhausted).
        log.info('ввод исчерпан — программа прервана на последнем кадре')
    _terminal.flush(True)
    return {
        'wire': bytes(_terminal.wire),
        'bytes': _terminal.byte_count,
        'ns': ns,
        'terminal': _terminal,
    }


def log_unreached(goto) -> None:
    """Сообщить о переходе за пределы программы (дефект оригинала)."""
    import logging
    logging.getLogger(__name__).warning(
        'переход GОТО %s не достигнут: метка вне программы', goto.n)