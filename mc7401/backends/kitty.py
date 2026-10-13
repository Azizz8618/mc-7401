"""
kitty.py — вывод кадра МС 7401 прямо в окно терминала.

Зачем.  Экран терминала — это растр 560×400.  Знакоместами его показать
нельзя: 560 точек по горизонтали и 400 по вертикали не разложились бы
ни по одному знакоместу, а уменьшение в 8 раз по вертикали рассыпает
тонкие линии (это и было видно в псевдографике: вместо птицы — редкие
точки).

Поэтому нужен терминал, умеющий рисовать настоящую картинку.  Протокол
графики kitty поддерживают kitty, WezTerm, Konsole, Ghostty, foot,
Apple Terminal и ряд других.  В этом окне кадр занимает своё
прямоугольник знакомест **как есть**, а над и под ним остаются строки
для текста — то есть графика и текст видны одновременно, в одном окне,
как на настоящем терминале, где текст пишется в тот же растр.

Протокол (APC):

    \\e_G<ключи>;<данные>\\e\\

Данные режутся на куски по 4096 байт; полный набор ключей несёт первый
кусок, продолжения помечены `m=1`, последний — `m=0`.  Кадр кодируется
PNG и передаётся base64.

Проверить в терминале: `printf '\\e_Ga=T,t=f,i=99,s=1,v=1,f=100;%s\\e\\\\' \\
    "$(base64 -w0 tiny.png)"` — если появился цветной квадрат, протокол
работает.
"""

from __future__ import annotations

import base64
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger('mc7401.backends.kitty')

#: Наибольший кусок полезной нагрузки в одном APC (ограничение kitty).
MAX_CHUNK = 4096

#: Начало и конец APC-последовательности.
APC_HEAD = '\x1b_G'
APC_TAIL = '\x1b\\'

#: Настройки кадра по умолчанию, если запрос клетчатого размера не прошёл.
DEFAULT_CELL = (8, 16)


def detect(env: Optional[dict] = None) -> bool:
    """Похоже ли это на терминал с протоколом графики kitty."""
    e = os.environ if env is None else env
    if e.get('KITTY_WINDOW_ID') or e.get('KITTY_LISTEN_ON'):
        return True
    term = (e.get('TERM') or '').lower()
    prog = (e.get('TERM_PROGRAM') or '').lower()
    if 'kitty' in term or 'ghostty' in term:
        return True
    if prog in ('wezterm', 'ghostty', 'iterm.app', 'rio'):
        return True
    if 'foot' in term:
        return True
    if e.get('KONSOLE_VERSION') or e.get('KONSOLE_DBUS_SESSION'):
        return True
    return False


def _apc(keys: str, payload: bytes = b'') -> str:
    """Одна APC-последовательность с полными ключами."""
    data = base64.b64encode(payload).decode('ascii') if payload else ''
    return f'{APC_HEAD}{keys};{data}{APC_TAIL}'


def _apc_stream(keys: str, payload: bytes) -> str:
    """APC с данными, разбитыми на куски.

    По спецификации kitty: полный набор ключей несёт **первый** кусок,
    все куски, кроме последнего, помечены `m=1` («есть продолжение»),
    последний — `m=0`.  Раньше ключи клались в последний кусок без `m`,
    и изображения больше 4 КБ никогда не отображались.
    """
    data = base64.b64encode(payload).decode('ascii')
    if len(data) <= MAX_CHUNK:
        return _apc(keys, payload)
    chunks = [data[i:i + MAX_CHUNK]
              for i in range(0, len(data), MAX_CHUNK)]
    last = len(chunks) - 1
    parts: list[str] = []
    for n, chunk in enumerate(chunks):
        if n == 0:
            head = f'{keys},m=1' if keys else 'm=1'   # ключи — в первом
        elif n == last:
            head = 'm=0'                # последний кусок
        else:
            head = 'm=1'
        parts.append(f'{APC_HEAD}{head};{chunk}{APC_TAIL}')
    return ''.join(parts)


def parse_response(text: str) -> Optional[dict]:
    """Разобрать ответ терминала на запрос графики.

    Возвращает словарь полей (`c`, `r`, ...) либо `None`, если ответа
    нет.  Ответ приходит в виде APC с кодом `OK` или `ENOENT`.
    """
    i = text.find(APC_HEAD)
    if i < 0:
        return None
    j = text.find(APC_TAIL, i)
    if j < 0:
        return None
    body = text[i + len(APC_HEAD):j]
    # Ответ устроен так: `\e_G<первая часть>;<ключи>;<данные>\e\\`.
    # В первой части — только идентификатор, ключи (`OK,c=…,r=…`) стоят
    # во второй, поэтому разбирать надо обе.
    first, _, rest = body.partition(';')
    keys, _, payload = rest.partition(';')
    out: dict = {}
    for part in (first, keys):
        for item in part.split(','):
            item = item.strip()
            if not item:
                continue
            if '=' in item:
                k, _, v = item.partition('=')
                out[k.strip()] = v.strip()
            elif item == 'OK':
                out['OK'] = ''
            else:
                # Ошибка приходит токеном без `=`: `i=31;ENOTSUP`,
                # `i=31;ENOENT:…`.  Раньше такие токены отбрасывались.
                out.setdefault('error', item)
    if not out:
        return None
    out['_payload'] = payload
    return out


class KittyGraphics:
    """Кадр МС 7401 в окне терминала."""

    def __init__(self, stream=None) -> None:
        self.out = stream or sys.stdout
        #: Поддержка подтверждена запросом размера клетки.
        self.active = False
        #: Размер знакоместа в пикселях: (ширина, высота).
        self.cell = DEFAULT_CELL
        self.id = 1
        self._sent: Optional[bytes] = None     # что уже показано

    # ── размер клетки ───────────────────────────────────────────────────────

    def request_cell(self) -> bytes:
        """Запросить размер знакоместа у терминала (запрос kitty)."""
        return (f'{APC_HEAD}i={self.id},a=q,t=d,f=24,s=1,v=1;AAAA'
                f'{APC_TAIL}').encode('ascii')

    def apply_response(self, text: str) -> bool:
        """Разобрать ответ; `True`, если протокол работает.

        Ошибки (`ENOTSUP`, `ENOENT:…`) и ответы без размера клетки
        возвращают `False`: раньше `apply_response('\x1b_Gi=31;ENOTSUP\x1b\\')`
        отвечал `True`, и несуществующая графика считалась включённой.
        """
        resp = parse_response(text)
        if not resp or resp.get('error'):
            return False
        try:
            w = int(resp.get('c', 0))
            h = int(resp.get('r', 0))
        except ValueError:
            return False
        if w <= 0 or h <= 0:
            return False
        self.cell = (w, h)
        self.active = True
        return True

    # ── геометрия ───────────────────────────────────────────────────────────

    def size_for(self, fb_width: int, fb_height: int, *, max_cols: int = 0,
                 max_rows: int = 0, zoom: int = 1) -> tuple[int, int]:
        """Сколько знакомест займёт кадр при заданном увеличении."""
        cw, ch = self.cell
        cols = (fb_width * zoom + cw - 1) // cw
        rows = (fb_height * zoom + ch - 1) // ch
        if max_cols:
            cols = min(cols, max_cols)
        if max_rows:
            rows = min(rows, max_rows)
        return max(1, cols), max(1, rows)

    # ── вывод кадра ─────────────────────────────────────────────────────────

    def blit(self, fb, *, row: int = 0, col: int = 0, cols: int = 0,
             rows: int = 0, zoom: int = 1, force: bool = False) -> bool:
        """Показать кадр `fb` в прямоугольнике знакомест.

        `row`, `col` — левый верхний угол в знакоместах (с нуля), `cols`,
        `rows` — размер прямоугольника размещения (ключи `c`/`r`).
        Кадр пересылается, только если он изменился: по линии ДКС идут
        мегабайты, лишних пересылок быть не должно.

        По спецификации kitty `x,y,w,h` — прямоугольник **исходного**
        изображения (кадр передаётся целиком: `x=y=0`), а место на экране
        задаёт позиция курсора перед APC.  `X,Y` — смещение внутри клетки
        (меньше её размера).
        """
        if not self.active:
            return False
        from . import PNGBackend
        payload = PNGBackend(fb, scale=zoom).to_png_bytes()
        if not force and payload == self._sent:
            return True
        img_w, img_h = fb.width * zoom, fb.height * zoom
        cw, ch = self.cell
        # Кадр центруется в сетке клеток: остаток меньше клетки — это
        # и есть смещение `X,Y` внутри клетки.
        x_off = ((img_w + cw - 1) // cw * cw - img_w) // 2
        y_off = ((img_h + ch - 1) // ch * ch - img_h) // 2
        keys = (f'a=T,f=100,t=d,i={self.id},x=0,y=0,w={img_w},h={img_h}'
                + (f',X={x_off},Y={y_off}' if x_off or y_off else '')
                + (f',c={cols},r={rows}' if cols and rows else '')
                + ',p=2,q=2')
        # Картинка ложится туда, где стоит курсор (позиция — с нуля).
        self.out.write(f'\x1b[{row + 1};{col + 1}H'
                       + _apc_stream(keys, payload))
        self.out.flush()
        self._sent = payload
        return True

    def clear(self) -> None:
        """Убрать картинку из окна."""
        if not self.active:
            return
        self.out.write(f'{APC_HEAD}a=d,d=A,i={self.id}{APC_TAIL}')
        self.out.flush()
        self._sent = None

    # ── вспомогательное ─────────────────────────────────────────────────────

    @staticmethod
    def frame_from_stream(bin_path: Path) -> Optional[object]:
        """Кадр «как видел оператор» из эталонного потока команд.

        Программа `graf.pas` чистит экран перед выходом, поэтому берётся
        самый наполненный кадр, а не последний.
        """
        from ..emulator import TerminalEmulator
        from ..reader import CommandStreamReader, EventKind
        emu = TerminalEmulator()
        reader = CommandStreamReader()
        reader.feed(Path(bin_path).read_bytes())
        reader.close()
        best, best_n = emu.fb.copy(), 0
        for ev in reader.pop():
            if ev.kind is EventKind.VUK_BLOCK:
                n = emu.fb.count_set()
                if n > best_n:
                    best, best_n = emu.fb.copy(), n
            emu.handle_event(ev)
        return best if best_n else emu.fb


__all__ = ['KittyGraphics', 'detect', 'parse_response']