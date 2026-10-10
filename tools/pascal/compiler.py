"""
compiler.py — транслятор диалекта Паскаль-Монитор (исходники `graf.pas`)
в язык Python.

Диалект — «Паскаль для терминала Электроника МС 7401»: подмножество
Паскаля с экстракодами `СОDЕ`, макросами ввода-вывода `ВЫД`/`РИС` и
файлами `INР` (ввод) / `OUTRUT` (вывод в терминал).

Исходники `graf.pas` и `graf1.pas` получены распознаванием с оригинала
и содержат типографику, характерную для машинного набора 1985 года:

* пробел заменён подчёркиванием ``_``;
* кириллические буквы-омоглифы вперемешку с латиницей
  (``WRITЕ``, ``РRОСЕD``, ``FUNСТI``, ``ДО``);
* символ apostrophe заменён на типографский ``’``;
* восьмеричные константы через префикс ``≡`` (``≡125`` = 0o125 = 85).

Перед разбором текст нормализуется, после чего строится AST и
порождается текст программы на Python.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger('mc7401.pascal')

# ─────────────────────────────────────────────────────────────────────────────
# Нормализация
# ─────────────────────────────────────────────────────────────────────────────

#: Кириллические буквы-омоглифы → латинские (по начертанию).
HOMOGLYPHS = {
    'А': 'A', 'В': 'B', 'Е': 'E', 'К': 'K', 'М': 'M', 'Н': 'H', 'О': 'O',
    'Р': 'P', 'С': 'C', 'Т': 'T', 'У': 'Y', 'Х': 'X', 'Ё': 'E',
    'а': 'a', 'е': 'e', 'о': 'o', 'р': 'p', 'с': 'c', 'у': 'y', 'х': 'x',
    'ё': 'e', 'І': 'I', 'Ј': 'J', 'Ѕ': 'S',
    'і': 'i', 'ј': 'j', 'ѕ': 's',
    # остальные кириллические буквы (для слов-идентификаторов)
    'Б': 'B', 'Г': 'G', 'Д': 'D', 'З': 'Z', 'И': 'I', 'Л': 'L', 'П': 'P',
    'Ф': 'F', 'Ц': 'C', 'Ч': 'CH', 'Ш': 'SH', 'Щ': 'SCH', 'Ы': 'Y',
    'Ъ': "'", 'Ь': "'", 'Э': 'E', 'Ю': 'YU', 'Я': 'YA', 'Ж': 'ZH',
    'б': 'b', 'г': 'g', 'д': 'd', 'з': 'z', 'и': 'i', 'л': 'l', 'п': 'p',
    'ф': 'f', 'ц': 'c', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch', 'ы': 'y',
    'ъ': "'", 'ь': "'", 'э': 'e', 'ю': 'yu', 'я': 'ya', 'ж': 'zh',
}

_COMMENT_RE = re.compile(r'\(\*.*?\*\)|\{.*?\}', re.S)
_OCTAL_CHAR_RE = re.compile(r'≡([0-7]{1,3})')


def _octal_char(m: re.Match) -> str:
    return chr(int(m.group(1), 8) & 0xFF)


#: Исправления дефектов распознавания оригинала.
#:
#: Применяются к нормализованному тексту до лексического анализа.
#: Каждая правка — следы потери символов при OCR и оговорена в JOURNAL.md.
REPAIRS: tuple[tuple[str, str, str], ...] = (
    # `ТАКЕ` набирает число: в оригинале потеряно вычитание кода '0'.
    # Без него `К:=К*10+ОRD(c)` даёт 49, 540, 5400… — число никогда не
    # попадает в заданный диапазон и программа зацикливается.
    ('К:=К*10+ОRD(INРUТ↑)', "К:=К*10+ОRD(INРUТ↑)-48",
     'ТАКЕ: вычитание кода "0"'),
    ('К:=К*10+ОRD(INР↑)', "К:=К*10+ОRD(INР↑)-48",
     'ТАКЕ: вычитание кода "0"'),
)


def apply_repairs(text: str) -> tuple[str, list[str]]:
    """Исправить дефекты распознавания; вернуть текст и список правок."""
    applied = []
    for src, dst, note in REPAIRS:
        if src in text:
            text = text.replace(src, dst)
            applied.append(note)
    return text, applied


def preprocess(text: str) -> str:
    """Нормализовать текст исходника."""
    text = text.replace('’', "'").replace('‘', "'")
    text = text.replace('„', '"').replace('“', '"')
    text = _COMMENT_RE.sub(' ', text)
    # Пробел, набранный подчёркиванием, — вне строковых литералов.
    out = []
    in_str = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "'":
            # строковый литерал
            j = i + 1
            buf = ["'"]
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        buf.append("''")
                        j += 2
                        continue
                    break
                # В строковых литералах подчёркивание также заменяет
                # пробел (`’_ВСЕГО_=_’` → « ВСЕГО = »).
                buf.append(' ' if text[j] in '_ ' else text[j])
                j += 1
            out.append(''.join(buf) + "'")
            i = j + 1
            in_str = False
            continue
        out.append(' ' if ch == '_' else ch)
        i += 1
    text = ''.join(out)
    text = _OCTAL_CHAR_RE.sub(_octal_char, text)
    # Буквы Ъ/Ь в исходнике употребляются только в метках экстракодов
    # `СОDЕ`; заменяем их на букву, чтобы не путать с апострофом.
    text = text.replace('Ъ', 'Y').replace('Ь', 'Y')
    text, applied = apply_repairs(text)
    if applied:
        log.debug('применены правки: %s', '; '.join(applied))
    return text


def canon(word: str) -> str:
    """Каноническая (латинская) форма идентификатора — для ключевых слов."""
    return ''.join(HOMOGLYPHS.get(c, c) for c in word).upper()


# ─────────────────────────────────────────────────────────────────────────────
# Лексический анализ
# ─────────────────────────────────────────────────────────────────────────────

TOK_NUM, TOK_STR, TOK_ID, TOK_OP, TOK_EOF = 'num', 'str', 'id', 'op', 'eof'

_OPERATORS = [
    ':=', '<=', '>=', '<>', '..', '=',
    '(', ')', '[', ']', ',', ';', ':', '.', '+', '-', '*', '/', '<', '>',
    '^', '↑', '@', '≈', '≤', '≥',
]
_OPERATORS.sort(key=len, reverse=True)

#: Символы, заменяемые ключевыми словами при лексическом анализе.
SYMBOL_KEYWORDS = {
    '∧': 'AND', '∨': 'OR', '¬': 'NOT',
}
SYMBOL_OPERATORS = {
    '≠': '<>', '≥': '>=', '≤': '<=', '≈': '=',
}

#: Числовая константа диалекта: десятичная, вещественная либо восьмеричная
#: с суффиксом `В`/`Т`/`С` (`54В`, `01200002Т`, `60740103607417С`).
_NUM_RE = re.compile(r'\d+(?:\.\d+)?(?:[eE][-+]?\d+)?[ВвТтСс]?')
_ID_RE = re.compile(r'[^\W\d_][\w]*', re.UNICODE)
class Token:
    __slots__ = ('kind', 'text', 'pos', 'value')

    def __init__(self, kind: str, text: str, pos: int, value: Any = None):
        self.kind = kind
        self.text = text
        self.pos = pos
        self.value = value

    def __repr__(self) -> str:  # pragma: no cover
        return f'Token({self.kind},{self.text!r})'


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "'":
            j = i + 1
            buf = []
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        buf.append("'")
                        j += 2
                        continue
                    break
                buf.append(text[j])
                j += 1
            s = ''.join(buf)
            tokens.append(Token(TOK_STR, s, i, s))
            i = j + 1
            continue
        m = _NUM_RE.match(text, i)
        if m and (ch.isdigit()):
            raw = m.group(0)
            pos = i
            tokens.append(Token(TOK_NUM, raw, pos))
            i = m.end()
            continue
        m = _ID_RE.match(text, i)
        if m:
            tokens.append(Token(TOK_ID, m.group(0), i))
            i = m.end()
            continue
        for op in _OPERATORS:
            if text.startswith(op, i):
                tokens.append(Token(TOK_OP, op, i))
                i += len(op)
                break
        else:
            if ch in SYMBOL_KEYWORDS:
                tokens.append(Token(TOK_ID, SYMBOL_KEYWORDS[ch], i))
                i += 1
                continue
            if ch in SYMBOL_OPERATORS:
                tokens.append(Token(TOK_OP, SYMBOL_OPERATORS[ch], i))
                i += 1
                continue
            raise SyntaxError(f'непознанный символ {ch!r} в позиции {i}')
    tokens.append(Token(TOK_EOF, '', n))
    return tokens


# ─────────────────────────────────────────────────────────────────────────────
# Ключевые слова
# ─────────────────────────────────────────────────────────────────────────────

KEYWORDS = {
    'PROGRAM', 'LABEL', 'CONST', 'TYPE', 'VAR', 'PROCED', 'FUNCTI', 'FUNC',
    'BEGIN', 'END', 'IF', 'THEN', 'ELSE', 'CASE', 'OF', 'SELECT', 'WHILE',
    'DO', 'REPEAT', 'UNTIL', 'FOR', 'TO', 'DOWNTO', 'AND', 'OR', 'NOT',
    'XOR', 'DIV', 'MOD', 'IN', 'EXIT', 'GOTO', 'RECORD', 'ARRAY', 'SET',
    'OTHERWISE',
}

#: Имена типов, отображаемые на Python-типы.
BUILTIN_TYPES = {'INT': 'int', 'INTEGER': 'int', 'REAL': 'real',
                 'BOOL': 'bool', 'BOOLEAN': 'bool', 'CHAR': 'char',
                 'ALFA': 'alfa', 'TEXT': 'file', 'ARSTRING': 'alfa'}


# ─────────────────────────────────────────────────────────────────────────────
# AST
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Program:
    name: str = ''
    consts: dict = field(default_factory=dict)
    nested: list = field(default_factory=list)
    types: dict = field(default_factory=dict)
    vars: dict = field(default_factory=dict)
    procedures: list = field(default_factory=list)


@dataclass
class Routine:
    name: str
    params: list          # [(имя, тип, 'var'|'value')]
    locals: dict          # имя → тип-описание
    body: list
    is_function: bool = False
    result_type: Any = None
    result_name: str = ''
    labels: set = field(default_factory=set)
    consts: dict = field(default_factory=dict)
    nested: list = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Разбор
# ─────────────────────────────────────────────────────────────────────────────

class Parser:
    def __init__(self, text: str):
        self.tokens = tokenize(text)
        self.i = 0
        self.prog = Program()
        #: стек видимых переменных: имя → описание типа
        self.scopes: list[dict] = [self.prog.vars]
        self.labels: set = set()
        self.warnings: list = []
        self._stops: list[set[str]] = []
        self._no_eq = False
        self._local_consts: list[dict] = []

    # ── базовые операции ─────────────────────────────────────────────────────

    def peek(self, k: int = 0) -> Token:
        return self.tokens[min(self.i + k, len(self.tokens) - 1)]

    def at(self, text: str) -> bool:
        t = self.peek()
        if t.kind == TOK_ID:
            return canon(t.text) == text
        return t.kind == TOK_OP and t.text == text

    def accept(self, text: str) -> bool:
        if self.at(text):
            self.i += 1
            return True
        return False

    def expect(self, text: str) -> Token:
        if not self.at(text):
            t = self.peek()
            raise SyntaxError(f'ожидалось {text!r}, получено {t.text!r} '
                              f'(позиция {t.pos})')
        t = self.tokens[self.i]
        self.i += 1
        return t

    def at_ident(self) -> bool:
        return self.peek().kind == TOK_ID

    def ident(self) -> str:
        t = self.peek()
        if t.kind != TOK_ID:
            raise SyntaxError(f'ожидалось имя, получено {t.text!r} '
                              f'(позиция {t.pos})')
        self.i += 1
        return t.text

    # ── типы ─────────────────────────────────────────────────────────────────

    def type_ref(self):
        """Разобрать описание типа."""
        self.accept('↑')            # тип-указатель трактуем как сам тип
        t = self.peek()
        if t.kind == TOK_ID:
            name = canon(t.text)
            if name in ('ARRAY',):
                return self.array_type()
            if name == 'RECORD':
                return self.record_type()
            if name == 'SET':
                self.i += 1
                self.expect('OF')
                rng = None
                nxt, nxt2 = self.peek(), self.peek(1)
                if nxt.kind == TOK_NUM and nxt2.kind == TOK_OP \
                        and nxt2.text == '..':
                    lo = self.const_expr()
                    self.expect('..')
                    hi = self.const_expr()
                    rng = (lo, hi)
                else:
                    # `SET OF имя-типа` — имя может отсутствовать
                    if self.peek().kind == TOK_ID:
                        self.i += 1
                return {'kind': 'set', 'range': rng}
            if name in BUILTIN_TYPES:
                self.i += 1
                return {'kind': BUILTIN_TYPES[name]}
            self.i += 1
            if self.at('↑'):
                self.i += 1
            return {'kind': 'alias', 'name': t.text}
        raise SyntaxError(f'ожидался тип, получено {t.text!r} (позиция {t.pos})')

    def array_type(self):
        self.expect('ARRAY')
        idx = []
        if self.accept('['):
            while True:
                lo = self.const_expr()
                self.expect('..')
                hi = self.const_expr()
                idx.append((lo, hi))
                if not self.accept(','):
                    break
            self.expect(']')
        self.expect('OF')
        elem = self.type_ref()
        return {'kind': 'array', 'index': idx, 'elem': elem}

    def record_type(self):
        self.expect('RECORD')
        fields = {}
        while True:
            # `RECORD` в этом диалекте закрывается и `END`, и скобкой.
            if self.accept('END'):
                break
            if self.at(')'):
                self.i += 1
                break
            if self.at('VAR') or self.at('BEGIN') or self.at(';'):
                self.accept(';')
                continue
            names = [self.ident()]
            while self.accept(','):
                names.append(self.ident())
            self.expect(':')
            t = self.type_ref()
            self.accept(';')
            for n in names:
                fields[n] = t
        return {'kind': 'record', 'fields': fields}

    # ── константы в описаниях ───────────────────────────────────────────────

    def const_expr(self) -> Any:
        """Константное выражение (границы массива, константы раздела)."""
        left = self._const_primary()
        while True:
            t = self.peek()
            if t.kind == TOK_OP and t.text in ('+', '-', '*', '/'):
                self.i += 1
                right = self._const_primary()
                op = t.text
                if isinstance(left, str) or isinstance(right, str):
                    left = str(left)
                elif op == '+':
                    left = left + right
                elif op == '-':
                    left = left - right
                elif op == '*':
                    left = left * right
                else:
                    left = left // right if right else 0
                continue
            break
        return left

    def _const_primary(self) -> Any:
        t = self.peek()
        if t.kind == TOK_NUM:
            self.i += 1
            return parse_number(t.text)
        if t.kind == TOK_STR:
            self.i += 1
            return t.value
        if t.kind == TOK_ID:
            name = t.text
            self.i += 1
            c = canon(name)
            if c in ('TRUE', 'FALSE'):
                return c == 'TRUE'
            if c in self.prog.consts:
                return self.prog.consts[c][1]
            return name
        if t.kind == TOK_OP and t.text == '-':
            self.i += 1
            v = self._const_primary()
            return -v if not isinstance(v, str) else v
        if t.kind == TOK_OP and t.text == '+':
            self.i += 1
            return self._const_primary()
        if t.kind == TOK_OP and t.text == '(':
            self.i += 1
            v = self.const_expr()
            self.expect(')')
            return v
        raise SyntaxError(f'ожидалось константное выражение, получено '
                          f'{t.text!r} (позиция {t.pos})')

    def const_value(self):
        """Константное выражение с поддержкой множеств `X..Y`."""
        lo = self.const_expr()
        if self.peek().kind == TOK_OP and self.peek().text == '..':
            self.i += 1
            hi = self.const_expr()
            return ('setrange', lo, hi)
        return lo

    # ── программа ───────────────────────────────────────────────────────────

    def parse(self) -> Program:
        if self.accept('PROGRAM'):
            self.prog.name = self.ident()
            self.expect(';')
        if self.accept('LABEL'):
            while not self.accept(';'):
                self.i += 1
        while True:
            t = self.peek()
            if t.kind == TOK_EOF:
                break
            if t.kind == TOK_ID:
                c = canon(t.text)
                if c == 'CONST':
                    self.i += 1
                    self.parse_consts()
                    continue
                if c == 'TYPE':
                    # Раздел TYPE в этом диалекте вперемешку с разделом
                    # CONST содержит описания типов (`INТ=INТЕGЕR`) и
                    # констант (`ГРТЕР=23В`); разбирает их `parse_consts`.
                    self.i += 1
                    self.parse_consts()
                    continue
                if c == 'VAR':
                    self.i += 1
                    self.parse_vars(self.scopes[-1])
                    continue
                if c in ('PROCED', 'FUNCTI', 'FUNC'):
                    self.prog.procedures.append(self.parse_routine())
                    continue
                if c == 'BEGIN':
                    self.i += 1
                    self.prog.body = self.statements()
                    self.expect('END')
                    self.expect('.')
                    break
            if t.kind == TOK_OP and t.text == '(':
                # Тело программы записано в скобках (см. `graf.pas`).
                self.i += 1
                self.prog.body = self.statements(frozenset({')'}))
                self.expect(')')
                self.accept('.')
                break
            raise SyntaxError(f'неожиданный токен {t.text!r} '
                              f'(позиция {t.pos})')
        return self.prog

    def parse_consts(self) -> None:
        """Раздел CONST.

        Имена, объявленные через `=`, могут относиться и к типам
        (`INТ=INТЕGЕR`), и к константам (`ГРТЕР=23В`); различаем их по
        тому, что стоит после `=`.
        """
        while self._at_decl():
            if self._starts_type():
                self.parse_types()
                continue
            name = self.ident()
            self.expect('=')
            value = self.const_value()
            if self._local_consts:
                self._local_consts[-1][canon(name)] = (name, value)
            self.prog.consts[canon(name)] = (name, value)
            if not self.accept(';'):
                break

    def _width_follows(self) -> bool:
        # `self.peek(0)` — сам `:`; ширина стоит сразу за ним.
        nxt = self.peek(1)
        if nxt.kind == TOK_NUM:
            return True
        return nxt.kind == TOK_OP and nxt.text in ('-', '*')

    def _at_stop(self) -> bool:
        """Достигнут ли разделитель внешнего блока операторов."""
        t = self.peek()
        return bool(self._stops) and t.kind == TOK_OP \
            and t.text in self._stops[-1]

    def _at_decl(self) -> bool:
        t, nxt = self.peek(), self.peek(1)
        return t.kind == TOK_ID and nxt.kind == TOK_OP and nxt.text == '='

    def _starts_type(self) -> bool:
        """После `=` начинается описание типа, а не константа."""
        if not (self.peek(1).kind == TOK_OP and self.peek(1).text == '='):
            return False
        t = self.peek(2)
        if t.kind == TOK_STR or t.kind == TOK_NUM:
            return False
        if t.kind == TOK_ID:
            c = canon(t.text)
            if c in ('ARRAY', 'RECORD', 'SET'):
                return True
            if c in ('TRUE', 'FALSE'):
                return False
            return c in BUILTIN_TYPES
        return False

    def parse_types(self) -> None:
        while self._at_decl() and self._starts_type():
            name = canon(self.ident())
            self.expect('=')
            self.prog.types[name] = self.type_ref()
            if not self.accept(';'):
                break

    def parse_vars(self, target: dict) -> None:
        while True:
            t = self.peek()
            nxt = self.peek(1)
            if t.kind != TOK_ID:
                break
            if not (nxt.kind == TOK_OP and nxt.text in (':', ',')):
                break
            names = [self.ident()]
            while self.accept(','):
                names.append(self.ident())
            self.expect(':')
            ty = self.type_ref()
            if self.accept('='):          # инициализация: пропускаем
                self.skip_initializer()
            for n in names:
                target[n] = ty
            if not self.accept(';'):
                break

    def skip_initializer(self) -> None:
        depth = 0
        while True:
            t = self.peek()
            if t.kind == TOK_EOF:
                return
            if t.kind == TOK_OP and t.text == '(':
                depth += 1
            elif t.kind == TOK_OP and t.text == ')':
                depth -= 1
            elif t.kind == TOK_OP and t.text == ';' and depth == 0:
                return
            self.i += 1

    def parse_routine(self) -> Routine:
        t = self.ident()
        is_func = canon(t) in ('FUNCTI', 'FUNC')
        name = self.ident()
        params = []
        if self.accept('('):
            while not self.accept(')'):
                mode = 'value'
                if self.accept('VAR'):
                    mode = 'var'
                names = [self.ident()]
                while self.accept(','):
                    names.append(self.ident())
                self.expect(':')
                ty = self.type_ref()
                for n in names:
                    params.append((n, ty, mode))
                if not self.accept(';'):
                    self.accept(',')
        result_type = None
        result_name = ''
        if is_func:
            self.expect(':')
            result_type = self.type_ref()
        self.accept(';')
        r = Routine(name=name, params=params, locals={}, body=[],
                    is_function=is_func, result_type=result_type,
                    result_name=result_name)
        scope: dict = {}
        for pname, pty, pmode in params:
            scope[pname] = pty
        self.scopes.append(scope)
        while True:
            tt = self.peek()
            if tt.kind == TOK_ID and canon(tt.text) == 'LABEL':
                self.i += 1
                while not self.accept(';'):
                    self.labels.add(int(self.const_expr()))
                continue
            if tt.kind == TOK_ID and canon(tt.text) == 'VAR':
                self.i += 1
                self.parse_vars(scope)
                continue
            if tt.kind == TOK_ID and canon(tt.text) == 'TYPE':
                self.i += 1
                self.parse_types()
                continue
            if tt.kind == TOK_ID and canon(tt.text) == 'CONST':
                self.i += 1
                self._local_consts.append({})
                self.parse_consts()
                r.consts.update(self._local_consts.pop())
                continue
            if tt.kind == TOK_ID and canon(tt.text) in ('PROCED', 'FUNCTI', 'FUNC'):
                # Вложенные подпрограммы хранятся отдельно от `VАR`:
                # `r.locals` перезаписывается таблицей объявлений.
                r.nested.append(self.parse_routine())
                continue
            break
        r.locals = scope
        before = set(self.labels)
        if self.accept('BEGIN'):
            r.body = self.statements()
            self.expect('END')
        elif self.at('('):
            self.i += 1
            r.body = self.statements(frozenset({')'}))
            self.expect(')')
        else:
            self.warnings.append(
                f'у подпрограммы {name!r} нет тела')
            r.body = []
        r.labels = {int(x) for x in (self.labels - before)
                    if isinstance(x, int)}
        # В исходнике, полученном распознаванием, встречаются лишние
        # закрывающие скобки (следы `SЕLЕСТ`-цепочки) — пропускаем их.
        while self.at(')'):
            self.i += 1
            self.warnings.append(f'лишняя скобка после {name!r}')
        self.accept(';')
        self.scopes.pop()
        return r

    # ── операторы ────────────────────────────────────────────────────────────

    def statements(self, stops: frozenset[str] = frozenset({'END'})) -> list:
        # Список операторов заканчивается первым из встреченных разделителей:
        # собственным (`END` или `)`) либо разделителем внешнего блока.
        eff = set(stops)
        if self._stops:
            eff |= self._stops[-1]
        self._stops.append(eff)
        out = []
        try:
            while True:
                t = self.peek()
                if t.kind == TOK_EOF or t.text in self._stops[-1]:
                    break
                if t.kind == TOK_ID and canon(t.text) in self._stops[-1]:
                    break
                st = self.statement()
                if st is not None:
                    out.append(st)
        finally:
            self._stops.pop()
        return out

    def statement(self):
        t = self.peek()
        if t.kind == TOK_OP and t.text == '(':
            # Метка, заданная вперёд: `( Е ) <оператор>`
            if self.peek(1).kind == TOK_ID and \
                    self.peek(2).kind == TOK_OP and self.peek(2).text == ')':
                self.i += 1
                name = self.ident()
                self.expect(')')
                self.labels.add(name)
                return ('label', name, self.statement())
            self.i += 1
            body = self.statements(frozenset({')'}))
            self.expect(')')
            return ('block', body)
        if t.kind == TOK_NUM and self.peek(1).kind == TOK_OP \
                and self.peek(1).text == ':':
            label = int(t.text)
            self.labels.add(label)
            self.i += 2
            return ('label', label, self.statement())
        if t.kind == TOK_ID:
            c = canon(t.text)
            if c == 'BEGIN':
                self.i += 1
                body = self.statements()
                self.expect('END')
                return ('block', body)
            if c == 'IF':
                return self.if_statement()
            if c == 'WHILE':
                return self.while_statement()
            if c == 'REPEAT':
                return self.repeat_statement()
            if c == 'FOR':
                return self.for_statement()
            if c in ('CASE', 'SELECT'):
                return self.case_statement()
            if c == 'GOTO':
                self.i += 1
                label = self.const_expr()
                self.accept(';')
                return ('goto', label)
            if c == 'EXIT':
                self.i += 1
                self.accept(';')
                return ('exit',)
            if c == 'CODE':
                st = self.code_statement()
                # `СОDЕ(…1 ЛС7…); GЕТ(f)` — считать символ в результат
                if (isinstance(st, list) and len(st) == 1 and
                        st[0][0] == 'code' and
                        self.peek().kind == TOK_ID and
                        canon(self.peek().text) == 'GET' and
                        self.peek(1).kind == TOK_OP and
                        self.peek(1).text == '('):
                    self.i += 2
                    fname = self.ident()
                    self.expect(')')
                    return [('code_get', fname, st[0][1])]
                return st
            if c in ('OTHERWISE',):
                self.i += 1
                return None
        # присваивание или вызов
        if t.kind == TOK_ID:
            target = self.lvalue()
            if target[0] != 'var':
                # присваивание к элементу: `U1[1]:=X1`, `ПХ.МС[J,0]:=…`
                self.expect(':=')
                if self.at(';'):
                    self.i += 1
                    return ('code_assign', '', '')
                value = self.expression()
                self.accept(';')
                return ('assign', target, value)
            name = target[1]
            if self.at(':='):
                self.i += 1
                if self.at(';'):
                    # `X:=;` — правую часть задаёт макрос `СОDЕ`.
                    self.i += 1
                    return ('code_assign', name, '')
                value = self.expression()
                self.accept(';')
                return ('assign', target, value)
            if self.peek().kind == TOK_OP and self.peek().text == '(':
                self.i += 1
                args = self.arg_list(')')
                self.accept(';')
                return ('call', name, args)
            self.accept(';')
            return ('call', name, [])
        if t.kind == TOK_OP and t.text == ';':
            self.i += 1
            return None
        raise SyntaxError(f'неожиданный токен {t.text!r} в операторе '
                          f'(позиция {t.pos})')

    def arg_list(self, closer: str) -> list:
        args = []
        if self.accept(closer):
            return args
        while True:
            args.append(self.expression())
            # ширина поля вывода: `K:0B`, `Q:1`.  Ширина — всегда число,
            # поэтому `:` внутри списка параметров подпрограммы (`VАR X:RЕАL`)
            # не путается с форматом вывода.
            while self.at(':') and self._width_follows():
                self.i += 1
                args.append(('width', self.parse_additive()))
            if self.accept(','):
                continue
            self.expect(closer)
            return args

    def if_statement(self):
        self.expect('IF')
        cond = self.expression()
        self.expect('THEN')
        then = self.statement()
        other = None
        if self.accept('ELSE'):
            other = self.statement()
        return ('if', cond, then, other)

    def while_statement(self):
        self.expect('WHILE')
        cond = self.expression()
        self.expect('DO')
        body = self.statement()
        return ('while', cond, body)

    def repeat_statement(self):
        self.expect('REPEAT')
        body = self.statements(frozenset({'UNTIL'}))
        self.expect('UNTIL')
        cond = self.expression()
        self.accept(';')
        return ('repeat', body, cond)

    def for_statement(self):
        self.expect('FOR')
        var = self.ident()
        # Начальное значение задаётся не всегда: `FОR К ТО 7 ДО …`
        start = ('const', 1) if self.at(':=') else None
        if self.accept(':='):
            start = self.expression()
        downto = False
        if self.accept('TO'):
            pass
        elif self.accept('DOWNTO'):
            downto = True
        else:
            raise SyntaxError('ожидалось TO или DOWNTO')
        limit = self.expression()
        self.expect('DO')
        body = self.statement()
        return ('for', var, start, limit, body, downto)

    def case_statement(self):
        """`САSЕ` — переключатель;  `SЕLЕСТ` — цепочка условий.

        `САSЕ N ОF 0: …; 1: … EНD` — обычный переключатель.

        `SЕLЕСТ К=77777В: ЕХIТ; К=1: …; ТR: …` — аналог цепочки
        `IF … ELSЕ IF …` (см. `ЗАХВАТ`, `КИНО`, `ПТИЦА`); метка `ТR`
        задаёт ветвь по умолчанию.
        """
        keyword = canon(self.ident())
        branches = []
        if keyword == 'CASE':
            subject = self.expression()
            self.expect('OF')
            while True:
                self.accept(';')
                if self.accept('END') or self._at_stop():
                    break
                labels = [self.parse_additive()]
                while self.accept('..'):
                    labels.append(self.parse_additive())
                while self.accept(','):
                    labels.append(self.parse_additive())
                self.expect(':')
                branches.append((labels, self.statement()))
            if self.at('END'):
                self.i += 1
            self.accept(';')
            return ('case', subject, branches)
        # SЕLЕСТ
        while True:
            self.accept(';')
            if self.at('END') or self._at_stop():
                break
            if self.at('OTHERWISE'):
                self.i += 1
                self.accept(':')
                branches.append((None, self.statement()))
                continue
            labels = [self.expression()]
            while self.accept(','):
                labels.append(self.parse_additive())
            self.expect(':')
            branches.append((labels, self.statement()))
        if self.at('END'):
            self.i += 1
        elif self._at_stop():
            self.i += 1
        self.accept(';')
        return ('case', None, branches)

    def code_statement(self) -> list:
        """`СОDЕ(...)` — макрос; возвращает список эмитируемых действий."""
        self.expect('CODE')
        self.expect('(')
        depth = 1
        start = self.i
        while depth:
            t = self.peek()
            if t.kind == TOK_EOF:
                break
            if t.kind == TOK_STR:
                self.i += 1
                continue
            if t.kind == TOK_OP:
                if t.text == '(':
                    depth += 1
                elif t.text == ')':
                    depth -= 1
            self.i += 1
        body = self.tokens[start:self.i - 1]
        text = _detokenize(body)
        out = [('code', text)]
        # `СОDЕ(...); V:=;`  — макрос задаёт значение переменной
        if self.at(';'):
            self.i += 1
            if self.peek().kind == TOK_ID and self.peek(1).kind == TOK_OP \
                    and self.peek(1).text == ':=':
                var = self.ident()
                self.expect(':=')
                if self.peek().kind == TOK_OP and self.peek().text == ';':
                    self.i += 1
                    out.append(('code_assign', var, text))
        return out

    # ── выражения ───────────────────────────────────────────────────────────

    def lvalue(self):
        """Разобрать выражение, допустимое в левой части присваивания."""
        name = self.ident()
        node = ('var', name)
        while True:
            if self.peek().kind == TOK_OP and self.peek().text == '↑':
                self.i += 1
                node = ('deref', node)
                continue
            if self.peek().kind == TOK_OP and self.peek().text == '.':
                self.i += 1
                node = ('field', node, self.ident())
                continue
            if self.peek().kind == TOK_OP and self.peek().text == '[':
                self.i += 1
                idxs = [self.expression()]
                while self.accept(','):
                    idxs.append(self.expression())
                self.expect(']')
                node = ('index', node, idxs)
                continue
            break
        return node

    def expression(self):
        return self.parse_or()

    def parse_or(self):
        left = self.parse_and()
        while self.at('OR'):
            self.i += 1
            left = ('binop', 'or', left, self.parse_and())
        return left

    def parse_and(self):
        left = self.parse_not()
        while self.at('AND'):
            self.i += 1
            left = ('binop', 'and', left, self.parse_not())
        return left

    def parse_not(self):
        if self.at('NOT'):
            self.i += 1
            return ('unop', 'not', self.parse_not())
        return self.parse_relation()

    def parse_relation(self):
        # В списке ветвей `SЕLЕСТ` знак `=` отделяет метку от оператора,
        # поэтому на время разбора условия ветви он не трактуется как
        # знак отношения.
        no_eq = self._no_eq
        self._no_eq = False
        self._local_consts: list[dict] = []
        left = self.parse_additive()
        while True:
            t = self.peek()
            if no_eq and t.kind == TOK_OP and t.text == '=':
                break
            if t.kind == TOK_OP and t.text in ('=', '<>', '<', '>', '<=', '>='):
                self.i += 1
                left = ('binop', t.text, left, self.parse_additive())
                continue
            if self.at('IN'):
                self.i += 1
                left = ('in', left, self.parse_additive())
                continue
            break
        return left

    def parse_additive(self):
        left = self.parse_multiplicative()
        while True:
            t = self.peek()
            if t.kind == TOK_OP and t.text in ('+', '-'):
                self.i += 1
                left = ('binop', t.text, left, self.parse_multiplicative())
                continue
            break
        return left

    def parse_multiplicative(self):
        left = self.parse_factor()
        while True:
            t = self.peek()
            if t.kind == TOK_OP and t.text in ('*', '/'):
                self.i += 1
                left = ('binop', t.text, left, self.parse_factor())
                continue
            if self.at('DIV'):
                self.i += 1
                left = ('binop', '//', left, self.parse_factor())
                continue
            if self.at('MOD'):
                self.i += 1
                left = ('binop', '%', left, self.parse_factor())
                continue
            break
        return left

    def parse_factor(self):
        t = self.peek()
        if t.kind == TOK_OP and t.text == '[':
            self.i += 1
            items = []
            while not self.at(']'):
                lo = self.expression()
                if self.accept('..'):
                    items.append(('range', lo, self.expression()))
                else:
                    items.append(('item', lo))
                if not self.accept(','):
                    break
            self.expect(']')
            return ('setlit', items)
        if t.kind == TOK_OP and t.text == '(':
            self.i += 1
            node = self.expression()
            self.expect(')')
            return node
        if t.kind == TOK_OP and t.text == '-':
            self.i += 1
            return ('unop', '-', self.parse_factor())
        if t.kind == TOK_OP and t.text == '+':
            self.i += 1
            return self.parse_factor()
        if t.kind == TOK_STR:
            self.i += 1
            return ('const', t.value)
        if t.kind == TOK_NUM:
            self.i += 1
            return ('const', parse_number(t.text))
        if t.kind == TOK_ID:
            c = canon(t.text)
            if c in ('TRUE', 'FALSE'):
                self.i += 1
                return ('const', c == 'TRUE')
            if c == 'NIL':
                self.i += 1
                return ('const', None)
            node = self.lvalue()
            if self.peek().kind == TOK_OP and self.peek().text == '(':
                self.i += 1
                args = self.arg_list(')')
                return ('call', node[1], args)
            return node
        raise SyntaxError(f'неожиданный токен {t.text!r} в выражении '
                          f'(позиция {t.pos})')


def _detokenize(tokens: list[Token]) -> str:
    parts = []
    for t in tokens:
        if t.kind == TOK_STR:
            parts.append(f"'{t.value}'")
        else:
            parts.append(t.text)
    return ' '.join(parts)


def parse_number(raw: str):
    """Разобрать число диалекта (восьмеричные помечены суффиксом)."""
    text = raw.strip()
    last = text[-1] if text else ''
    if last in 'ВвТтСсMmЧчB':
        digits = text[:-1]
        val = int(digits, 8) if digits else 0
        return val & 0xFFFF
    if '.' in text:
        return float(text)
    return int(text, 10)