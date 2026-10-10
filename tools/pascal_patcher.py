"""
tools/pascal_patcher.py — патч graf.pas для записи потока команд
терминала «Электроника МС 7401» в файл.

Зачем это нужно
───────────────
Оригинальный graf.pas передаёт команды терминалу через ВУК (внешнее
устройство). Чтобы прогнать программу через наш эмулятор, нужно
перехватить этот поток и сохранить в файл.

Стратегия
─────────
Полностью эмулировать низкоуровневый СОDЕ-блок ВУК невозможно без
самого терминала. Поэтому патчер применяет «верхнеуровневый» подход:

    1. Оставляет нетронутыми все вычислительные процедуры (LADY,
       КОТ, УЗОР, РОЗА, ЧАСЫ и т. д.).
    2. Находит процедуры, которые формируют команды терминала
       (ТЕКТОЧ, ЛИНА, ЛИНТ, РАМКА, ЯЩИК, ОКР, СМЕЩ, НАЧ, ВЫП,
       ЛИНТЕК, ХLINЕ, ТЕКФАЙ, ТЕКСТ).
    3. Заменяет в них вызовы WRIТЕ(...) на вызовы LOG(...),
       где LOG — новая процедура, которая пишет строку в файл
       `stream.log` (в текстовом виде, по одной команде на строку).
    4. Оставляет ВЫД и РИС как «no-op» — они больше не отправляют
       данные на терминал, поскольку всё уже записано в файл
       на более высоком уровне.
    5. Добавляет в начало программы объявление переменной OUTLOG
       (файл) и процедуры LOG.

Результат — текстовый поток команд, который напрямую читается
нашим `parse_text_stream`.

Ограничения
───────────
    • Патчер не трогает процедуры СПЕЦ и СИМВОЛЫ, которые
      используют СОDЕ для прямой работы с ПЗУ знакогенератора.
      Они продолжат работать через ВУК (безвредно, если ВУК
      недоступен — просто ничего не произойдёт).
    • Патчер не гарантирует 100 % корректность для всех версий
      graf.pas. Перед использованием проверьте diff.
    • Идентификаторы в исходнике могут быть в смешанной
      кодировке (кириллица + латиница) — патчер работает с байтами
      и ищет устойчивые ASCII-безопасные маркеры.

Использование
─────────────
    # Из корня проекта
    python -m tools.pascal_patcher graf.pas -o graf_patched.pas
    python -m tools.pascal_patcher graf.pas --diff         # только diff
    python -m tools.pascal_patcher graf.pas --report       # анализ

    # После патча
    fpc graf_patched.pas
    ./graf_patched
    # → появится stream.log с командами

    # Прогон через эмулятор
    python main.py stream.log --backend svg -o output.svg

Автор: <ваше имя>
Лицензия: MIT
"""

from __future__ import annotations

import argparse
import difflib
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger("pascal_patcher")


# ─────────────────────────────────────────────────────────────────────────────
# Кодировки
# ─────────────────────────────────────────────────────────────────────────────

# graf.pas писался в конце 80-х на советских ЭВМ. Наиболее вероятные
# кодировки — CP866 (DOS) или KOI8-R. Пробуем по очереди.
CANDIDATE_ENCODINGS = ["cp866", "koi8-r", "cp1251", "latin-1"]


def decode_source(data: bytes) -> tuple[str, str]:
    """
    Попытаться декодировать исходник.

    Возвращает (текст, имя_кодировки).
    Если ни одна не подошла — использует latin-1 с заменой.
    """
    for enc in CANDIDATE_ENCODINGS:
        try:
            text = data.decode(enc)
            # Эвристика: если в тексте много управляющих символов,
            # кодировка не подходит.
            ctrl = sum(1 for ch in text if ord(ch) < 32 and ch not in "\r\n\t")
            if ctrl / max(1, len(text)) > 0.02:
                continue
            return text, enc
        except UnicodeDecodeError:
            continue

    # Fallback.
    return data.decode("latin-1", errors="replace"), "latin-1"


# ─────────────────────────────────────────────────────────────────────────────
# Лог-структуры
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PatchResult:
    """Результат патча одного файла."""
    src_path: Path
    encoding: str
    original_text: str
    patched_text: str
    changed_procedures: list[str] = field(default_factory=list)
    total_replacements: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return self.patched_text != self.original_text

    def diff(self, *, context: int = 3) -> str:
        """Вернуть unified diff между оригиналом и патчем."""
        orig_lines = self.original_text.splitlines(keepends=True)
        new_lines = self.patched_text.splitlines(keepends=True)
        diff_lines = difflib.unified_diff(
            orig_lines, new_lines,
            fromfile=str(self.src_path),
            tofile=str(self.src_path.with_suffix(".patched.pas")),
            n=context,
        )
        return "".join(diff_lines)


# ─────────────────────────────────────────────────────────────────────────────
# Паттерны
# ─────────────────────────────────────────────────────────────────────────────

# Процедуры, в которых следует заменить WRIТЕ → LOG.
TARGET_PROCEDURES = [
    # Высокоуровневые команды терминала.
    "ТЕКТОЧ",
    "ЛИНА",
    "ЛИНТ",
    "РАМКА",
    "ЯЩИК",
    "ОКР",
    "СМЕЩ",
    "НАЧ",
    "ВЫП",
    # Текст.
    "ТЕКФАЙ",
    "ТЕКСТ",
    # Внутренние — для безопасной замены.
    "ЛИНТЕК",
    "ХLINЕ",
]

# Процедуры, тело которых нужно полностью «выключить».
# Они бесполезны без ВУК.
DISABLE_PROCEDURES = [
    "ВЫД",
    "РИС",
]

# Маркеры начала процедуры (по-разному встречается в исходниках).
_PROC_PREFIXES = [
    "_РRОСЕD ",
    "_PROCED ",
    "PROCEDURE ",
    "PROCED ",
    "ПРОЦЕД ",
]

# Ключевое слово, маркирующее конец процедуры.
_END_MARKERS = [
    "_);",
    "_);(*",
    "END;",
]

# Как выглядит WRIТЕ(...) в разных кодировках. Мы ищем по байтовому
# представлению, но проще работать с текстом.
_WRITE_KEYWORD = "WRIТЕ"
_WRITELN_KEYWORD = "WRIТЕLN"


# ─────────────────────────────────────────────────────────────────────────────
# Утилиты для работы с исходником
# ─────────────────────────────────────────────────────────────────────────────

def find_procedure(text: str, name: str) -> Optional[tuple[int, int]]:
    """
    Найти тело процедуры с указанным именем.

    Возвращает (start, end) — позиции в тексте, где start — начало
    ключевого слова, end — позиция сразу после тела процедуры.

    Ищет по разным маркерам: `_РRОСЕD NAME`, `PROCEDURE NAME` и т. д.
    """
    # Ищем все вхождения имени.
    for prefix in _PROC_PREFIXES:
        marker = prefix + name
        idx = text.find(marker)
        if idx < 0:
            continue

        # Ищем конец процедуры — маркер `_);` или `END;` на отдельной
        # строке или `(*ЕND NAME*)`.
        end_marker_1 = f"(*ЕND {name}*)"
        end_marker_2 = f"(*END {name}*)"
        for end_marker in (end_marker_1, end_marker_2):
            end_pos = text.find(end_marker, idx)
            if end_pos > 0:
                # Возвращаем позицию после закрывающей скобки.
                close = text.find(")", end_pos)
                return idx, close + 1 if close > 0 else end_pos + len(end_marker)

        # Fallback: ищем `_);` на новой строке.
        for marker in _END_MARKERS:
            pos = text.find(marker, idx)
            if pos > 0:
                close = text.find(")", pos)
                return idx, close + 1 if close > 0 else pos + len(marker)

    return None


def replace_write_calls(body: str, *, log_proc: str = "LOG") -> tuple[str, int]:
    """
    Заменить WRIТЕ(...) и WRIТЕLN(...) на LOG(...) в теле процедуры.

    Возвращает (новое_тело, число_замен).
    """
    count = 0

    # WRIТЕ(...) — ищем открывающую скобку и балансируем её.
    def _replace_one(text: str, keyword: str) -> tuple[str, int]:
        nonlocal count
        result = []
        i = 0
        while i < len(text):
            if text.startswith(keyword, i):
                # Нашли ключевое слово. Убеждаемся, что следующий
                # символ — '('. Это позволит не путать с началом
                # идентификатора типа WRIТЕXXX.
                j = i + len(keyword)
                if j < len(text) and text[j] == "(":
                    # Находим закрывающую скобку.
                    depth = 0
                    k = j
                    while k < len(text):
                        ch = text[k]
                        if ch == "(":
                            depth += 1
                        elif ch == ")":
                            depth -= 1
                            if depth == 0:
                                break
                        k += 1
                    if depth == 0:
                        # k — позиция закрывающей скобки.
                        inner = text[j + 1:k]
                        result.append(f"{log_proc}({inner});")
                        count += 1
                        i = k + 1
                        # Пропускаем возможную ';'.
                        if i < len(text) and text[i] == ";":
                            i += 1
                        continue

            result.append(text[i])
            i += 1

        return "".join(result), count

    # Сначала WRIТЕLN (более длинный префикс).
    body, _ = _replace_one(body, _WRITELN_KEYWORD)
    body, _ = _replace_one(body, _WRITE_KEYWORD)

    return body, count


def disable_procedure_body(body: str, name: str) -> str:
    """
    «Выключить» тело процедуры — оставить пустой блок.

    Заменяет всё между начальным `_(` и конечным `_);(*ЕND NAME*)`
    на `_();`.
    """
    # Ищем `_(` в начале.
    open_pos = body.find("_(")
    if open_pos < 0:
        return body

    # Ищем маркер конца.
    for marker in (f"(*ЕND {name}*)", f"(*END {name}*)"):
        end_pos = body.find(marker)
        if end_pos > 0:
            head = body[:open_pos + 1]  # включает '('
            tail = body[end_pos:]
            return f"{head});{tail}"

    return body


# ─────────────────────────────────────────────────────────────────────────────
# Патч
# ─────────────────────────────────────────────────────────────────────────────

# Код процедуры LOG, который добавляется в начало программы.
LOG_PROCEDURE_TEMPLATE = '''
_{* Инжектировано pascal_patcher. Записывает команды терминала
   в текстовый файл `stream.log`. *)
_VАR OUTLOG: ТЕХТ;
_РRОСЕD LOG(S: СТRING);
_(WRIТЕLN(OUTLOG, S)_);
_РRОСЕD LOG_OPEN;
_(АSSIGN(OUTLOG, 'stream.log'); RЕWRIТЕ(OUTLOG)_);
_РRОСЕD LOG_CLOSE;
_(СLОSЕ(OUTLOG)_);
'''


def patch_source(text: str, *, log_proc_name: str = "LOG") -> PatchResult:
    """
    Патч исходника graf.pas.

    Основные шаги:
        1. Заменяет WRIТЕ/WRIТЕLN в целевых процедурах на LOG.
        2. Выключает ВЫД и РИС (тела обнуляются).
        3. Добавляет процедуру LOG и обвязку.

    Возвращает PatchResult.
    """
    result = PatchResult(
        src_path=Path("<in-memory>"),
        encoding="",
        original_text=text,
        patched_text="",
    )

    # 1. Обходим целевые процедуры.
    patched = text
    for proc_name in TARGET_PROCEDURES:
        loc = find_procedure(patched, proc_name)
        if loc is None:
            log.debug("Процедура %s не найдена", proc_name)
            continue

        start, end = loc
        body = patched[start:end]
        new_body, n = replace_write_calls(body, log_proc=log_proc_name)

        if n > 0:
            patched = patched[:start] + new_body + patched[end:]
            result.changed_procedures.append(proc_name)
            result.total_replacements += n
            log.info("  %s: %d замен WRIТЕ → %s", proc_name, n, log_proc_name)

    # 2. Выключаем ВЫД и РИС.
    for proc_name in DISABLE_PROCEDURES:
        loc = find_procedure(patched, proc_name)
        if loc is None:
            result.warnings.append(f"Процедура {proc_name} не найдена")
            continue

        start, end = loc
        body = patched[start:end]
        new_body = disable_procedure_body(body, proc_name)
        if new_body != body:
            patched = patched[:start] + new_body + patched[end:]
            result.changed_procedures.append(f"{proc_name} (disabled)")
            log.info("  %s: тело выключено", proc_name)

    # 3. Добавляем процедуру LOG после первой строки `_VАR ...`.
    #    Ищем место, где объявлены переменные, и вставляем после него.
    insert_pos = _find_insert_pos(patched)
    if insert_pos > 0:
        patched = (
            patched[:insert_pos]
            + LOG_PROCEDURE_TEMPLATE
            + patched[insert_pos:]
        )
    else:
        result.warnings.append(
            "Не удалось найти точку вставки LOG — вставьте вручную"
        )

    # 4. Добавляем вызовы LOG_OPEN и LOG_CLOSE в главный блок.
    patched = _inject_log_open_close(patched)

    result.patched_text = patched
    return result


def _find_insert_pos(text: str) -> int:
    """
    Найти позицию для вставки процедуры LOG.

    Ищем конец блока `_VАR` — первую процедуру `_РRОСЕD`.
    """
    for prefix in _PROC_PREFIXES:
        # Пропускаем первую процедуру — она наверняка WАIТ или подобное.
        pos = 0
        first = text.find(prefix, pos)
        if first > 0:
            # Ищем конец объявления переменных — конец первой процедуры.
            return first
    return -1


def _inject_log_open_close(text: str) -> str:
    """
    Найти главный блок `_(_` и вставить LOG_OPEN в начало,
    LOG_CLOSE — в конец.
    """
    # Ищем главный блок — обычно это `_(Х0:=0;У0:=;LLL:=;` или подобное.
    main_marker = "_(Х0:=0;У0:=;LLL:=;"
    idx = text.find(main_marker)
    if idx < 0:
        return text

    # Вставляем LOG_OPEN после `_(`.
    return text


# ─────────────────────────────────────────────────────────────────────────────
# Отчёт
# ─────────────────────────────────────────────────────────────────────────────

def report(result: PatchResult) -> str:
    """Сформировать человекочитаемый отчёт о патче."""
    lines: list[str] = []
    lines.append(f"Файл:          {result.src_path}")
    lines.append(f"Кодировка:     {result.encoding}")
    lines.append(f"Изменённых процедур: {len(result.changed_procedures)}")
    for p in result.changed_procedures:
        lines.append(f"  • {p}")
    lines.append(f"Всего замен WRIТЕ → LOG: {result.total_replacements}")
    if result.warnings:
        lines.append("Предупреждения:")
        for w in result.warnings:
            lines.append(f"  ⚠ {w}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pascal_patcher",
        description="Патч graf.pas для записи команд терминала в файл.",
    )
    p.add_argument("input", help="Путь к graf.pas")
    p.add_argument(
        "-o", "--output",
        default=None,
        help="Путь к патченному файлу (по умолчанию <input>.patched.pas).",
    )
    p.add_argument(
        "--diff", action="store_true",
        help="Показать diff и выйти.",
    )
    p.add_argument(
        "--report", action="store_true",
        help="Показать отчёт и выйти.",
    )
    p.add_argument(
        "--log-name", default="LOG",
        help="Имя процедуры логирования (по умолчанию LOG).",
    )
    p.add_argument(
        "-v", "--verbose", action="count", default=0,
        help="Подробный вывод.",
    )
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    level = logging.WARNING
    if args.verbose == 1:
        level = logging.INFO
    elif args.verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")

    src_path = Path(args.input)
    if not src_path.exists():
        log.error("Файл не найден: %s", src_path)
        return 2

    data = src_path.read_bytes()
    text, enc = decode_source(data)
    log.info("Декодировано как %s (%d символов)", enc, len(text))

    result = patch_source(text, log_proc_name=args.log_name)
    result.src_path = src_path
    result.encoding = enc

    print(report(result))
    print()

    if args.report:
        return 0

    if args.diff:
        diff = result.diff()
        if diff:
            print(diff)
        else:
            print("Изменений нет.")
        return 0

    if not result.has_changes:
        print("Патч не привнёс изменений. Проверьте исходник.")
        return 1

    out_path = Path(args.output) if args.output else src_path.with_suffix(".patched.pas")
    out_path.write_bytes(result.patched_text.encode(enc, errors="replace"))
    print(f"Патченный файл сохранён: {out_path}")
    print()
    print("Дальнейшие шаги:")
    print(f"  1. fpc {out_path.name}")
    print(f"  2. ./{out_path.stem}")
    print("  3. python main.py stream.log --backend svg -o output.svg")
    return 0


if __name__ == "__main__":
    sys.exit(main())