# mc7401.fish — короткие команды для fish.
#
# Подключение:
#   source /путь/к/mc7401.fish
# или положить в ~/.config/fish/conf.d/mc7401.fish
#
# Команды:
#   mc  <файл> …        — отрисовать поток (короткая форма mc7401)
#   мск <файл> …        — то же на русском
#   мсг <сценарий> …    — сгенерировать эталонный кадр
#   мсд <файл>          — показать кадр текстом (ASCII-арт)
#   мсдк [хост:порты]   — подключиться к БЭСМ-6 по ДКС
#   мсв [хост:порт]     — слушать порт ДКС (обратный режим)

set -gx MC7401_ROOT (cd (dirname (status --current-filename))/..; and pwd)

function mc --description 'отрисовать поток команд МС 7401'
    cd $MC7401_ROOT; and python3 -m mc7401 $argv
end

function мск --description 'отрисовать поток команд МС 7401'
    mc $argv
end

function мсг --description 'сгенерировать эталонный кадр'
    cd $MC7401_ROOT; and python3 -u tools/gen_golden.py $argv
end

function мсд --description 'показать кадр текстом (ASCII-арт)'
    cd $MC7401_ROOT; and python3 -m mc7401 --dump-ascii $argv
end

function мсдк --description 'связь с БЭСМ-6 по ДКС (клиент)'
    set -l host 127.0.0.1:4202
    if test (count $argv) -gt 0; set host $argv[1]; end
    cd $MC7401_ROOT; and python3 -m mc7401 --link $host $argv[2..-1]
end

function мсв --description 'связь с БЭСМ-6 по ДКС (слушать)'
    set -l host 127.0.0.1:4202
    if test (count $argv) -gt 0; set host $argv[1]; end
    cd $MC7401_ROOT; and python3 -m mc7401 --listen $host $argv[2..-1]
end

# ── автодополнение ────────────────────────────────────────────────────────

# Сценарии берём из генератора, чтобы список не расходился с кодом.
function __mc7401_scenarios
    cd $MC7401_ROOT; or return
    python3 -c 'import re,pathlib,ast; s=pathlib.Path("tools/gen_golden.py").read_text(encoding="utf-8"); m=re.search(r"SCENARIOS\s*=\s*(\[.*?\])", s, re.S); print(" ".join(ast.literal_eval(m.group(1))) if m else "")' 2>/dev/null
end

function __mc7401_needs_arg
    set -l tokens (commandline -opc)
    test (count $tokens) -eq 1
end

function __mc7401_using_backend
    contains -- $argv[2] -b --backend
end

function __mc7401_using_link
    contains -- $argv[2] -b --backend --link --listen
end

# аргументы после `--backend`
complete -c mc -n '__mc7401_needs_arg' -s b -l backend \
    -a 'png svg tk pygame' -d 'способ вывода'
complete -c mc -n '__mc7401_needs_arg' -s o -l output -F -d 'выходной файл'
complete -c mc -n '__mc7401_needs_arg' -l png -F -d 'дополнительно PNG'
complete -c mc -n '__mc7401_needs_arg' -s b -l scale -x -d 'масштаб'
complete -c mc -n '__mc7401_needs_arg' -l link -x -a '127.0.0.1:4202' \
    -d 'связь с БЭСМ-6 по ДКС'
complete -c mc -n '__mc7401_needs_arg' -l listen -x -a '127.0.0.1:4202' \
    -d 'слушать порт ДКС'
complete -c mc -n '__mc7401_needs_arg' -l peak -d 'самый наполненный кадр'
complete -c mc -n '__mc7401_needs_arg' -l stats -d 'статистика разбора'
complete -c mc -n '__mc7401_needs_arg' -l dump-ascii -d 'кадр текстом'
complete -c mc -n '__mc7401_needs_arg' -l list-backends -d 'список backend-ов'
complete -c mc -n '__mc7401_needs_arg' -l help -d 'справка'
complete -c mc -n '__mc7401_needs_arg' -l version -d 'версия'
complete -c mc -n '__mc7401_needs_arg' -F -d 'поток.bin'

for c in мск
    complete -c $c -w mc
end
complete -c мсд -w mc -a '(--dump-ascii)'

# сценарии генератора
complete -c мсг -n '__mc7401_needs_arg' -a '(__mc7401_scenarios)' \
    -d 'сценарий эталонного кадра'
complete -c мсг -n '__mc7401_needs_arg' -l list -d 'список сценариев'
complete -c мсг -n '__mc7401_needs_arg' -s n -l names -d 'выбрать сценарии'
complete -c мсг -n '__mc7401_needs_arg' -F -d 'файл потока'

complete -c мсд -n '__mc7401_needs_arg' -F -a '*.bin' -d 'поток.bin'
complete -c мсдк -n '__mc7401_needs_arg' -x -a '127.0.0.1:4202 127.0.0.1:4202-4223' \
    -d 'линия ДКС'
complete -c мсв -n '__mc7401_needs_arg' -x -a '127.0.0.1:4202' -d 'порт ДКС'