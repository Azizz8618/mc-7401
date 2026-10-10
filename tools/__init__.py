"""
tools/__init__.py — пакет утилит проекта mc7401-emulator.

Содержит инструменты, не входящие в ядро эмулятора:

    pascal_patcher    — патч graf.pas для записи команд терминала
                        в текстовый файл (stream.log);
    vuk_emulator      — заглушка эмуляции ВУК (в разработке).

Экспортирует ключевые сущности pascal_patcher, чтобы их можно было
импортировать коротко:

    from tools import patch_source, PatchResult, decode_source

Автор: <ваше имя>
Лицензия: MIT
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Реэкспорт pascal_patcher
# ─────────────────────────────────────────────────────────────────────────────

from tools.pascal_patcher import (
    # основной API
    patch_source,
    decode_source,
    report as format_report,
    main as pascal_patcher_main,

    # вспомогательные функции
    find_procedure,
    replace_write_calls,
    disable_procedure_body,

    # структуры
    PatchResult,

    # константы
    CANDIDATE_ENCODINGS,
    TARGET_PROCEDURES,
    DISABLE_PROCEDURES,
    LOG_PROCEDURE_TEMPLATE,
)


# ─────────────────────────────────────────────────────────────────────────────
# vuk_emulator — мягкий импорт (может быть ещё не реализован)
# ─────────────────────────────────────────────────────────────────────────────

try:
    from tools.vuk_emulator import (  # type: ignore[import-not-found]
        VukEmulator,
        create_vuk_emulator,
    )
    _VUK_AVAILABLE = True
except ImportError:
    VukEmulator = None  # type: ignore[assignment]
    create_vuk_emulator = None  # type: ignore[assignment]
    _VUK_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
# Метаданные пакета
# ─────────────────────────────────────────────────────────────────────────────

__all__ = [
    # pascal_patcher
    "patch_source",
    "decode_source",
    "format_report",
    "pascal_patcher_main",
    "find_procedure",
    "replace_write_calls",
    "disable_procedure_body",
    "PatchResult",
    "CANDIDATE_ENCODINGS",
    "TARGET_PROCEDURES",
    "DISABLE_PROCEDURES",
    "LOG_PROCEDURE_TEMPLATE",

    # vuk_emulator
    "VukEmulator",
    "create_vuk_emulator",

    # утилиты пакета
    "available_tools",
    "tools_info",
]


# ─────────────────────────────────────────────────────────────────────────────
# Утилиты пакета
# ─────────────────────────────────────────────────────────────────────────────

def available_tools() -> dict[str, bool]:
    """
    Вернуть словарь доступных инструментов.

    Ключ — имя, значение — True/False (доступен ли).
    """
    return {
        "pascal_patcher": True,
        "vuk_emulator": _VUK_AVAILABLE,
    }


def tools_info() -> str:
    """Человекочитаемая справка о пакете tools."""
    tools = available_tools()
    lines = ["tools — утилиты mc7401-emulator", ""]
    descriptions = {
        "pascal_patcher": (
            "Патч graf.pas: заменяет WRIТЕ(...) на LOG(...), "
            "чтобы сохранить поток команд терминала в stream.log."
        ),
        "vuk_emulator": (
            "Эмуляция внешнего устройства терминала (в разработке)."
        ),
    }
    for name, ok in tools.items():
        mark = "✓" if ok else "✗"
        desc = descriptions.get(name, "")
        lines.append(f"  {mark} {name:16s} — {desc}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Самотест
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    print("=" * 64)
    print(tools_info())
    print("=" * 64)
    print()

    # Минимальный smoke-тест pascal_patcher: патчим кусок синтетического
    # исходника и убеждаемся, что WRIТЕ заменилось на LOG.
    sample = """
_РRОСЕD ТЕКТОЧ(АБС:ВООL;Х,У:INТ);
_(_IF АБС_ТНЕN WRIТЕ(’4’)_ЕLSЕ WRIТЕ(’5’);
WRIТЕ(П1,Х:0,П1,У:0,ВК)_);
(*ЕND ТЕКТОЧ*)

_РRОСЕD ВЫД(S:ВООL);
_VАR К,L,М:INТ;
_(СОDЕ(...);WRIТЕ(АР2);ЕХIТ;СОDЕ(...)_);
(*ЕND ВЫД*)
"""

    result = patch_source(sample)
    print(f"Изменённых процедур: {len(result.changed_procedures)}")
    for p in result.changed_procedures:
        print(f"  • {p}")
    print(f"Всего замен WRIТЕ → LOG: {result.total_replacements}")
    print()

    print("Патченный фрагмент:")
    print("-" * 64)
    print(result.patched_text[:600])
    print("-" * 64)

    if result.has_changes:
        print("✓ Патч сработал.")
    else:
        print("✗ Патч не дал изменений — проверьте шаблон.")