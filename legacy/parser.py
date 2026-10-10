"""
parser.py — разбор команд терминала «Электроника МС 7401».

Принимает сырые команды от CommandStreamReader и превращает их
в объекты Command. Поддерживает два способа кодирования параметров:

    1. Десятичное — параметры разделяются пробелами (П1 = ' ').
       Пример: b"7 1 100 200"

    2. Двоичное — параметры передаются 7-битными байтами.
       Пример: b"7" + b"\\x01" + b"\\x64" + b"\\xc8"

Признак конца команды — ВК (0o125), который уже удалён reader'ом.

Формат команды:
    <op> [<args>]

где <op> — ASCII-символ или двухбуквенный код (J0, J1, F‾).
Мнемоники в исходниках graf.pas могут быть на русском, но в
самом потоке команд всегда используется ASCII.

Автор: <ваше имя>
Лицензия: MIT
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from reader import (
    CommandStreamReader,
    VK, S0, S1, END, LF, SP, CR, NL,
    decode_text,
    split_by_font,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Ошибки
# ─────────────────────────────────────────────────────────────────────────────

class ParseError(ValueError):
    """Ошибка разбора команды."""
    def __init__(self, msg: str, raw: bytes = b"", index: int = 0):
        super().__init__(msg)
        self.raw = raw
        self.index = index


# ─────────────────────────────────────────────────────────────────────────────
# Объект команды
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Command:
    """
    Разобранная команда терминала.

    Поля:
        op       — мнемоника/символ операции (например, '7', 'J1', ']', '9')
        args     — числовые параметры
        text     — текст (только для команды '9')
        font     — активный шрифт в момент вывода текста ('EN'/'RU')
        raw      — сырые байты команды (для отладки)
        index    — порядковый номер команды в потоке
    """
    op: str
    args: list[int] = field(default_factory=list)
    text: Optional[str] = None
    font: str = "EN"
    raw: bytes = b""
    index: int = 0

    # удобные псевдонимы
    @property
    def kind(self) -> str:
        return self.op

    def __repr__(self) -> str:
        s = f"Command(#{self.index} {self.op!r}"
        if self.args:
            s += f" args={self.args}"
        if self.text is not None:
            s += f" text={self.text!r} font={self.font}"
        s += ")"
        return s


# ─────────────────────────────────────────────────────────────────────────────
# Категории операций
# ─────────────────────────────────────────────────────────────────────────────

# Команды, у которых все аргументы — целые числа.
# Для каждой указано (минимальное, максимальное) число аргументов.
# None означает отсутствие верхней границы.
NUMERIC_COMMANDS: dict[str, tuple[int, int | None]] = {
    "4": (2, 2),   # X Y
    "5": (2, 2),   # dX dY
    "7": (3, 3),   # T X Y
    "8": (3, 3),   # T dX dY
    "]": (4, 4),   # X1 Y1 X2 Y2
    ">": (1, 2),   # T [R]     (T по умолчанию 1)
    "0": (0, 1),   # [C]       (перо)
    "1": (0, 1),   # [T]       (тип линии)
    "}": (2, 2),   # X Y       (смещение окна)
}

# Команды без аргументов.
NULLARY_COMMANDS: set[str] = {
    "2",    # конец кадра
    "+",    # OR
    "≠",    # XOR
    "S",    # очистка
    "K",    # сброс
    "F",    # конец/заполнение
}

# Префиксные команды режима — J0 и J1.
MODE_COMMANDS: set[str] = {"J0", "J1"}


# ─────────────────────────────────────────────────────────────────────────────
# Парсер
# ─────────────────────────────────────────────────────────────────────────────

class CommandParser:
    """
    Разбирает поток команд.

    Использование:
        reader = CommandStreamReader(data)
        parser = CommandParser(reader)
        while True:
            cmd = parser.parse()
            if cmd is None:
                break
            ...
    """

    __slots__ = ("_reader", "_index", "_auto_binary")

    def __init__(self, reader: CommandStreamReader, *, auto_binary: bool = False):
        self._reader = reader
        self._index = 0
        # Если True, то десятичные параметры при отсутствии пробелов
        # пытаемся интерпретировать как двоичные байты.
        self._auto_binary = auto_binary

    # ─── Свойства ────────────────────────────────────────────────────────────

    @property
    def reader(self) -> CommandStreamReader:
        return self._reader

    @property
    def index(self) -> int:
        return self._index

    # ─── Основной метод ──────────────────────────────────────────────────────

    def parse(self) -> Optional[Command]:
        """
        Прочитать и разобрать одну команду.

        Возвращает:
            Command  — успешно разобранная команда;
            None     — поток исчерпан.

        Бросает ParseError при ошибке разбора.
        """
        raw = self._reader.read_raw_command()
        if raw is None:
            return None

        self._index += 1
        try:
            return self._parse_raw(raw)
        except ParseError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ParseError(
                f"Непредвиденная ошибка разбора: {exc}",
                raw=raw, index=self._index,
            ) from exc

    # ─── Разбор ──────────────────────────────────────────────────────────────

    def _parse_raw(self, raw: bytes) -> Command:
        """Разобрать сырые байты одной команды."""
        # Пустая команда — допустима, но пропускается.
        if not raw:
            return Command(op="", raw=raw, index=self._index)

        # 1. Проверяем префиксные команды из двух символов: J0, J1, F‾
        first = raw[0]
        if first == 0x4A:  # 'J'
            return self._parse_j_command(raw)

        # 2. Операция — первый байт; остаток — параметры.
        op_byte = first
        op = self._decode_op(op_byte)
        rest = raw[1:]

        # 3. Диспетчеризация по типу команды.
        if op in NULLARY_COMMANDS:
            return Command(op=op, args=[], raw=raw, index=self._index)

        if op in NUMERIC_COMMANDS:
            args = self._parse_numeric_args(rest, NUMERIC_COMMANDS[op])
            return Command(op=op, args=args, raw=raw, index=self._index)

        if op == "9":
            # Команда текста занимает две строки. В graf.pas:
            #   WRIТЕ('9', S, ВК, ';', O, ' ', текст, ВК)
            # То есть reader уже вернул только первую строку "9 <S>",
            # а вторая строка ";<O> <текст>" ещё не прочитана.
            return self._parse_text_command(rest, raw)

        # Неизвестная команда — пробуем трактовать как поток текста.
        if self._looks_like_text(raw):
            text = decode_text(raw)
            return Command(op="?", args=[], text=text, raw=raw, index=self._index)

        # Совсем непонятно — возвращаем как есть, чтобы вызывающий код
        # мог решить, что делать.
        log.warning("Неизвестная команда #%d: %r", self._index, raw)
        return Command(op=op, args=[], raw=raw, index=self._index)

    # ─── J-команды (J0, J1, F‾) ──────────────────────────────────────────────

    def _parse_j_command(self, raw: bytes) -> Command:
        if len(raw) >= 2:
            op = raw[:2].decode("ascii", errors="replace")
            if op in MODE_COMMANDS:
                return Command(op=op, args=[], raw=raw, index=self._index)
        # иначе — не J0/J1, вернём как одиночную 'J'
        return Command(op="J", args=[], raw=raw, index=self._index)

    # ─── Числовые аргументы ──────────────────────────────────────────────────

    def _parse_numeric_args(
        self,
        rest: bytes,
        counts: tuple[int, int | None],
    ) -> list[int]:
        """
        Разобрать параметры команды. Поддерживает:
            - десятичное кодирование: пробелы-разделители, ASCII-цифры;
            - двоичное кодирование: 7-битные байты, разделителей нет;
            - смешанный случай: сначала десятичные, потом двоичные.
        """
        min_args, max_args = counts

        # 1. Пустой остаток — параметров нет.
        if not rest:
            if min_args > 0:
                raise ParseError(
                    f"Ожидалось минимум {min_args} параметров",
                    index=self._index,
                )
            return []

        # 2. Пробуем десятичное кодирование.
        text = rest.decode("latin-1", errors="replace")
        stripped = text.strip()

        if stripped and all(
            ch.isdigit() or ch in " \t+-" for ch in stripped
        ):
            parts = stripped.replace("\t", " ").split()
            try:
                args = [int(p) for p in parts if p]
            except ValueError as exc:
                raise ParseError(
                    f"Некорректный числовой параметр: {stripped!r}",
                    raw=rest, index=self._index,
                ) from exc

            if len(args) < min_args:
                raise ParseError(
                    f"Мало параметров: {len(args)} < {min_args}",
                    raw=rest, index=self._index,
                )
            if max_args is not None and len(args) > max_args:
                # обрезаем лишние, но сообщаем
                log.debug(
                    "Команда #%d: получено %d параметров, ожидалось до %d",
                    self._index, len(args), max_args,
                )
                args = args[:max_args]
            return args

        # 3. Двоичное кодирование: 7-битные байты.
        args = [b & 0x7F for b in rest]

        # 3a. Возможен ведущий пробел (десятичный разделитель перед
        #     двоичными байтами). Например, b' 1 2' — с точки зрения
        #     двоичного это тоже валидно.
        if not args:
            if min_args > 0:
                raise ParseError(
                    f"Мало параметров: 0 < {min_args}",
                    raw=rest, index=self._index,
                )
            return []

        if len(args) < min_args:
            raise ParseError(
                f"Мало двоичных параметров: {len(args)} < {min_args}",
                raw=rest, index=self._index,
            )
        if max_args is not None and len(args) > max_args:
            args = args[:max_args]
        return args

    # ─── Команда 9 (текст) ───────────────────────────────────────────────────

    def _parse_text_command(self, rest: bytes, raw: bytes) -> Command:
        """
        Разобрать команду текста.

        Формат в потоке (после reader'а, который съел первый ВК):
            9 <S>
            ;<O> <текст>

        То есть первая строка — уже в `raw`, а вторая ещё не прочитана.
        Читаем вторую строку отдельно.
        """
        # Первая строка: "9 <S>" — размер шрифта.
        size_text = rest.decode("latin-1", errors="replace").strip()
        size: Optional[int] = None
        if size_text and size_text.isdigit():
            size = int(size_text)
        elif not size_text:
            size = 1  # по умолчанию

        # Читаем вторую строку: ";<O> <текст>"
        raw2 = self._reader.read_raw_command()
        if raw2 is None:
            raise ParseError(
                "Команда 9 оборвана (нет второй строки)",
                raw=raw, index=self._index,
            )

        if not raw2.startswith(b";"):
            raise ParseError(
                f"Вторая строка команды 9 должна начинаться с ';', "
                f"получено {raw2[:8]!r}",
                raw=raw2, index=self._index,
            )

        # Отделяем ориентацию от текста.
        body = raw2[1:]
        # Разделитель — пробел.
        sp_pos = body.find(b" ")
        if sp_pos < 0:
            orient_bytes = body
            text_bytes = b""
        else:
            orient_bytes = body[:sp_pos]
            text_bytes = body[sp_pos + 1:]

        # Ориентация — целое.
        orient_text = orient_bytes.decode("latin-1", errors="replace").strip()
        try:
            orient = int(orient_text) if orient_text else 0
        except ValueError:
            orient = 0
            log.debug(
                "Команда #%d: не удалось разобрать ориентацию %r",
                self._index, orient_text,
            )

        # Декодируем текст с учётом шрифта.
        font = self._detect_font(text_bytes)
        text = decode_text(text_bytes)

        args: list[int] = []
        if size is not None:
            args.append(size)
        args.append(orient)

        return Command(
            op="9",
            args=args,
            text=text,
            font=font,
            raw=raw + b"|" + raw2,
            index=self._index,
        )

    # ─── Определение шрифта ──────────────────────────────────────────────────

    @staticmethod
    def _detect_font(raw: bytes) -> str:
        """
        Определить активный шрифт в сырой строке.
        Если встретился S0 — русский, S1 — латинский.
        По умолчанию — латинский (EN).
        """
        font = "EN"
        for b in raw:
            if b == S0:
                font = "RU"
            elif b == S1:
                font = "EN"
        return font

    # ─── Разное ──────────────────────────────────────────────────────────────

    @staticmethod
    def _decode_op(b: int) -> str:
        """Преобразовать байт операции в строку."""
        if 0x20 <= b < 0x7F:
            return chr(b)
        # некоторые команды в graf.pas используют «непечатаемые» символы
        # (например, ’≠’ = ’≡137’ в KOI-8, но в ASCII это ’≠’)
        special = {
            0x3D: "=",
            0x3E: ">",
            0x5D: "]",
            0x7D: "}",
            0x2B: "+",
            0x2D: "-",
        }
        return special.get(b, f"\\x{b:02x}")

    @staticmethod
    def _looks_like_text(raw: bytes) -> bool:
        """Похоже ли содержимое на текст, а не на команду."""
        # Текст — это, как правило, последовательность букв и пробелов.
        if not raw:
            return False
        printable = sum(1 for b in raw if 0x20 <= b < 0x7F or b in (S0, S1))
        return printable / len(raw) > 0.85

    # ─── Пакетный разбор ─────────────────────────────────────────────────────

    def parse_all(self, *, limit: int = 0) -> list[Command]:
        """Разобрать все команды в списке (осторожно с большими потоками)."""
        result: list[Command] = []
        while True:
            cmd = self.parse()
            if cmd is None:
                break
            result.append(cmd)
            if limit and len(result) >= limit:
                break
        return result

    # ─── Итерация ────────────────────────────────────────────────────────────

    def __iter__(self):
        while True:
            cmd = self.parse()
            if cmd is None:
                return
            yield cmd

    def __repr__(self) -> str:
        return f"CommandParser(index={self._index}, reader={self._reader!r})"


# ─────────────────────────────────────────────────────────────────────────────
# Утилиты
# ─────────────────────────────────────────────────────────────────────────────

def parse_stream(data: bytes, *, limit: int = 0) -> list[Command]:
    """Удобная функция для одноразового разбора потока."""
    reader = CommandStreamReader(data)
    parser = CommandParser(reader)
    return parser.parse_all(limit=limit)


def parse_text_stream(text: str, *, encoding: str = "latin-1") -> list[Command]:
    """
    Разобрать текстовую форму команд (каждая команда — на отдельной строке).

    Полезно для отладки: можно писать команды в человекочитаемом виде.
    """
    data = text.encode(encoding, errors="replace")
    # перевод строки → ВК
    data = data.replace(b"\r\n", bytes([VK]))
    data = data.replace(b"\n", bytes([VK]))
    return parse_stream(data)


# ─────────────────────────────────────────────────────────────────────────────
# Самотест
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")

    # Собираем поток команд вручную.
    def cmd(s: bytes) -> bytes:
        return s + bytes([VK])

    stream = (
        cmd(b"J1") +
        cmd(b"S") +
        cmd(b"] 8 1 552 398") +
        cmd(b"4 280 200") +
        cmd(b"> 1 150") +
        # текстовка: "9 1" ВК ";0 HELLO" ВК
        cmd(b"9 1") +
        cmd(b";0 HELLO") +
        # двоичная команда: b"7" + три 7-битных байта
        cmd(bytes([0x37, 0x01, 0x64, 0x64])) +  # "7", T=1, X=100, Y=100
        cmd(b"2")
    )

    print("=== Разбор ===")
    parser = CommandParser(CommandStreamReader(stream))
    for c in parser:
        print(f"  {c}")

    print("\n=== Разбор текстовой формы ===")
    text = """J1
S
4 100 200
7 1 300 400
9 2
;0 ПРИВЕТ
2"""
    for c in parse_text_stream(text):
        print(f"  {c}")