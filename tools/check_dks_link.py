#!/usr/bin/env python3
"""
check_dks_link.py — проверка связи эмулятора МС 7401 с БЭСМ-6 по ДКС.

Сценарий проверки:
  1. опрос портов линии ДКС (по умолчанию 4202-4223) с разбором отказа;
  2. подключение к первой свободной линии;
  3. приём данных и разбор потока команд терминала;
  4. отчёт: сколько байт, сколько команд, сколько кадров ВУК.

ЭВМ слушает порт, терминал к нему подключается (`TMXR` в
`BESM6/besm6_tty.c`, режим `set ttyN dks,raw`).

    python3 tools/check_dks_link.py
    python3 tools/check_dks_link.py --ports 4203 --seconds 30
    python3 tools/check_dks_link.py --render link.png
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mc7401 import TerminalEmulator                              # noqa: E402
from mc7401.link import (DKSLink, LinkError, parse_port_spec,   # noqa: E402
                         spec_hint, split_endpoint)
from mc7401.reader import CommandStreamReader, EventKind         # noqa: E402

log = logging.getLogger('mc7401.dks')


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description='Проверка связи с БЭСМ-6 по каналу ДКС.')
    ap.add_argument('endpoint', nargs='?', default='127.0.0.1',
                    help='хост или хост:порты (по умолчанию 127.0.0.1)')
    ap.add_argument('--ports', default=None,
                    help='порты линий ДКС, например 4202-4223')
    ap.add_argument('--seconds', type=float, default=30.0,
                    help='сколько ждать данных, с (0 — до обрыва)')
    ap.add_argument('--render', metavar='FILE',
                    help='сохранить последний кадр в PNG')
    ap.add_argument('--dump', metavar='FILE',
                    help='сохранить принятый поток в файл')
    ap.add_argument('-v', '--verbose', action='count', default=0)
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=(logging.DEBUG if args.verbose > 1 else
               logging.INFO if args.verbose else logging.WARNING),
        format='%(message)s')

    if args.ports:
        host, ports = split_endpoint(args.endpoint)[0], parse_port_spec(args.ports)
    else:
        host, ports = split_endpoint(args.endpoint)
    print(f'линии ДКС: {host}:{spec_hint(ports)}')

    # 1-2. подключение
    try:
        link = DKSLink(host, ports, timeout=10.0)
        link.connect()
    except LinkError as exc:
        print(f'НЕ СВЯЗЬ: {exc}', file=sys.stderr)
        print('Проверьте, что эмулятор БЭСМ-6 запущен, линия в режиме ДКС '
              '(`set ttyN dks,raw`) и порт свободен.', file=sys.stderr)
        return 2
    print(f'подключено: {link.host}:{link.port}')

    # 3. приём и разбор
    emu = TerminalEmulator()
    reader = CommandStreamReader()
    total = 0
    kinds: dict[str, int] = {}
    deadline = time.monotonic() + args.seconds if args.seconds else None
    raw = bytearray()

    def drain() -> None:
        nonlocal total
        for ev in reader.pop():
            kinds[ev.kind.name] = kinds.get(ev.kind.name, 0) + 1
            if ev.kind is EventKind.COMMAND:
                emu.execute(emu.parser.parse(ev.data))
            else:
                emu.handle_event(ev)

    try:
        with link:
            while True:
                if deadline and time.monotonic() > deadline:
                    break
                try:
                    data = link.recv()
                except TimeoutError:
                    continue
                if not data:
                    print('ЭВМ закрыла линию')
                    break
                total += len(data)
                raw += data
                reader.feed(data)
                drain()
                print(f'\rпринято {total} Б, команд {emu.stats["commands"]}, '
                      f'блоков {kinds.get("VUK_BLOCK", 0)}   ',
                      end='', flush=True)
            reader.close()
            drain()
    except KeyboardInterrupt:
        print('\nпрервано')
    finally:
        print()

    # 4. отчёт
    print(f'принято байтов : {total}')
    print(f'команд разобрано: {emu.stats["commands"]}')
    print(f'символов знаков : {emu.stats["chars"]}')
    print(f'блоков ВУК      : {kinds.get("VUK_BLOCK", 0)}')
    print(f'границ кадра    : {kinds.get("FRAME_END", 0)}')
    print(f'записей в кадре : {emu.frames}')
    print(f'точек на экране : {emu.fb.count_set()}')
    if emu.stats['unknown']:
        print(f'нераспознано    : {emu.stats["unknown"]}')

    if args.dump:
        Path(args.dump).write_bytes(bytes(raw))
        print(f'поток сохранён  : {args.dump} ({len(raw)} Б)')
    if args.render:
        from mc7401.backends import PNGBackend
        PNGBackend(emu.fb, scale=2).save(args.render)
        print(f'кадр сохранён   : {args.render}')

    return 0 if total else 1


if __name__ == '__main__':
    raise SystemExit(main())