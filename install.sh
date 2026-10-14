#!/usr/bin/env bash
# install.sh — развёртывание эмулятора МС 7401 на Linux/macOS.
#
#   ./install.sh              # пользовательская установка (по умолчанию)
#   ./install.sh --user       # то же, ~/.local
#   ./install.sh --venv .venv # в виртуальное окружение
#   ./install.sh --system     # системно (нужен sudo)
#   ./install.sh --dev        # редактируемая установка + тесты
#   ./install.sh --uninstall  # удалить
#
# Ядро работает на чистой стандартной библиотеке Python 3.10+.
# Необязательные компоненты: pygame (просмотр анимации), tkinter
# (интерактивный просмотр) — устанавливаются по --gui.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="$HOME/.local"
MODE="user"
GUI=0
DEV=0
UNINSTALL=0

while [ $# -gt 0 ]; do
    case "$1" in
        --user)     MODE="user"; shift ;;
        --system)   MODE="system"; PREFIX="/usr/local"; shift ;;
        --venv)     MODE="venv"
                    # путь можно указать сразу: --venv /путь
                    if [ $# -ge 2 ] && [ "${2#-}" = "$2" ]; then
                        PREFIX="$2"; shift
                    else
                        PREFIX="$HERE/.venv"
                    fi
                    shift ;;
        --prefix)   MODE="prefix"; PREFIX="$2"; shift 2 ;;
        --dev)      DEV=1; shift ;;
        --gui)      GUI=1; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help)  sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "неизвестный ключ: $1" >&2; exit 2 ;;
    esac
done

say() { printf '\033[32m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31mОШИБКА:\033[0m %s\n' "$*" >&2; exit 1; }

# ── проверка Python ────────────────────────────────────────────────────────
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || die "не найден $PY; нужен Python 3.10+"
"$PY" - <<'EOF' || die "нужен Python 3.10 или новее"
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
EOF
say "Python: $("$PY" -V 2>&1)"

# ── удаление ───────────────────────────────────────────────────────────────
if [ "$UNINSTALL" = 1 ]; then
    if [ "$MODE" = venv ]; then
        say "удаляю виртуальное окружение $PREFIX"
        rm -rf "$PREFIX"
    else
        say "удаляю $PREFIX/bin/{mc,mc7401} и пакет"
        rm -f "$PREFIX/bin/mc" "$PREFIX/bin/mc7401"
        "$PY" -m pip uninstall -y mc7401-emulator >/dev/null 2>&1 || true
    fi
    say "готово"
    exit 0
fi

# ── установка ──────────────────────────────────────────────────────────────
EXTRAS=""
[ "$GUI" = 1 ] && EXTRAS="[gui]"

case "$MODE" in
    venv)
        [ -d "$PREFIX" ] || "$PY" -m venv "$PREFIX"
        PIP="$PREFIX/bin/pip"
        [ "$DEV" = 1 ] && "$PIP" install -e "$HERE$EXTRAS" \
                      || "$PIP" install --upgrade "$HERE$EXTRAS"
        BIN="$PREFIX/bin"
        say "установлено в $PREFIX"
        ;;
    system)
        "$PY" -m pip install --upgrade $EXTRAS "$HERE" || die "нужен sudo"
        BIN="$PREFIX/bin"
        say "установлено в $PREFIX"
        ;;
    prefix)
        "$PY" -m pip install --upgrade --prefix "$PREFIX" "$HERE" || die "нужен sudo"
        BIN="$PREFIX/bin"
        ;;
    *)
        "$PY" -m pip install --user --upgrade $EXTRAS "$HERE" \
            || die "установка не удалась (попробуйте --venv)"
        BIN="$PREFIX/bin"
        ;;
esac

if [ "$DEV" = 1 ]; then
    say "ставлю dev-зависимости"
    "$PY" -m pip install --user --upgrade pytest ruff 2>/dev/null || true
fi

# ── предупреждение о запуске из другого дерева ─────────────────────────────
# `pip install` **копирует** исходники в site-packages, а не ссылается на
# рабочее дерево.  Поэтому после правки в дереве запускалка `mc7401`
# продолжает работать старой копией — и правки «не работают», хотя в
# дереве всё верно.  Ловушка стоила двух сообщений о неработающих
# PgUp/PgDn, поэтому предупреждаем сразу.
say "проверяю, откуда запустится mc7401"
SAW=$(cd / && "$PY" -c "
import mc7401, sys
sys.stderr.write(mc7401.__file__)
" 2>&1 >/dev/null)
case "$SAW" in
    "$HERE"/*)
        say "  запускается из рабочего дерева — всё в порядке" ;;
    *)
        say "  ВНИМАНИЕ: из любого каталога mc7401 запускается из"
        say "            $SAW"
        say "            это копия в site-packages, а не дерево $HERE."
        say "            После правок в дереве её надо переставить:"
        say "              ./install.sh --$MODE"
        say "            либо запускать прямо из дерева:"
        say "              cd $HERE && python3 -m mc7401 --gui" ;;
esac

# ── проверка ───────────────────────────────────────────────────────────────
# Короткая команда `mc7401` — консольный скрипт pip; если он не создался
# (например, без поддержки скриптов), ставим однофайловый лаунчер.
if [ ! -x "$BIN/mc7401" ] && [ -f "$HERE/tools/mc7401" ]; then
    mkdir -p "$BIN"
    cp "$HERE/tools/mc7401" "$BIN/mc7401"
    chmod +x "$BIN/mc7401"
    say "поставлен лаунчер $BIN/mc7401"
fi

if [ -x "$BIN/mc7401" ]; then
    PATH="$BIN:$PATH"
    say "проверка: mc7401 --version → $("$BIN/mc7401" --version)"
else
    say "скрипты в $BIN (добавьте в PATH)"
fi

cat <<EOF

Готово. Команды:
  mc7401                            интерактивный режим (диалог с терминалом)
  mc7401 <поток.bin> -o кадр.png    отрисовать поток команд
  mc  <поток.bin> -o кадр.png       то же, короткая форма
  mc7401 --listen 127.0.0.1:4202    ждать БЭСМ-6 по ДКС
  mc7401 --link 127.0.0.1:4202      подключиться к ДКС

Без установки: один исполняемый файл tools/mc7401 (запускает пакет
из репозитория).

Добавьте в PATH (если нужно):
  export PATH="$BIN:\$PATH"
EOF