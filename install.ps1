<#
.SYNOPSIS
    Развёртывание эмулятора МС 7401 на Windows.

.DESCRIPTION
    Создаёт виртуальное окружение и ставит в него эмулятор, чтобы не
    трогать системный Python. Ядро работает на чистой стандартной
    библиотеке, внешние зависимости не нужны.

.PARAMETER Venv
    Каталог окружения. По умолчанию .\mc7401-env

.PARAMETER Dev
    Дополнительно установить pytest и ruff.

.EXAMPLE
    .\install.ps1
    .\install.ps1 -Dev
#>
[CmdletBinding()]
param(
    [string]$Venv = "$PSScriptRoot\mc7401-env",
    [switch]$Dev
)

$ErrorActionPreference = 'Stop'

function Say($msg) { Write-Host "==> $msg" -ForegroundColor Green }
function Die($msg) { Write-Host "ОШИБКА: $msg" -ForegroundColor Red; exit 1 }

# ── поиск Python ──────────────────────────────────────────────────────────
$py = $null
foreach ($cand in @('py -3', 'python', 'python3')) {
    try {
        $v = Invoke-Expression "$cand -c `"import sys;print('%d.%d' % sys.version_info[:2])`"" 2>$null
        if ($v -and [version]$v -ge [version]'3.10') { $py = $cand; break }
    } catch { }
}
if (-not $py) { Die "нужен Python 3.10 или новее (https://python.org)" }
Say "Python: $(& $py -V)"

# ── окружение ─────────────────────────────────────────────────────────────
if (-not (Test-Path $Venv)) {
    Say "создаю окружение $Venv"
    Invoke-Expression "$py -m venv `"$Venv`"" | Out-Null
}
$venvPy = Join-Path $Venv 'Scripts\python.exe'
if (-not (Test-Path $venvPy)) { Die "окружение не создалось: $Venv" }

Say "ставлю mc7401-emulator"
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install --upgrade --quiet "$PSScriptRoot"
if ($LASTEXITCODE -ne 0) { Die "установка не удалась" }

if ($Dev) {
    Say "ставлю pytest и ruff"
    & $venvPy -m pip install --upgrade --quiet pytest ruff
}

# ── проверка ──────────────────────────────────────────────────────────────
$exe = Join-Path $Venv 'Scripts\mc7401.exe'
if (Test-Path $exe) {
    Say "проверка: $exe --version -> $(& $exe --version)"
} else {
    Die "скрипт mc7401.exe не найден в $Venv\Scripts"
}

@"

Готово. Запуск (из любого каталога):
  $Venv\Scripts\mc7401.exe поток.bin -o кадр.png
  $Venv\Scripts\mc.exe        поток.bin -o кадр.png

Чтобы не набирать полный путь, добавьте в PATH:
  `$env:PATH += ";$Venv\Scripts"
"@