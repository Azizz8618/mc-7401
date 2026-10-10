# shellcheck shell=bash
# mc7401-completion.bash — короткие команды для bash.
#
# Подключение:
#   source /путь/к/mc7401-completion.bash
# или добавить в ~/.bashrc:
#   source /путь/к/mc7401-completion.bash
#
# Команды:
#   mc  <файл> …        — отрисовать поток (короткая форма mc7401)
#   мск <файл> …        — то же на русском
#   мсг <сценарий> …    — сгенерировать эталонный кадр (tools/gen_golden.py)
#   мсдк [хост:порты]   — подключиться к БЭСМ-6 по ДКС
#   мсв [хост:порт]     — слушать порт ДКС (обратный режим)
#   мсд <файл>          — показать кадр текстом (ASCII-арт)

_mc7401_root() {
    local d="${BASH_SOURCE[0]}"
    while [ -L "$d" ]; do
        d="$(readlink "$d")"
    done
    # Поднимаемся вверх до каталога с pyproject.toml — это корень
    # проекта (сам файл лежит в tools/).
    local dir
    dir="$(cd -P "$(dirname "$d")" >/dev/null 2>&1 && pwd)"
    while [ -n "$dir" ] && [ "$dir" != "/" ] && [ ! -f "$dir/pyproject.toml" ]; do
        dir="$(dirname "$dir")"
    done
    [ -f "$dir/pyproject.toml" ] && printf '%s\n' "$dir"
}

_mc7401_run() {
    local root
    root="$(_mc7401_root)" || return 1
    ( cd "$root" && python3 -m mc7401 "$@" )
}

_mc7401_golden() {
    local root
    root="$(_mc7401_root)" || return 1
    ( cd "$root" && python3 -u tools/gen_golden.py "$@" )
}

# Сценарии, известные генератору эталонных кадров.
_mc7401_scenarios() {
    ( cd "$(_mc7401_root)" && \
      python3 -c 'import re,pathlib,ast; s=pathlib.Path("tools/gen_golden.py").read_text(encoding="utf-8"); m=re.search(r"SCENARIOS\s*=\s*(\[.*?\])", s, re.S); print(" ".join(ast.literal_eval(m.group(1))) if m else "")' ) 2>/dev/null
}

# ── сами функции ───────────────────────────────────────────────────────────
mc()  { _mc7401_run "$@"; }                     # коротко: отрисовать
мск() { _mc7401_run "$@"; }                     # то же кириллицей
мсг() { _mc7401_golden "$@"; }                  # эталонные кадры
мсд() { _mc7401_run "$@" --dump-ascii; }       # кадр текстом
мсдк() {                                     # связь с БЭСМ-6: клиент ДКС
    _mc7401_run --link "${1:-127.0.0.1:4202}" "${@:2}"
}
мсв() {                                     # связь с БЭСМ-6: слушать
    _mc7401_run --listen "${1:-127.0.0.1:4202}" "${@:2}"
}

# ── автодополнение ─────────────────────────────────────────────────────────
_mc7401_complete() {
    local cur prev
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"

    case "$prev" in
        -b|--backend)
            COMPREPLY=( $(compgen -W "png svg tk pygame" -- "$cur") ); return ;;
        -o|--output|--png)
            COMPREPLY=( $(compgen -f -X '!*.png' -X '!*.svg' -- "$cur") ); return ;;
        --link|--listen)
            COMPREPLY=( $(compgen -W "127.0.0.1:4202 127.0.0.1:4202-4223" -- "$cur") )
            return ;;
    esac

    if [[ "$cur" == -* ]]; then
        COMPREPLY=( $(compgen -W \
            "--backend --output --png --scale --peak --stats --dump-ascii \
             --list-backends --link --listen --verbose --version --help" \
            -- "$cur") )
        return
    fi

    case "${COMP_WORDS[0]}" in
        мсг)
            COMPREPLY=( $(compgen -W "$(_mc7401_scenarios)" -- "$cur") ); return ;;
        мсд)
            local root; root="$(_mc7401_root)"
            COMPREPLY=( $(compgen -f -X '!*.bin' -- "$cur") ); return ;;
        *)
            COMPREPLY=( $(compgen -f -X '!*.bin' -- "$cur") ); return ;;
    esac
}

complete -F _mc7401_complete mc мск мсг мсд мсдк мсв

# Сообщение при загрузке — только при интерактивной сессии.
case $- in
    *i*) printf '\033[32m==>\033[0m mc7401: команды mc, мск, мсг, мсд, мсдк, мсв доступны\n' ;;
esac