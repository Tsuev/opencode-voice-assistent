# Jarvis one-command installer for Windows (PowerShell).
# Equivalent to install.sh; use this if Git Bash is not available.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "uv не найден — устанавливаю."
    irm https://astral.sh/uv/install.ps1 | iex
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv установлен, но не найден в PATH. Откройте новый терминал и повторите .\install.ps1."
}

Write-Host "Python 3.12 и зависимости Jarvis..."
uv python install 3.12
uv sync

Write-Host "Установка моделей и голоса..."
$cliArgs = $args
& uv run python scripts/install.py $cliArgs
