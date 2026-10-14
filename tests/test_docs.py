"""
test_docs.py — примеры в докстрингах проверяются наравне с кодом.

Докстринги в этом проекте объясняют неочевидное: почему время берётся
у ЭВМ, а не у компьютера, откуда номер ЭВМ и почему кадр не должен
сдвигаться при начале выделения.  Такие объяснения врут не хуже кода,
а проверять их было нечем: примеры расходились с поведением молча
(`pen.move_cell` обещал `(200, 200)` и возвращал `(39, 377)`, а
`MachineClock.stamp()` в модульном докстринге возвращал секунды там,
где отдаёт короткую форму).

Примеры здесь — не украшение, а утверждения о поведении, поэтому они
выполняются как тесты.  Модуль без примеров молча пропускается: их
появление ничего не ломает.
"""

from __future__ import annotations

import contextlib
import doctest
import importlib
import io
import pkgutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mc7401                                                  # noqa: E402

#: Модули пакета, кроме `__main__`: он при импорте разбирает командную
#: строку и выходит, так что проверять его примеры нельзя.
MODULES = sorted(m.name for m in pkgutil.iter_modules(mc7401.__path__)
                 if m.name != '__main__')


def test_package_has_modules_to_check():
    """Список модулей не пуст — иначе проверка ниже проходит вхолостую."""
    assert 'machine_time' in MODULES
    assert len(MODULES) > 5


@pytest.mark.parametrize('name', MODULES)
def test_doctests_in_module(name):
    """Примеры докстринга модуля дают именно обещанный результат."""
    # Модуль при импорте пишет в stdout («пустой поток команд» и
    # подобное) — вывод не должен попасть в отчёт pytest.
    with contextlib.redirect_stdout(io.StringIO()):
        module = importlib.import_module('mc7401.' + name)
        result = doctest.testmod(module, verbose=False,
                                 report=True, raise_on_error=False)
    assert result.failed == 0, (
        f'в mc7401/{name}.py примеры докстринга не сходятся: '
        f'{result.failed} из {result.attempted}')


def test_package_doctests_are_actually_run():
    """Примеры в пакете есть и они выполняются.

    Без этой проверки набор выше прошёл бы вхолостую: модуль без
    примеров ему не противоречит, и ошибиться было бы негде.  Считается
    по-настоящему — через `testmod`, а не через `DocTestFinder.find`:
    тот находит объекты и для докстрингов вовсе без примеров, так что
    такой список ничего не говорит о том, что проверялось.
    """
    total = 0
    for name in MODULES:
        with contextlib.redirect_stdout(io.StringIO()):
            module = importlib.import_module('mc7401.' + name)
            total += doctest.testmod(module, verbose=False,
                                     report=True,
                                     raise_on_error=False).attempted
    assert total > 0, 'в пакете не нашлось ни одного примера'
    # Модули, где объяснение важнее всего, должны быть среди проверенных.
    for name in ('machine_time', 'select', 'undo', 'history'):
        with contextlib.redirect_stdout(io.StringIO()):
            module = importlib.import_module('mc7401.' + name)
            assert doctest.testmod(module, verbose=False, report=True,
                                   raise_on_error=False).attempted, (
                f'в mc7401/{name}.py нет ни одного примера — '
                f'документация не проверяется')


def test_machine_time_documents_the_wire_form():
    """Модуль часов объясняет, как выглядит ответ ЭВМ на линии.

    Ответ приходит кодами КОИ-7, где кириллица читается как латиница
    (`BPE DATA:… BPEMЯ:…`), и это ровно то, что разбирает
    `parse_machine_time`.  Если формат поменяется, докстринг должен
    поменяться вместе с образцом.
    """
    from mc7401 import machine_time
    text = machine_time.__doc__ or ''
    assert 'BPE' in text, 'не показано, как выглядит директива на линии'
    assert 'КОИ-7' in text, 'не сказано, в какой кодировке приходит ответ'


def test_stamp_documents_the_known_and_unknown_cases():
    """`stamp` разбирает оба случая: время есть и времени нет."""
    from mc7401.machine_time import MachineClock
    doc = MachineClock.stamp.__doc__ or ''
    assert 'КОМП' in doc, 'не сказано, чем помечается время компьютера'
    assert 'full' in doc, 'не сказано, зачем короткая и полная формы'
    # Поведение, о котором говорит докстринг.
    assert MachineClock().stamp() == '', 'без ответа ЭВМ время не пустое'