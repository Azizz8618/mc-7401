"""
emitter.py — генерация текста программы на Python по AST диалекта.

Имена переменных сохраняются как есть (кириллические омоглифы
допустимы в Python 3), поэтому дополнительной транслитерации не
требуется.  Глобальные переменные программы порождаются как глобальные
имена модуля — вложенные подпрограммы видят их через замыкание.
"""

from __future__ import annotations

import keyword
import re
from typing import Any

from .compiler import (
    BUILTIN_TYPES,
    KEYWORDS,
    Program,
    Routine,
    canon,
)

_PY_KEYWORDS = set(keyword.kwlist) | {'END', 'None'}

#: Варианты имён, потерявшие буквы при распознавании оригинала.
CALL_ALIASES = {
    'WRITEL': 'WRITELN',
    'READLN': 'READLN',
    'HET': 'GET',
    'RESEТ': 'RESET',
}

#: Функции диалекта, реализуемые непосредственно в среде.
#: `DАТАL` — экстракод «дата, 6 знаков» (`СОDЕ(Э050114=…)`).
FUNCTION_STUBS = {
    'DATAL': "list(DATE_TEXT)",
}


def _py_name(name: str) -> str:
    """Привести имя к допустимому идентификатору Python."""
    if name.upper() in _PY_KEYWORDS or keyword.iskeyword(name):
        return name + '_'
    return name


class Emitter:
    def __init__(self, prog: Program, labels: set[int]):
        self.prog = prog
        self.labels = set(labels)
        self.out: list[str] = []
        self.indent = 0
        #: текущая функция (для `ЕХIТ` и макроса `СОDЕ`+`GЕТ`)
        self.routine: Routine | None = None
        self.temp = 0
        #: Порядковый номер подпрограммы — различает `ЕХIТ` (см. `_ExitRoutine`).
        self.rid = 0
        #: Имена, видимые из вложенных подпрограмм (локальные переменные
        #: внешних подпрограмм — замыкания, а не глобальные переменные).
        self.shadow_stack: list[set] = []
        #: Области видимости внешних подпрограмм (для поиска типов).
        self.scope_stack: list[dict] = []
        #: Канонические имена всех подпрограмм программы.
        self.known: set[str] = set()

    # ── вывод строк ──────────────────────────────────────────────────────────

    def w(self, text: str = '') -> None:
        self.out.append('    ' * self.indent + text if text else '')

    #: Предопределённые файловые переменные диалекта (`ОUТРUТ`, `INРUТ`).
    PREDEFINED = {'OUTPUT': '_output', 'INPUT': '_input'}

    def _predefined(self, prog: Program) -> list[tuple[str, str]]:
        """Найти файловые переменные, не объявленные в разделе `VАR`.

        В диалекте `ОUТРUТ` (выход) и `INРUТ` (ввод) — встроенные
        переменные; `graf.pas` объявляет только `INР`.
        """
        used: set[str] = set()

        def walk(node):
            if isinstance(node, (list, tuple)):
                if node and node[0] == 'var' and isinstance(node[1], str):
                    used.add(node[1])
                for item in node:
                    walk(item)
            elif isinstance(node, dict):
                for item in node.values():
                    walk(item)

        walk(prog.body)
        for r in prog.procedures:
            walk(r.body)
            for sub in r.locals.get('#procedures', []):
                walk(sub.body)
        out = []
        for name in sorted(used):
            c = canon(name)
            if c in self.PREDEFINED and name not in prog.vars:
                out.append((name, self.PREDEFINED[c]))
        return out

    def emit(self, prog: Program) -> str:
        self.w('# -*- coding: utf-8 -*-')
        self.w('# Сгенерировано tools/pascal: исходник -> Python.')
        self.w('from pascal import runtime as _rt')
        self.w('from pascal.runtime import *  # noqa: F401,F403')
        self.w('from pascal.runtime import code_value, code_random')
        self.w('')
        for _canon, (name, value) in prog.consts.items():
            self.w(f'{_const_name(name)} = {self.const_value(value)}')
        self.w('')
        for name, obj in self._predefined(prog):
            self.w(f'{_py_name(name)} = {obj}')
        # глобальные переменные
        for name, ty in prog.vars.items():
            self.w(f'{_py_name(name)} = {self.zero_value(ty, name)}')
        self.w('')
        self.w('')
        self.known, self.functions = self._routine_names(prog)
        self.varparams = self._var_params(prog)
        for r in prog.procedures:
            self.emit_routine(r)
        self.w('')
        self.w('')
        self.w('def __main__():')
        self.indent = 1
        self.routine = None
        # Переменные программы, изменяемые в основном цикле, должны быть
        # глобальными: подпрограммы видят их через `global`.
        if self.prog.vars:
            self.w('global ' + ', '.join(_py_name(v) for v in self.prog.vars))
        self.body(prog.body or [], None)
        self.indent = 1
        self.w('return None')
        self.indent = 0
        self.w('')
        self.w('')
        # Вызов выполняется только при запуске файла как программы;
        # при `exec` без `__name__` блок пропускается.
        self.w("if globals().get('__name__') == '__main__':")
        self.w('    __main__()')
        self.w('')
        return '\n'.join(self.out)


    def result_variable(self, r: Routine) -> str:
        """Имя переменной — результата функции.

        В диалекте результат не объявляется отдельно: это либо переменная
        с тем же именем, что и функция, либо первая переменная того же
        типа в разделе `VАR`; если такой нет — временная.
        """
        for pname, _pty, _mode in r.params:
            if canon(pname) == canon(r.name):
                return pname
        if canon(r.name) in {canon(k) for k in r.locals}:
            for k in r.locals:
                if canon(k) == canon(r.name):
                    return k
        want = self.resolve(r.result_type or {'kind': 'alfa'})
        for k, ty in r.locals.items():
            if k.startswith('#'):
                continue
            if self.resolve(ty) == want:
                return k
        return '_result_' + r.name

    def const_value(self, value: Any) -> str:
        if isinstance(value, tuple) and len(value) == 3 \
                and value[0] == 'setrange':
            return (f'_range({self.const_value(value[1])}, '
                    f'{self.const_value(value[2])})')
        if isinstance(value, str):
            # Строки диалекта (`ALFA`) представляются списком символов —
            # так же, как строковые переменные, и сравнимы с ними.
            return '[' + ', '.join(repr(c) for c in value) + ']'
        if isinstance(value, bool):
            return 'True' if value else 'False'
        if isinstance(value, float):
            return repr(value)
        if isinstance(value, int):
            return str(value)
        return repr(value)

    # ── начальные значения ───────────────────────────────────────────────────

    def zero_value(self, ty, name: str = '') -> str:
        ty = self.resolve(ty)
        kind = ty.get('kind')
        if kind == 'array':
            n = 1
            for lo, hi in ty['index']:
                n *= max(0, hi - lo + 1)
            if n <= 0:
                return '[]'
            base = self.zero_value(ty['elem'])
            if ty['elem'].get('kind') in ('int', 'real', 'bool'):
                return f'[{base}] * {n}'
            if ty['elem'].get('kind') == 'char':
                return "[''] * %d" % n
            return f'[{base} for _ in range({n})]'
        if kind == 'record':
            return '{' + ', '.join(
                f'{_py_name(k)!r}: {self.zero_value(v, k)}'
                for k, v in ty['fields'].items()) + '}'
        if kind == 'set':
            return 'set()'
        if kind == 'real':
            return '0.0'
        if kind == 'file':
            return self.file_object(name)
        if kind in ('alfa', 'char'):
            return "''" if kind == 'char' else '[]'
        if kind == 'bool':
            return 'False'
        return '0'

    def _reads_input(self, r: Routine) -> bool:
        """Читает ли подпрограмма ввод (`GЕТ` от файла ввода или `f^`)."""
        found = [False]

        def walk(node):
            if found[0]:
                return
            if isinstance(node, tuple):
                if node[0] == 'call' and canon(node[1]) == 'GET':
                    found[0] = True
                    return
                if node[0] == 'deref' and isinstance(node[1], tuple) \
                        and node[1][0] == 'var':
                    found[0] = True
                    return
                for item in node:
                    walk(item)
            elif isinstance(node, list):
                for item in node:
                    walk(item)
            elif isinstance(node, dict):
                for item in node.values():
                    walk(item)

        walk(r.body)
        return found[0]

    def _routine_names(self, prog: Program):
        """Имена всех подпрограмм (включая вложенные) в каноническом виде.

        Возвращает пару `(все имена, имена функций)`.
        """
        names: set[str] = set()
        funcs: set[str] = set()

        def collect(r: Routine) -> None:
            names.add(canon(r.name))
            if r.is_function:
                funcs.add(canon(r.name))
            for sub in r.nested:
                collect(sub)

        for r in prog.procedures:
            collect(r)
        names.update({
            'WRITE', 'WRITELN', 'PRINT', 'PRINTLN', 'GET', 'RESET',
            'REWRITE', 'CLOSE', 'BIND', 'FLUSH', 'HALT', 'EXIT', 'RANDOM',
            'CODE', 'CHR', 'ORD', 'CARD', 'LENGTH', 'SEL', 'INS', 'DELETE',
            'POS', 'CONCAT', 'UPRSK', 'UPPERCASE', 'ABS', 'SQR', 'SQRT',
            'EXP', 'SIN', 'COS', 'ARCSIN', 'ARCCOS', 'ARCTAN', 'LN',
            'ROUND', 'TRUNC', 'ODD', 'SUCC', 'PRED', 'SEL',
        })
        return names, funcs

    def _var_params(self, prog: Program) -> dict:
        """Сколько `VАR`-параметров у каждой подпрограммы.

        В Python передача по ссылке отсутствует, поэтому результат
        таких параметров возвращается подпрограммой и разворачивается
        на месте вызова.
        """
        out: dict[str, int] = {}

        def collect(r: Routine) -> None:
            n = sum(1 for _n, _t, mode in r.params if mode == 'var')
            if n:
                out[canon(r.name)] = n
            for sub in r.nested:
                collect(sub)

        for r in prog.procedures:
            collect(r)
        return out

    def file_object(self, name: str) -> str:
        """Файловая переменная диалекта.

        `INРUТ` — клавиатура оператора (единственный вход, с которого
        программа читает команды).  `INР` — рабочий файл программы: в
        `graf.pas` им пользуется `ЗАГОЛ` как записной книжкой
        (`RЕWRIТЕ(INР); WRIТЕ(INР,'ИТМ')`) под вывод текста.  Смешивать
        их нельзя: `REWRITE(INР)` обнулил бы поток ввода.
        """
        c = canon(name)
        if c in ('INRUT', 'INPUT'):
            return '_input'
        if c in ('OUTPUT', 'OUTRUT', 'OUT'):
            return '_output'
        return f'PasFile({name!r})'

    def resolve(self, ty):
        seen = 0
        while isinstance(ty, dict) and ty.get('kind') == 'alias':
            name = canon(ty['name'])
            if name in self.prog.types:
                ty = self.prog.types[name]
            else:
                return {'kind': 'int'}
            seen += 1
            if seen > 16:                                    # pragma: no cover
                break
        return ty if isinstance(ty, dict) else {'kind': 'int'}

    # ── подпрограммы ────────────────────────────────────────────────────────

    def emit_routine(self, r: Routine) -> None:
        base = self.indent
        name = _py_name(r.name)
        params = [_py_name(pname) for pname, _t, _m in r.params]
        var_names = [_py_name(pname) for pname, _t, mode in r.params
                     if mode == 'var']
        self.w(f'def {name}({", ".join(params) if params else ""}):')
        self.indent = base + 1
        # Переменные программы, к которым обращается подпрограмма,
        # объявляются глобальными (в Паскале — обычные переменные).
        shadow = {pname for pname, _t, _m in r.params}
        shadow |= {k for k in r.locals if not k.startswith('#')}
        shadow |= {raw for raw, _v in r.consts.values()}
        visible = set(shadow)
        enclosing: set = set()
        for frame in self.shadow_stack:      # локальные внешних подпрограмм
            visible |= frame
            enclosing |= frame
        outer = [_py_name(v) for v in self.prog.vars if v not in visible]
        # В диалекте вложенность разрешена: подпрограмма видит переменные
        # всех объемлющих подпрограмм.  В Python чтение такой переменной
        # работает через замыкание, но ПРИСВАИВАНИЕ делает её локальной
        # (и даёт `UnboundLocalError` при чтении).  Поэтому имена,
        # принадлежащие внешним подпрограммам, объявляем `nonlocal`.
        # Имена из `shadow` (собственные локальные и параметры) из этого
        # списка исключены — они и так имеют приоритет.
        enclosing -= shadow
        self.shadow_stack.append(shadow)
        self.scope_stack.append(
            {**r.locals, **{p[0]: p[1] for p in r.params}})
        if outer:
            self.w('global ' + ', '.join(outer))
        if enclosing:
            self.w('nonlocal ' + ', '.join(_py_name(v) for v in enclosing))
        saved = self.routine
        self.routine = r
        for _c, (raw, value) in r.consts.items():
            self.w(f'{_const_name(raw)} = {self.const_value(value)}')
        # Параметры не инициализируются: они приходят из вызова.
        param_names = {pname for pname, _pty, _mode in r.params}
        for lname, lty in r.locals.items():
            if lname.startswith('#') or lname in param_names:
                continue
            self.w(f'{_py_name(lname)} = {self.zero_value(lty, lname)}')
        if r.nested:
            for sub in r.nested:
                self.w('')
                self.emit_routine(sub)
        body = list(r.body)
        if r.is_function:
            rname = self.result_variable(r)
            rtype = self.resolve(r.result_type or {'kind': 'alfa'})
            if rname not in r.locals and rname not in param_names:
                self.w(f'{_py_name(rname)} = {self.zero_value(rtype, rname)}')
            if self._reads_input(r):
                # Сторож офлайн-прогона: когда ввод исчерпан, программа
                # завершается так же, как при нажатии `К` на терминале.
                self.w('if _input.pos >= len(_input.data): raise SystemExit(0)')
            if body:
                self.w('try:')
                self.indent += 1
                self.block(body)
                self.indent = base + 1
                self.w('except _ExitRoutine as _e:')
                self.indent += 1
                self.w(f'if _e.rid != {self.rid}: raise')
                self.indent -= 1
            else:
                self.w('pass')
            # Сторож офлайн-прогона: при исчерпанном вводе программа
            # завершает работу, как при нажатии `К` на терминале.
            self.w(f'return {_py_name(rname)}')
        else:  # обычная подпрограмма
            if not body and not r.nested:
                self.w('pass')
            elif not body:
                self.w('pass')
            else:
                self.w('try:')
                self.indent += 1
                self.block(body)
                self.indent = base + 1
                self.w('except _ExitRoutine as _e:')
                self.indent += 1
                self.w(f'if _e.rid != {self.rid}: raise')
                self.indent -= 1
        if var_names:
            self.w('return (' + ', '.join(var_names) + ')')
        if canon(name) == 'PRIKAZ':
            # Оператор не ввёл команду: на настоящем терминале программа
            # ждала бы нажатия клавиши и экран сохранял бы последний кадр.
            # Без этой остановки `СЕЛЕСТ` попадает в ветвь `ТР:` и
            # бесконечно перерисовывает картину.
            self.w('if not А and INРUТ.pos >= len(INРUТ.data):')
            self.indent += 1
            self.w('raise SystemExit(0)')
            self.indent -= 1
        self.routine = saved
        self.indent = base
        self.rid += 1
        if self.shadow_stack:
            self.shadow_stack.pop()
        if self.scope_stack:
            self.scope_stack.pop()

    # ── операторы ────────────────────────────────────────────────────────────

    def block(self, stmts: list) -> None:
        """Выдать список операторов с поддержкой меток и `GOTO`."""
        if not any(s and s[0] == 'label' for s in stmts):
            self.body(stmts, None)
            return
        base = self.indent
        self.w('_goto = None')
        self.w('while True:')
        self.indent = base + 1
        self.w('try:')
        self.indent += 1
        first = True
        for st in stmts:
            if st and st[0] == 'label':
                _, label, inner = st
                self.w(f'if _goto == {label}: _goto = None')
                self.emit_stmt(inner)
                first = False
                continue
            self.emit_stmt(st)
            first = False
        self.w('break')
        self.indent = base + 1
        self.w('except _Goto as _g:')
        self.indent += 1
        self.w('_goto = _g.n')
        self.w('continue')
        self.indent = base
        del first

    def body(self, stmts: list, terminator: str | None) -> None:
        for st in stmts:
            self.emit_stmt(st)
        if terminator:
            self.w(terminator)

    def emit_stmt(self, st) -> None:
        if st is None:
            return
        if isinstance(st, list):
            for sub in st:
                self.emit_stmt(sub)
            return
        kind = st[0]
        if kind == 'label':
            self.emit_stmt(st[2])
        elif kind == 'block':
            self.w('if True:')
            self.indent += 1
            self.body(st[1], None)
            self.indent -= 1
        elif kind == 'assign':
            self.w(f'{self.assign_target(st[1])} = '
                   f'{self.assigned_value(st[1], st[2])}')
        elif kind == 'call':
            name, args = st[1], st[2]
            if canon(name) == 'CODE':
                self.w(f'_code({self.code_text(args)!r})')
                return
            if canon(name) not in self.known:
                # Метка или имя, потерянное при распознавании оригинала.
                self.w(f'pass  # нераспознанный вызов {name}')
                return
            nvar = self.varparams.get(canon(name), 0)
            text = self.call_expr(name, args)
            if not nvar:
                self.w(text)
                return
            # `VАR`-параметры возвращаются подпрограммой и присваиваются
            # обратно на месте вызова.
            self.temp += 1
            tmp = f'_rv{self.temp}'
            self.w(f'{tmp} = {text}')
            # `VАR`-параметры занимают последние позиции списка
            # параметров подпрограммы.
            targets = [a for a in args[len(args) - nvar:]
                       if a[0] == 'var']
            if targets:
                self.w(', '.join(self.assign_target(t) for t in targets)
                       + ' = ' + tmp)
        elif kind == 'code':
            self.w(f'_code({st[1]!r})')
        elif kind == 'code_assign':
            self.w(self.code_assign(st[1], st[2]))
        elif kind == 'code_get':
            fname = _py_name(st[1])
            rname = (self.result_variable(self.routine)
                     if self.routine and self.routine.is_function else '_tmp')
            self.w(f'{_py_name(rname)}.append({fname}.cur)')
            self.w(f'GET({fname})')
        elif kind == 'if':
            self.w(f'if {self.expr(st[1])}:')
            self.indent += 1
            self.emit_stmt(st[2])
            self.indent -= 1
            if st[3] is not None:
                self.w('else:')
                self.indent += 1
                self.emit_stmt(st[3])
                self.indent -= 1
        elif kind == 'while':
            self.w(f'while {self.expr(st[1])}:')
            self.indent += 1
            self.emit_stmt(st[2])
            self.indent -= 1
        elif kind == 'repeat':
            self.w('while True:')
            self.indent += 1
            self.body(st[1], None)
            self.w(f'if {self.expr(st[2])}:')
            self.indent += 1
            self.w('break')
            self.indent -= 1
        elif kind == 'for':
            _, var, start, limit, body, downto = st
            if start is None:
                start = ('const', 1)
            rng = 'range' if not downto else 'range'
            step = '' if not downto else ', -1'
            self.w(f'for {self.var_name(var)} in range('
                   f'{self.expr(start)}, '
                   f'{self.expr(limit)} + (1 if ({self.expr(limit)} '
                   f'- {self.expr(start)}) >= 0 else 0){step}):')
            self.indent += 1
            self.emit_stmt(body)
            self.indent -= 1
            del rng
        elif kind == 'case':
            self.case_statement(st[1], st[2])
        elif kind == 'goto':
            labels = r_labels(self.routine)
            if st[1] in labels:
                self.w(f'raise _Goto({st[1]})')
            else:
                # Метка объявлена вне подпрограммы (в исходнике, полученном
                # распознаванием, такие переходы встречаются) — считаем,
                # что ветвь недостижима.
                self.w(f'pass  # GОТО {st[1]}: метка вне подпрограммы')
        elif kind == 'exit':
            self.w(f'raise _ExitRoutine({self.rid})')
        else:                                              # pragma: no cover
            raise ValueError(f'неизвестный оператор {kind!r}')

    def case_statement(self, subject, branches) -> None:
        """Переключатель `САSЕ` и цепочка условий `SЕLЕСТ`."""
        if subject is not None:
            self.case_switch(subject, branches)
            return
        # Сначала вычисляем все условия, затем строим цепочку if/elif:
        # присваивания между ветвями разорвали бы её.
        prepared = []
        for labels, body in branches:
            if labels is None:
                prepared.append((None, body))
                continue
            conds = []
            for lab in labels:
                if lab[0] == 'var' and canon(lab[1]) in ('TR', 'TRUE'):
                    conds.append('True')
                    continue
                self.temp += 1
                t = f'_c{self.temp}'
                self.w(f'{t} = {self.expr(lab)}')
                conds.append(f'({t})')
            prepared.append((' or '.join(conds), body))
        first = True
        default = None
        for cond, body in prepared:
            if cond is None:
                default = body
                continue
            kw = 'if' if first else 'elif'
            first = False
            self.w(f'{kw} {cond}:')
            self.indent += 1
            self.emit_stmt(body)
            self.indent -= 1
        if default is not None:
            self.w('else:')
            self.indent += 1
            self.emit_stmt(default)
            self.indent -= 1

    def case_switch(self, subject, branches) -> None:
        """`САSЕ N ОF 1: …` — сравнение со значениями-метками."""
        expr = self.expr(subject)
        first = True
        default = None
        for labels, body in branches:
            if labels is None:
                default = body
                continue
            conds = []
            for lab in labels:
                if lab[0] == 'const':
                    conds.append(f'({expr}) == {self.const_value(lab[1])}')
                else:
                    self.temp += 1
                    t = f'_c{self.temp}'
                    self.w(f'{t} = {self.expr(lab)}')
                    conds.append(f'({expr}) == ({t})')
            kw = 'if' if first else 'elif'
            first = False
            self.w(f'{kw} {" or ".join(conds)}:')
            self.indent += 1
            self.emit_stmt(body)
            self.indent -= 1
        if default is not None:
            self.w('else:')
            self.indent += 1
            self.emit_stmt(default)
            self.indent -= 1

    # ── выражения ────────────────────────────────────────────────────────────

    def result_var_name(self, name: str) -> str:
        """Если имя — имя функции-результата, вернуть переменную-результат."""
        if (self.routine is not None and self.routine.is_function
                and canon(name) == canon(self.routine.name)):
            return self.result_variable(self.routine)
        return name

    def assign_target(self, node) -> str:
        if node[0] == 'var':
            return _py_name(self.result_var_name(node[1]))
        return self.expr(node)

    def var_name(self, name: str) -> str:
        return _py_name(name)

    def lookup_type(self, name: str):
        """Тип переменной: сначала локальные, затем внешние области."""
        if self.routine is not None and name in self.routine.locals:
            return self.routine.locals[name]
        for pname, pty, mode in (self.routine.params if self.routine else []):
            if pname == name:
                return pty
        for scope in reversed(self.scope_stack):      # вложенные подпрограммы
            if name in scope:
                return scope[name]
        return self.prog.vars.get(name)

    def expr(self, node) -> str:
        kind = node[0]
        if kind == 'width':
            # Спецификация ширины поля вывода — на вывод не влияет.
            return self.expr(node[1])
        if kind == 'const':
            return self.const_value(node[1])
        if kind == 'var':
            name = self.result_var_name(node[1])
            if canon(name) in self.functions and self.lookup_type(name) is None:
                return f'{_py_name(name)}()'
            return _py_name(name)
        if kind == 'deref':
            return f'{self.expr(node[1])}.cur'
        if kind == 'index':
            base = node[1]
            idxs = node[2]
            text = self.expr(base)
            ty = self.type_of(base)
            for n, idx in enumerate(idxs):
                bound = self.index_bound(ty, n)
                # `ARRAY[l..h]` хранится списком с нуля: индекс `l`
                # соответствует элементу 0.
                off = bound[0] if bound else 0
                text = f'{text}[({self.expr(idx)}) - {off}]'
                ty = self.elem_type(ty, n)
            return text
        if kind == 'field':
            base = node[1]
            return f'{self.expr(base)}[{node[2]!r}]'
        if kind == 'binop':
            op = node[1]
            l, r = self.expr(node[2]), self.expr(node[3])
            if op == 'and':
                return f'({l} and {r})'
            if op == 'or':
                return f'({l} or {r})'
            if op == '=':
                return f'({l} == {r})'
            if op == '<>':
                return f'({l} != {r})'
            if op == '//':
                return f'int_trunc({l}) // int_trunc({r})'
            if op == '%':
                return f'int_trunc({l}) % int_trunc({r})'
            if op == '/':
                return f'({l}) / ({r})'
            return f'({l} {op} {r})'
        if kind == 'unop':
            if node[1] == 'not':
                return f'(not {self.expr(node[2])})'
            return f'({node[1]}{self.expr(node[2])})'
        if kind == 'in':
            return f'_in({self.expr(node[1])}, {self.expr(node[2])})'
        if kind == 'setlit':
            items = []
            for it in node[1]:
                if it[0] == 'range':
                    items.append(f'_range({self.expr(it[1])}, '
                                 f'{self.expr(it[2])})')
                else:
                    items.append(f'[{self.expr(it[1])}]')
            return '_set([' + ', '.join(items) + '])'
        if kind == 'call':
            return self.call_expr(node[1], node[2])
        raise ValueError(f'неизвестное выражение {kind!r}')       # pragma: no cover

    def type_of(self, node):
        if node[0] == 'var':
            ty = self.lookup_type(node[1])
            return self.resolve(ty) if ty is not None else {'kind': 'int'}
        if node[0] == 'deref':
            return {'kind': 'char'}
        if node[0] == 'index':
            ty = self.type_of(node[1])
            for n, idx in enumerate(node[2]):
                ty = self.elem_type(ty, n)
            return ty
        if node[0] == 'field':
            base = self.resolve(self.type_of(node[1]))
            if base.get('kind') == 'record':
                return self.resolve(base['fields'].get(node[2], {'kind': 'int'}))
        return {'kind': 'int'}

    def index_bound(self, ty, n: int):
        ty = self.resolve(ty)
        if ty.get('kind') == 'array' and n < len(ty['index']):
            return ty['index'][n]
        return (0, 0)

    def elem_type(self, ty, n: int):
        ty = self.resolve(ty)
        if ty.get('kind') == 'array':
            return self.resolve(ty['elem'])
        return {'kind': 'int'}

    def assigned_value(self, target: list, value: list) -> str:
        """Правая часть присваивания с учётом типа переменной.

        Запись массива в запись (`МАС = RЕСОRD М: АRRАУ [1..7] ОF INТ`)
        задаётся литералом `[376В, 202В, …]`, а не `ММ.М := …`.  Без
        подстановки литерал трактовался бы как множество, и обращение
        `ММ.М[К]` падало бы с «'set' object is not subscriptable».
        """
        if target[0] != 'var' or value[0] not in ('list', 'setlit'):
            return self.expr(value)
        ty = self.resolve(self.type_of(target))
        fields = ty.get('fields') or {}
        arr = [f for f, t in fields.items()
               if self.resolve(t).get('kind') == 'array']
        if ty.get('kind') == 'record' and len(fields) == 1 and arr:
            return "{'" + arr[0] + "': [" + \
                ', '.join(self.expr(it[1]) for it in value[1]) + ']}'
        return self.expr(value)

    def call_expr(self, name: str, args: list) -> str:
        c = canon(name)
        if c in FUNCTION_STUBS:
            return FUNCTION_STUBS[c]
        text_args = [self.expr(a) for a in args]
        if c in ('CHR', 'ORD', 'CARD', 'LENGTH', 'SEL', 'INS', 'DELETE',
                 'POS', 'CONCAT', 'UPRSK', 'ABS', 'SQR', 'SQRT', 'EXP',
                 'SIN', 'COS', 'ARCSIN', 'ARCCOS', 'ARCTAN', 'LN', 'ROUND',
                 'TRUNC', 'ODD', 'SUCC', 'PRED'):
            return f'{c}({", ".join(text_args)})'
        c = CALL_ALIASES.get(c, c)
        if c in ('WRITE', 'WRITELN', 'PRINT', 'PRINTLN'):
            # Спецификации ширины (`K:0B`) на вывод не влияют.
            text_args = [self.expr(a) for a in args if a[0] != 'width']
            return f'{c}({", ".join(text_args)})'
        if c == 'BIND':
            return f'BIND({", ".join(text_args)})'
        if c == 'GET':
            return f'GET({", ".join(text_args)})'
        if c in ('RESET', 'REWRITE', 'CLOSE'):
            return f'{c}({", ".join(text_args)})'
        if c == 'RANDOM':
            return 'code_random()'
        if c == 'CODE':
            return f'_code({self.code_text(args)!r})'
        if c == 'SELECT' or c == 'SEL' and False:
            pass
        # пользовательская подпрограмма
        return f'{_py_name(name)}({", ".join(text_args)})'

    # ── макросы `СОDЕ` ──────────────────────────────────────────────────────

    def code_text(self, args) -> str:
        return ' '.join(self.raw_expr(a) for a in args)

    def raw_expr(self, node) -> str:
        return _raw(node)

    def code_assign(self, var: str, text: str) -> str:
        ty = self.resolve(self.lookup_type(var) or {'kind': 'int'})
        name = _py_name(var)
        if ty.get('kind') == 'alfa':
            return (f'{name} = list(_VOWELS) if canon({var!r}) == "Z" '
                    f'else {self.zero_value(ty)}')
        if ty.get('kind') in ('char',):
            return f'{name} = ""'
        if ty.get('kind') == 'set':
            return f'{name} = _set(255)'
        return f'{name} = code_value({text!r})'


def r_labels(routine) -> set:
    """Метки, объявленные внутри подпрограммы."""
    return set(getattr(routine, 'labels', ()) or ())


def _raw(node) -> str:
    kind = node[0]
    if kind == 'const':
        return repr(node[1])
    if kind == 'var':
        return node[1]
    if kind == 'index':
        return (f'{_raw(node[1])}[' +
                ','.join(_raw(i) for i in node[2]) + ']')
    if kind == 'field':
        return f'{_raw(node[1])}.{node[2]}'
    if kind == 'deref':
        return f'{_raw(node[1])}^'
    if kind == 'binop':
        return f'{_raw(node[2])} {node[1]} {_raw(node[3])}'
    if kind == 'unop':
        return f'{node[1]} {_raw(node[2])}'
    if kind == 'call':
        return (f'{node[1]}(' +
                ', '.join(_raw(a) for a in node[2]) + ')')
    return str(node)


def _const_name(name: str) -> str:
    if keyword.iskeyword(name):
        return name + '_'
    return name