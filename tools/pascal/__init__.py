"""
pascal — транслятор исходников терминальных программ `graf.pas` / `graf1.pas`.

Пакет позволяет получить настоящий поток команд терминала
«Электроника МС 7401» из программы на Паскале-Мониторе:

    >>> from pascal import translate, run_source
    >>> src = translate('golden/graf.pas')
    >>> result = run_source(src, input_text='СЕТ\\nВЫХ\\n')
    >>> len(result['wire']) > 0
    True

Модули:

* `compiler` — лексический анализ, разбор, AST;
* `emitter`  — генерация текста на Python;
* `runtime`  — исполняющая среда (файлы, `WRITЕ`, макросы `СОDЕ`).
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from .compiler import Parser, preprocess          # noqa: E402
from .emitter import Emitter                     # noqa: E402
from . import runtime                            # noqa: E402

__all__ = ['translate', 'run_source', 'Parser', 'preprocess', 'Emitter',
           'runtime']


def translate(path_or_text: str, *, is_path: bool | None = None) -> str:
    """Перевести исходник `graf.pas` на Python.

    `path_or_text` — путь к файлу или сам текст (если `is_path=False`).
    """
    if is_path is None:
        is_path = '\n' not in path_or_text and len(path_or_text) < 4096 \
            and Path(path_or_text).exists()
    text = Path(path_or_text).read_text(encoding='utf-8') if is_path \
        else path_or_text
    text = preprocess(text)
    parser = Parser(text)
    prog = parser.parse()
    return Emitter(prog, parser.labels).emit(prog)


def run_source(python_source: str, input_text: str = '') -> dict:
    """Выполнить транслированную программу и вернуть поток в линию."""
    return runtime.make_program(python_source, input_text)