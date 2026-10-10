#!/usr/bin/env python3
"""
gen_golden.py — генерация эталонных потоков и изображений.

Сценарии повторяют работу оператора за терминалом «Электроника МС 7401»:
запускается программа `graf.pas` из папки `golden/`, на вход терминала
подаётся последовательность приказов, а на выход снимается поток
команд в линию.  Поток отдаётся эмулятору и сохраняется как PNG/SVG.

Примеры:

    python tools/gen_golden.py                 # все сценарии
    python tools/gen_golden.py СЕТ СОЛ         # только указанные
    python tools/gen_golden.py --list

Результат:

    golden/streams/<сценарий>.bin   поток команд в линию
    golden/render/<сценарий>.png    изображение экрана
    golden/render/<сценарий>.svg    то же в векторе
    golden/index.md                 сводная таблица
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

from mc7401 import TerminalEmulator                       # noqa: E402
from mc7401.backends import PNGBackend, SVGBackend        # noqa: E402
from pascal import run_source, translate                  # noqa: E402

GOLDEN = ROOT / 'golden'
STREAMS = GOLDEN / 'streams'
RENDER = GOLDEN / 'render'

#: Приказ оператора — ввод, передаваемый терминалу.
#: Клавиши разделяются пробелом (его пропускает `ПРОБ` программы).
EXIT_KEYS = 'ВЫХ '


def scenario(name: str) -> str:
    """Вход терминала для сценария."""
    table = {
        'СЕТ': 'СЕТ ',
        'СОЛ': 'СОЛ ',
        'КОТ': 'КОТ ',
        'ЦЕП': 'ЦЕП ',
        'ФУН': 'ФУН ',
        'ЗАГ': 'ЗАГ ',
        'СИМ': 'СИМ ',
        'ТЕК': 'ТЕК ПРИВЕТ МС 7401' + chr(255) + ' ',
        'ЛИН': 'ЛИН 3 ',
        'ОКР': 'ОКР 150 ',
        'ГРА': 'ГРА 12 ',
        'ШАР': 'ШАР ',
        'ЛЕД': 'ЛЕД ',
        'УЗО': 'УЗО ',
        'РОЗ': 'РОЗ ',
        'ТИП': 'ТИП 3 СЕТ ',
        'ЧАС': 'ЧАС 12 30 1 150 ',
        # ПТИЦА сама читает два числа с клавиатуры:
        # NN (2…10) и QQ (1…350).
        'ПТИ': 'ПТИ 5 120 ',
    }
    if name not in table:
        raise KeyError(f'неизвестный сценарий {name!r}')
    return table[name] + EXIT_KEYS


SCENARIOS = ['СЕТ', 'СОЛ', 'КОТ', 'ЦЕП', 'ФУН', 'ЗАГ', 'СИМ', 'ТЕК',
             'ЛИН', 'ОКР', 'ГРА', 'ШАР', 'ЛЕД', 'УЗО', 'РОЗ', 'ТИП', 'ЧАС',
             'ПТИ']

DESCRIPTIONS = {
    'СЕТ': 'сетка: рамка и линии через 7 точек',
    'СОЛ': 'солнце: 150 концентрических окружностей',
    'КОТ': 'ко��: криволинейный рисунок',
    'ЦЕП': 'цепь: имитация схемы',
    'ФУН': 'функция: затухающая синусоида с рамкой',
    'ЗАГ': 'заголовок: титульный экран с текстом',
    'СИМ': 'символы: таблица знаков и заказные знаки',
    'ТЕК': 'текст: вывод строки символов',
    'ЛИН': 'линии: образцы типов линий 1…7',
    'ОКР': 'окружности: серия окружностей со смещением окна',
    'ГРА': 'граф: полный граф на N вершинах',
    'ШАР': 'шар: псевдообъёмный шар',
    'ЛЕД': 'леди: фигура «леди»',
    'УЗО': 'узор: фрактальный узор',
    'РОЗ': 'роза: розетка со случайным именем',
    'ТИП': 'тип линии 3 и сетка',
    'ЧАС': 'часы: циферблат со стрелками',
    'ПТИ': 'птица: анимация полёта (кадр для начального экрана)',
}


def generate(source: str, name: str, *, scale: int = 2,
             write_svg: bool = True) -> dict:
    """Выполнить сценарий и сохранить поток и изображения."""
    text = scenario(name)
    started = time.perf_counter()
    try:
        result = run_source(source, text)
        wire = result['wire']
        counted = result['bytes']
    except SystemExit:
        import pascal.runtime as rt
        wire, counted = bytes(rt._terminal.wire), rt._terminal.byte_count
    elapsed = time.perf_counter() - started

    # Оператор видел самую «содержательную» картину: программа очищает
    # экран командой `S` в начале каждой картины и оставляет экран пустым
    # после выхода.  Поэтому запоминаем состояние кадра на границе
    # каждого блока ВУК и берём самое «наполненное».
    from mc7401.reader import CommandStreamReader, EventKind
    emu = TerminalEmulator()
    best = emu.fb.copy()
    best_pts = 0
    reader = CommandStreamReader()
    reader.feed(wire)
    reader.close()
    for ev in reader.pop():
        if ev.kind is EventKind.VUK_BLOCK:
            n = emu.fb.count_set()
            if n > best_pts:
                best, best_pts = emu.fb.copy(), n
        emu.handle_event(ev)
    if emu.fb.count_set() > best_pts:
        best = emu.fb
    fb = best

    STREAMS.mkdir(parents=True, exist_ok=True)
    RENDER.mkdir(parents=True, exist_ok=True)
    (STREAMS / f'{name}.bin').write_bytes(wire)
    PNGBackend(fb, scale=scale).save(RENDER / f'{name}.png')
    if write_svg:
        SVGBackend(fb).save(RENDER / f'{name}.svg')

    bb = fb.bbox()
    return {
        'name': name,
        'wire': len(wire),
        'payload': counted,
        'commands': emu.stats['commands'],
        'unknown': emu.stats['unknown'],
        'chars': emu.stats['chars'],
        'points': fb.count_set(),
        'bbox': bb,
        'seconds': elapsed,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('names', nargs='*', help='имена сценариев')
    ap.add_argument('--source', default=str(GOLDEN / 'graf.pas'),
                    help='исходник на Паскале (по умолчанию golden/graf.pas)')
    ap.add_argument('--scale', type=int, default=2, help='масштаб PNG')
    ap.add_argument('--no-svg', action='store_true', help='не писать SVG')
    ap.add_argument('--list', action='store_true', help='список сценариев')
    args = ap.parse_args(argv)

    if args.list:
        for n in SCENARIOS:
            print(f'{n}  {DESCRIPTIONS.get(n, "")}')
        return 0

    names = args.names or SCENARIOS
    source = translate(args.source)
    (GOLDEN / 'graf.py').write_text(
        '# Сгенерировано tools/gen_golden.py: golden/graf.pas -> Python\n'
        '# Не редактировать вручную.\n\n' + source, encoding='utf-8')

    rows = []
    for name in names:
        try:
            info = generate(source, name, scale=args.scale,
                            write_svg=not args.no_svg)
        except Exception as exc:                      # noqa: BLE001
            print(f'{name}: ОШИБКА {exc}')
            rows.append((name, 'ошибка', str(exc)))
            continue
        rows.append(info)
        bb = info['bbox']
        print(f'{name}: {info["wire"]:7d} Б в линию, '
              f'{info["commands"]:5d} команд, '
              f'{info["points"]:6d} точек, '
              f'кадр {bb}, {info["seconds"]:.1f} с'
              + (f', нераспознано {info["unknown"]}'
                 if info['unknown'] else ''))

    write_index(rows)
    return 0


def write_index(rows) -> None:
    lines = ['# Эталонные кадры', '',
             'Сгенерированы `tools/gen_golden.py` из `golden/graf.pas`.',
             '',
             '| Сценарий | Содержание | Байт в линию | Команд | Тчек | Кадр |',
             '|---|---|---:|---:|---:|---|']
    for row in rows:
        if isinstance(row, tuple):
            lines.append(f'| {row[0]} | — | — | — | — | {row[2]} |')
            continue
        info = row
        bb = info['bbox']
        lines.append(
            f'| [{info["name"]}](render/{info["name"]}.png) | '
            f'{DESCRIPTIONS.get(info["name"], "")} | {info["wire"]} | '
            f'{info["commands"]} | {info["points"]} | '
            f'{bb if bb else "—"} |')
    lines.append('')
    (GOLDEN / 'index.md').write_text('\n'.join(lines), encoding='utf-8')


if __name__ == '__main__':
    raise SystemExit(main())