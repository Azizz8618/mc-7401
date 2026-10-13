"""
Тесты инструментов транслятора Паскаля БЭСМ-6 (`tools/pascal/`) и
генератора эталонов (`tools/gen_golden.py`).

Покрываются изменения транслятора и эталонных потоков `ПТИ`, `КИНО`,
`СИМ`:

1. Литерал двумерного массива резался неправильно: строки реплицировались
   целиком, и `ПХ.МС`/`ПУ.МС` (позы крыльев птицы, `ПТИЦА`) давали четыре
   одинаковые строки.  Литерал плоский и заполняет массив по строкам —
   строки должны резаться по размеру второй размерности.

2. `to_wire_char` перекодировал литералы `≡NNN` (NNN ≥ 0o200) через КОИ-8,
   где `U+00A0` попадает на `0x9A`, а `U+00B7` на `0x9E`: служебные маркеры
   потока (`≡240`, `≡267`, `≡215`) искажались.  Теперь символы `0x80…0xFF`
   уходят в линию кодом как есть.

3. В транскрипции `golden/graf.pas` в процедуре `СИМ(N)` было потеряно
   `СНR(N)` (`С:=;М:=;` вместо `С:=СНR(N);М:=;`): байт кода знака не
   попадал в поток после `≡110≡240`.  Правка транскрипции — минимальная.

4. Семантика статических переменных процедур.  **Выводы по исследованию:**
   в Паскале БЭСМ-6/Эльбрус переменные подпрограммы статичны (сохраняются
   между вызовами, если нет рекурсии).  Пример дефекта: `СИМВОЛЫ(МСШ)` в
   `golden/graf.pas` для `МСШ=0` делает `УУ:=340-ШГ*6; ШГ:=20`, рассчитывая
   на `ШГ=30`, установленный в вызове `СИМВОЛЫ(1)`; при обнулении локальных
   переменных второй проход таблицы знаков рисовался поверх первого (y=
   340…240 вместо 160…60).  Транслятор инициализировал локальные переменные
   в начале тела подпрограммы при каждом вызове.

   Решение: переменные **верхнеуровневых** подпрограмм вынесены в замыкание
   фабрики `_static_<имя>` — значение создаётся один раз при загрузке
   модуля и сохраняется между вызовами.  Рекурсивных (прямых и взаимных)
   подпрограмм в `graf.pas` нет — проверено обходом графа вызовов по
   транслированному коду (49 подпрограмм, циклов нет).

   Локальные имена **вложенных** подпрограмм намеренно оставлены с
   инициализацией при каждом вызове:
   - они могут совпадать с именами объёмлющей подпрограммы (`ПТ.К` и
     `ПТИЦА.К`) — общая фабрика дала бы им одну ячейку вместо двух
     разных переменных Паскаля;
   - все локальные переменные вложенных подпрограмм `graf.pas`
     присваиваются до чтения (`ПТ`: `Q:=…`, `К` — счётчик цикла;
     `WС`: `С:=СНR(I);М:=;`; `WS`: `G:=SЕL(…)`), поэтому для них
     инициализация при каждом вызове эквивалентна статичности.

   Эквивалентность подтверждена побайтовым сравнением потоков всех
   сценариев до и после изменения: 14 сценариев из 16 не изменились
   вовсе, изменились только целевые `СИМ` (байт кода знака + маркеры +
   корректный второй проход) и `ПТИ` (позы крыльев).  Семантика
   статических переменных верхнеуровневых подпрограмм считается
   зафиксированной как исправленная; вложенные — как безопасно
   эквивалентная (зафиксировано).

5. Сценарий `КИНО` добавлен в `tools/gen_golden.py`.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

from pascal import run_source, translate                    # noqa: E402
from pascal import runtime as rt                            # noqa: E402
import gen_golden                                          # noqa: E402

STREAMS = ROOT / 'golden' / 'streams'
VK = bytes((0o125,))                    # ВК — разделитель записей

#: Кэш трансляции `golden/graf.pas` (translate() занимает секунды).
_SRC: str | None = None


def graf_source() -> str:
    global _SRC
    if _SRC is None:
        _SRC = translate(str(ROOT / 'golden' / 'graf.pas'))
    return _SRC


def run_graf(name: str) -> bytes:
    """Прогнать `graf.pas` с вводом сценария и вернуть поток в линию."""
    keys = gen_golden.scenario(name)
    try:
        return run_source(graf_source(), keys)['wire']
    except SystemExit:
        return bytes(rt._terminal.wire)


def records(wire: bytes) -> list[bytes]:
    """Записи потока (разделитель — ВК)."""
    return [r.strip() for r in wire.split(VK)]


# ─────────────────────────────────────────────────────────────────────────────
# 2. Кодирование литералов `≡NNN`
# ─────────────────────────────────────────────────────────────────────────────

def test_to_wire_char_keeps_octal_literal_codes():
    """`≡NNN` (NNN ≥ 0o200) уходит в линию кодом NNN без КОИ-8."""
    assert rt.to_wire_char('\xa0') == 0xA0       # ≡240 — начало строки знака
    assert rt.to_wire_char('\xb7') == 0xB7       # ≡267 — глиф WN «7»
    assert rt.to_wire_char('\x8d') == 0x8D       # ≡215 — конец текста/знака
    assert rt.to_wire_char('\xaf') == 0xAF       # ≡257 — хвост ВНУК
    assert rt.to_wire_char('\x48') == 0x48       # ≡110 — загрузка знака


def test_to_wire_char_still_encodes_letters_in_koi8():
    """Буквы по-прежнему кодируются в КОИ-8, `CHAR_MAP` не тронут."""
    assert rt.to_wire_char('И') == 0xE9          # кириллица → КОИ-8
    assert rt.to_wire_char('П') == 0xF0
    assert rt.to_wire_char('A') == 0x41
    assert rt.to_wire_char('7') == 0x37
    assert rt.to_wire_char('≠') == 0o256          # НЕР — инвертирующее перо
    assert rt.to_wire_char('~') == 0o176


# ─────────────────────────────────────────────────────────────────────────────
# 1. Литералы двумерных массивов
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('var, expected', [
    ('ПХ', [[-5, -3, 0, 3, 5],
            [-6, -3, 0, 3, 6],
            [-7, -3, 0, 3, 7],
            [-8, -4, 0, 4, 8]]),
    ('ПУ', [[4, 1, -4, 1, 4],
            [2, 3, -3, 3, 2],
            [0, 2, -2, 2, 0],
            [-1, 1, -1, 1, -1]]),
])
def test_2d_array_literal_is_split_into_rows(var, expected):
    """Строки литерала `[1..4,0..4]` режутся по второй размерности.

    `ПХ`/`ПУ` в `ПТИЦА` — четыре позы крыльев (взмахи `J=1,2,3,4,3,2`);
    при репликации строки литерала получались четыре одинаковые позы.
    """
    src = graf_source()
    m = re.search(re.escape(var) + r" = \{'МС': (\[\[.*?\]\])\}", src)
    assert m, f'литерал {var}.МС не найден в трансляции'
    rows = ast.literal_eval(m.group(1))
    assert rows == expected


# ─────────────────────────────────────────────────────────────────────────────
# 3. Блоки знаков в потоке СИМ
# ─────────────────────────────────────────────────────────────────────────────

#: Целевой формат блока знака:
#: `0x48 0xA0 <код знака> 0xB7 0xB7` + 7 × (`0xA0` + 3 глифа WN) + `0x8D`.
SIM_BLOCK = re.compile(rb'\x48\xa0(.)\xb7\xb7(?:\xa0...){7}\x8d', re.S)

#: Глифы WN для значений 0…9 (таблица `WN` в `СПЕЦ`, как в ПЗУ знаков:
#: `060 261 262 063 264 065 066 267 270 071`).
WN_GLYPHS = {0o60, 0o261, 0o262, 0o63, 0o264, 0o65, 0o66, 0o267, 0o270,
             0o71}

#: Коды знаков из `СИМ(N)`: 3, 6, 7, 10В, 11В, 13В, 2, 1, 4, 5.
SIM_CODES = [3, 6, 7, 8, 9, 11, 2, 1, 4, 5]


@pytest.mark.parametrize('wire', [
    pytest.param(None, id='сгенерированный'),
    pytest.param('golden', id='эталонный СИМ.bin'),
])
def test_sim_symbol_blocks_format(wire):
    """Блоки знаков `СИМ` имеют формат `48 A0 код B7 B7 … 8D`.

    После `≡110≡240` идёт байт кода знака (задача 3: `С:=СНR(N)` терялся
    при транскрипции и `WRIТЕ(С)` писал пустоту), затем `≡267≡267`, семь
    строк знака по три глифа WN и `≡215`.
    """
    data = (STREAMS / 'СИМ.bin').read_bytes() if wire == 'golden' \
        else run_graf('СИМ')
    blocks = list(SIM_BLOCK.finditer(data))
    assert len(blocks) == len(SIM_CODES), 'блоков знаков не 10'
    assert [m.group(1)[0] for m in blocks] == SIM_CODES
    for m in blocks:
        for row in range(7):
            glyph = m.group(0)[5 + row * 4 + 1:5 + row * 4 + 4]
            assert set(glyph) <= WN_GLYPHS, f'не-глиф WN: {glyph.hex()}'


def test_sim_stream_markers_are_canonical():
    """Служебные маркеры — эталонные коды, без перекодировки КОИ-8.

    Раньше `≡240` попадал в линию как `0x9A`, а `≡267` как `0x9E`.
    """
    data = (STREAMS / 'СИМ.bin').read_bytes()
    assert b'\x9a' not in data and b'\x9e' not in data
    assert data.count(b'\x48\xa0') == 10          # ≡110≡240 — загрузка знака
    assert data.count(b'\xb7\xb7') == 10          # ≡267≡267
    assert data.count(b'\x1b\xc6\xde\x8d\xbb\xa0') == 13   # ≡033≡306≡336≡215≡273≡240
    assert data.count(b'\xaf\x8d') == 14          # ВНУК: ≡257≡215 + 14 нулей


# ─────────────────────────────────────────────────────────────────────────────
# ПТИ: позы крыльев
# ─────────────────────────────────────────────────────────────────────────────

#: Позы `ПХ.МС`/`ПУ.МС` из `ПТИЦА` (нормированные на начало силуэта).
POSES = {
    1: [(-5, 4), (-3, 1), (0, -4), (3, 1), (5, 4)],
    2: [(-6, 2), (-3, 3), (0, -3), (3, 3), (6, 2)],
    3: [(-7, 0), (-3, 2), (0, -2), (3, 2), (7, 0)],
    4: [(-8, -1), (-4, 1), (0, -1), (4, 1), (8, -1)],
}


def bird_silhouettes(wire: bytes) -> list[list[tuple[int, int]]]:
    """Силуэты птиц: `4 X Y` (ТЕКТОЧ) + четыре `7 T X Y` (ЛИНТ)."""
    recs = records(wire)
    birds = []
    i = 0
    while i < len(recs) - 4:
        m0 = re.fullmatch(rb'4 (-?\d+) (-?\d+)', recs[i])
        if m0:
            pts = [(int(m0.group(1)), int(m0.group(2)))]
            ok = True
            for k in range(1, 5):
                m = re.fullmatch(rb'7(\d) (-?\d+) (-?\d+)', recs[i + k])
                if not m:
                    ok = False
                    break
                pts.append((int(m.group(2)), int(m.group(3))))
            if ok:
                birds.append(pts)
                i += 5
                continue
        i += 1
    return birds


def match_pose(pts: list[tuple[int, int]]) -> tuple[int, float]:
    """Опознать позу силуэта (нормировка на масштаб Q и смещение)."""
    x0, y0 = pts[0]
    width = pts[-1][0] - x0
    norm = [((x - x0) / width, (y - y0) / width) for x, y in pts]

    def err(ref):
        x0r, y0r = ref[0]
        wr = ref[-1][0] - x0r
        rn = [((x - x0r) / wr, (y - y0r) / wr) for x, y in ref]
        return max(max(abs(a - b) for (a, _), (b, _) in zip(norm, rn)),
                   max(abs(a - b) for (_, a), (_, b) in zip(norm, rn)))

    best = min(POSES, key=lambda p: err(POSES[p]))
    return best, err(POSES[best])


def test_pti_wing_poses_cycle():
    """Силуэты птиц используют разные позы 1,2,3,4,3,2.

    Взмах `J:=(К МОD (NП+2))+1` при `J>NП` заворачивается (`J:=ДNП-J`),
    поэтому позы для K=0…5 идут циклом 1,2,3,4,3,2.  Раньше литерал
    `ПХ.МС` реплицировался целиком — все 50 птиц рисовались одной позой.
    """
    data = (STREAMS / 'ПТИ.bin').read_bytes()
    birds = bird_silhouettes(data)
    assert len(birds) >= 12, 'силуэтов птиц в потоке не найдено'
    # Каждая птица рисуется дважды: пером `+` и стирается пером `≠`.
    drawn = birds[::2]
    seq = [match_pose(b)[0] for b in drawn[:6]]
    assert seq == [1, 2, 3, 4, 3, 2], f'позы K=0..5: {seq}'
    # Четыре позы различны между собой.
    assert len({match_pose(b)[0] for b in drawn[:4]}) == 4
    # Все силуэты ближе к одной из эталонных поз (с учётом округления
    # координат при малом масштабе Q).
    assert all(match_pose(b)[1] < 0.12 for b in birds), 'чужой силуэт'


def test_pti_stream_markers_are_canonical():
    data = (STREAMS / 'ПТИ.bin').read_bytes()
    assert b'\x9a' not in data and b'\x9e' not in data


# ─────────────────────────────────────────────────────────────────────────────
# 5. Сценарий КИНО
# ─────────────────────────────────────────────────────────────────────────────

def test_kino_scenario_is_registered():
    """`КИНО` есть в списке сценариев с вводом `КИН 15 120 `."""
    assert 'КИНО' in gen_golden.SCENARIOS
    assert gen_golden.DESCRIPTIONS['КИНО'] == 'кино: движение окружности'
    assert gen_golden.scenario('КИНО') == 'КИН 15 120 ' + gen_golden.EXIT_KEYS


def test_kino_draws_moving_circles():
    """Кадр `КИНО` непуст, окружности рисуются в разных точках.

    Процедура `КИНО` (`ДВИЖЕНИЕ ОКРУЖНОСТИ`) читает `NN` (радиус) и `QQ`
    (паузу) и рисует `ОКР(NN)` в 29 точках вдоль диагонали.
    """
    data = (STREAMS / 'КИНО.bin').read_bytes()
    assert b'\x9a' not in data and b'\x9e' not in data
    recs = records(data)
    centers = set()
    prev = None
    circles = 0
    for rec in recs:
        m = re.fullmatch(rb'4 (-?\d+) (-?\d+)', rec)
        if m:
            prev = (int(m.group(1)), int(m.group(2)))
        elif re.fullmatch(rb'>\d+ \d+', rec):
            circles += 1
            centers.add(prev)
    assert circles >= 29, 'окружностей меньше, чем кадров движения'
    assert len(centers) >= 25, 'окружности не движутся'
    fb, _emu = gen_golden.pick_frame(data)
    assert fb.count_set() > 1000, 'кадр КИНО пуст'
    assert (ROOT / 'golden' / 'render' / 'КИНО.png').stat().st_size > 0
    assert (ROOT / 'golden' / 'render' / 'КИНО.svg').stat().st_size > 0


# ─────────────────────────────────────────────────────────────────────────────
# 4. Статические переменные процедур
# ─────────────────────────────────────────────────────────────────────────────

MINI_COUNTER = (
    "_РRОСЕD СЧЁТ;_VАR N:INТ;_(N:=N+1;WRIТЕ(N)_);\n"
    "_(СЧЁТ;СЧЁТ;СЧЁТ_)\n"
)


def test_local_variables_survive_between_calls():
    """Переменная процедуры сохраняется между вызовами (Паскаль БЭСМ-6).

    `СЧЁТ` вызывается трижды: при статичности переменной поток получает
    `123`, при обнулении при каждом вызове — `111`.
    """
    src = translate(MINI_COUNTER, is_path=False)
    wire = run_source(src, '')['wire']
    assert b'123' in bytes(wire), 'локальная переменная обнуляется'


def test_no_recursive_routines_in_graf():
    """Рекурсии нет — статичность переменных безопасна (см. докстринг)."""
    import ast as _ast
    tree = _ast.parse(graf_source())
    funcs = {n.name: n for n in tree.body if isinstance(n, _ast.FunctionDef)}

    def calls(node):
        return {c.func.id for c in _ast.walk(node)
                if isinstance(c, _ast.Call) and isinstance(c.func, _ast.Name)}

    graph = {name: calls(f) for name, f in funcs.items()}

    def reaches(start, target, seen=frozenset()):
        for c in graph.get(start, ()):
            if c == target:
                return True
            if c not in seen:
                if reaches(c, target, seen | {c}):
                    return True
        return False

    recursive = [n for n in funcs if reaches(n, n)]
    assert recursive == []


def test_static_variables_fix_second_table_pass():
    """Второй проход `СИМВОЛЫ(0)` идёт ниже первого, а не поверх него.

    `УУ:=340-ШГ*6` при сохранившемся `ШГ=30` даёт старт 160 и шаг 20:
    строки на y=160…40.  При обнулении локальных (`ШГ=0`) строки шли
    на y=340…240 — поверх таблицы первого прохода.
    """
    data = run_graf('СИМ')
    ys = [int(m.group(1)) for m in
          re.finditer(rb'\x554 40 (-?\d+)\x55', b'\x55' + data + b'\x55')]
    assert 160 in ys and 140 in ys and 60 in ys, 'второй проход не там'
    assert 320 not in ys, 'второй проход наложен на первый'


# ─────────────────────────────────────────────────────────────────────────────
# Целостность эталонов: потоки свежие
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('name', ['ПТИ', 'КИНО', 'СИМ'])
def test_golden_stream_matches_fresh_run(name):
    """`golden/streams/<имя>.bin` совпадает с прогоном `graf.pas`."""
    fresh = run_graf(name)
    golden = (STREAMS / f'{name}.bin').read_bytes()
    assert fresh == golden, (
        f'{name}: эталонный поток устарел — перегенерируйте '
        f'`python3 tools/gen_golden.py {name}`')
