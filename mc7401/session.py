"""
session.py — режимы работы терминала МС 7401 (по образцу 15ИЭ-00-013).

В терминале «Электроника-15ИЭ» (`ie15/ie15emu/session.py`) три вещи,
которые воспроизводятся здесь дословно:

* **АВТОНОМНО (LOCAL)** — набор идёт в локальный буфер и сразу на экран,
  обмена с линией нет;
* **С ЭВМ (HOST)** — каждый знак немедленно уходит в линию, эхо
  приходит от ЭВМ;
* **SEND** — «передать накопленное»: байты буфера уходят в линию, буфер
  очищается (на клавиатуре 15ВВВ-97-006 это отдельная клавиша SEND).

Из функций терминала воспроизведены также **ЭХО** (вывод набираемого на
экран — снимается, чтобы не печатать пароли) и **ОЧИСТКА** (команда `S`).

Сеанс не знает о транспорте: он возвращает байты для линии, а отправку
делает вызывающая сторона.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import chars
from .constants import ETX, VK

MODE_LOCAL = 'local'
MODE_HOST = 'host'

#: Подписи режимов на панели.
MODE_LABELS = {MODE_LOCAL: 'АВТОНОМНО', MODE_HOST: 'С ЭВМ'}


@dataclass
class Session:
    """Состояние сеанса терминала: режим, буфер, эхо, раскладка."""

    mode: str = MODE_HOST
    #: Накопленный автономный набор; в режиме HOST не растёт.
    buffer: list[str] = field(default_factory=list)
    #: Локальное эхо набираемого: терминал печатает знак сам.
    #:
    #: Ф6 (ЭХО) — вкл/выкл.  Режим «авто» упразднён: эхо не зависит
    #: от режима сеанса, а только от положения Ф6.
    echo: bool = False
    #: Русская раскладка (ФОН/ПОЗ) — как в терминале 15ИЭ.
    layout: Optional[str] = None
    #: Набор **ввода**: 'n2' (Н2, КОИ-7) | 'koi8r' (КОИ-8Р).
    #:
    #: Н2 — набор линии ДКС: семибитный, и в нём строчная латинская
    #: уходит заглавной (`chars.KOI7_LAT_UPPER`), иначе набранное вернулось
    #: бы кириллицей.  КОИ-8Р оператор выбирает сам (F8 «НАБОР»), когда
    #: нужен регистр латиницы.
    charset_set: str = chars.INPUT_SET

    def __post_init__(self) -> None:
        self._check_layout()

    # ── режим ───────────────────────────────────────────────────────────────

    def set_mode(self, mode: str) -> bytes:
        """Установить режим; вернуть накопленное для линии.

        При переходе в HOST накопленное не теряется: оно уходит
        вызывающему — сеанс не знает о транспорте, отправку в линию
        делает вызывающая сторона.  Новый режим — в `self.mode`.

        Обратный переход (HOST → LOCAL) накопленное **не** трогает: буфер
        сеанса в С ЭВМ не растёт, и SEND его тоже не отдаёт.
        """
        if mode not in (MODE_LOCAL, MODE_HOST):
            raise ValueError(f'неизвестный режим: {mode!r}')
        out = self.send(line_end=0) if mode == MODE_HOST else b''
        self.mode = mode
        self._check_layout()
        return out

    def toggle_mode(self) -> bytes:
        """Переключить режим; вернуть накопленное для линии.

        Новый режим — в `self.mode`.  При уходе из АВТОНОМНО в С ЭВМ
        набранное уходит вызывающему (без символа конца строки — набор
        может продолжаться в режиме С ЭВМ).
        """
        if self.mode == MODE_LOCAL:
            return self.set_mode(MODE_HOST)
        self.mode = MODE_LOCAL
        self._check_layout()
        return b''

    # ── набор ───────────────────────────────────────────────────────────────

    def type_key(self, ch: str) -> bytes:
        """Обработать нажатие; возвращает байты, которые надо послать.

        В режиме HOST знак уходит сразу.  В режиме LOCAL — копится в
        буфере и уходит только по SEND.
        """
        if self.mode == MODE_HOST:
            return chars.encode_input(ch, set_name=self.charset_set)
        self.buffer.append(ch)
        return b''

    def type_line(self, text: str) -> bytes:
        """Обработать целую строку (до `ВК`)."""
        if self.mode == MODE_HOST:
            return chars.encode_input(text, set_name=self.charset_set)
        self.buffer.extend(text)
        return b''

    def pending(self) -> bytes:
        """Что накоплено в автономном режиме (без `ВК`)."""
        return chars.encode_input(''.join(self.buffer),
                                    set_name=self.charset_set)

    # ── SEND ────────────────────────────────────────────────────────────────

    def send(self, *, line_end: int = 0o015) -> bytes:
        """SEND: передать накопленное в линию и вернуться в **С ЭВМ**.

        `line_end` — символ конца строки на линии ДКС; по умолчанию ПР
        (0o015): по эталону 15ИЭ (`ie15emu/charset.py::to_line`, ветка
        dks) окончание строки на ДКС — ПР, ETX для линии просто знак.
        Значение 0 — не добавлять ничего.

        **После передачи режим сеанса — С ЭВМ.**  Иначе терминал остаётся
        в АВТОНОМНО, и следующий набор снова копится в буфере и ждёт
        ПЕРЕДАЧА: на живой линии это читалось как «передача не работает
        после первого раза».  На 15ИЭ SEND не только отдаёт буфер, но и
        снимает автономный режим (клавиша «S» на клавиатуре 15ВВВ-97-006),
        и `ПРПС` из АВТОНОМНО ведёт себя так же.
        """
        data = self.pending()
        if line_end and data:
            data += bytes((line_end,))
        self.buffer.clear()
        if self.mode != MODE_HOST:
            self.mode = MODE_HOST
            self._check_layout()
        return data

    def reset_buffer(self) -> None:
        self.buffer.clear()

    # ── экран ───────────────────────────────────────────────────────────────

    def clear_command(self) -> bytes:
        """ОЧИСТКА: команда `S` очистки экрана.

        В режиме LOCAL — только локальная очистка, в HOST — команда
        уходит на ЭВМ, чтобы экран очистился у источника.
        """
        if self.mode == MODE_LOCAL:
            return b''
        return b'S' + bytes((VK,))

    def set_echo(self, value: bool) -> bool:
        """Задать эхо вручную (Ф6)."""
        self.echo = bool(value)
        return self.echo

    def toggle_echo(self) -> bool:
        """Переключить эхо (Ф6) — на пароль и обратно."""
        return self.set_echo(not self.echo)

    # ── набор знаков и раскладка ────────────────────────────────────────────

    def set_charset(self, name: str) -> None:
        """Переключить набор **ввода**: Н2 (КОИ-7) или КОИ-8Р.

        Легаси-значения 'n0'/'n1' (старые наборы 15ИЭ) нормализуются в
        'n2' — набор линии ДКС, чтобы старые конфиги не ломали запуск.
        """
        from .chars import INPUT_SET, SETS
        if name in ('n0', 'n1'):
            name = INPUT_SET
        if name not in SETS:
            name = INPUT_SET
        self.charset_set = name
        self._check_layout()

    def toggle_charset(self) -> str:
        """F8 «НАБОР»: Н2 (КОИ-7) ↔ КОИ-8Р; вернуть новый набор.

        В Н2 строчная латинская набирается заглавной (на линии ДКС
        строчных латинских нет), в КОИ-8Р — как нажата.
        """
        from .chars import INPUT_SET, SETS
        i = SETS.index(self.charset_set)
        self.set_charset(SETS[(i + 1) % len(SETS)])
        return self.charset_set

    def echo_char(self, ch: str) -> str:
        """Знак так, как его покажет терминал в текущем наборе.

        Локальное эхо рисует **это** (`chars.input_label`): в Н2
        `cat` встанет как `CAT`, ровно тем, что вернёт машина.
        """
        return chars.input_label(ch, set_name=self.charset_set)

    def _check_layout(self) -> None:
        """Русская раскладка есть в обоих наборах ввода."""
        if self.layout is None:
            self.layout = self.default_layout()

    @staticmethod
    def default_layout() -> str:
        return 'phonetic'          # ФОН — по умолчанию, как в 15ИЭ

    # ── панель ──────────────────────────────────────────────────────────────

    def panel(self) -> str:
        """Сведения для строки состояния.

        Про эхо сказано прямо — **кто** echoes: в С ЭВМ по умолчанию
        эхо делает ЭВМ, и подпись «эхо» без уточнения читалась бы как
        «эхо выключено совсем».
        """
        from .chars import SET_LABELS
        if self.echo:
            echo = 'эхо терминала'
        elif self.mode == MODE_HOST:
            echo = 'эхо ЭВМ'
        else:
            echo = 'эхо выкл'
        bits = [MODE_LABELS[self.mode],
                SET_LABELS.get(self.charset_set, self.charset_set),
                echo]
        if self.layout:
            bits.append('ФОН' if self.layout == 'phonetic' else 'ПОЗ')
        if self.buffer:
            bits.append(f'ввод {len(self.buffer)}')
        return ' '.join(bits)

__all__ = ['Session', 'MODE_LOCAL', 'MODE_HOST', 'MODE_LABELS']