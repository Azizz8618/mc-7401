"""
undo.py — отмена последнего действия в автономном режиме.

В режиме **АВТОНОМНО** терминал работает без ЭВМ, и все действия
оператора остаются локальными: штрих пера, стирание, набор знака,
очистка экрана.  Ошибиться легко — стёр не то или провёл линию мимо, —
и на живой машине ничего отменять нельзя: там действие уже ушло по
линии.  Поэтому в автономном режиме у терминала есть команда отмены
последнего действия (`undo.push` / `undo.pop`).

Действия хранятся не командами, а **кадром до действия**: отменяется не
«штрих пера», а состояние экрана, каким оно было.  Это даёт отмену
любого действия одинаково — и пером, и с клавиатуры, — и не требует
обратных операций, которых у стирания и набора просто нет.

Глубина ограничена (`LIMIT`): кадр 560×400 — это 28 килобайт, и
неограниченная история съела бы память терминала.

    >>> from mc7401.framebuffer import Framebuffer
    >>> fb = Framebuffer()
    >>> u = Undo()
    >>> u.push(fb, 'штрих')
    >>> fb.point(10, 10)
    >>> restored, what = u.pop()
    >>> what
    'штрих'
    >>> restored.get(10, 10)          # кадр до действия
    0
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .framebuffer import Framebuffer

#: Сколько действий помнится.  Кадр 560×400 занимает 28 килобайт;
#: двадцать шагов — это 560 килобайт, для терминала ничто, а
#: возвращаться дальше оператору и не нужно.
LIMIT = 20


@dataclass
class Undo:
    """История действий для отмены: `(кадр до действия, что сделали)`."""

    #: Новые — сверху; `pop` снимает последнее.
    steps: list[tuple[Framebuffer, str]] = field(default_factory=list)
    #: Сколько действий отменено подряд — для сообщения в подвале.
    undone: int = 0
    limit: int = LIMIT

    def push(self, fb: Framebuffer, what: str = '') -> None:
        """Запомнить кадр **до** действия `what`.

        Копия снимается здесь же: вызывающая сторона правит кадр после
        вызова, и держать ссылку на живой кадр нельзя.
        """
        if fb is None:
            return
        self.steps.append((fb.copy(), what))
        while len(self.steps) > self.limit:
            self.steps.pop(0)
        self.undone = 0

    def pop(self) -> Optional[tuple[Framebuffer, str]]:
        """Вернуть кадр перед последним действием и его название.

        `None` — отменять нечего: история пуста.  Возвращённый кадр
        вызывающая сторона кладёт на место сама.
        """
        if not self.steps:
            return None
        fb, what = self.steps.pop()
        self.undone += 1
        return fb, what

    def __bool__(self) -> bool:
        return bool(self.steps)

    def __len__(self) -> int:
        return len(self.steps)


__all__ = ['Undo', 'LIMIT']
