$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$tools = Join-Path $PSScriptRoot '.tools'
New-Item -ItemType Directory -Force -Path $tools | Out-Null
$uv = Join-Path $tools 'uv.exe'
if (-not (Test-Path $uv)) {
    $zip = Join-Path $tools 'uv-windows.zip'
    Invoke-WebRequest 'https://github.com/astral-sh/uv/releases/download/0.11.25/uv-x86_64-pc-windows-msvc.zip' -OutFile $zip
    Expand-Archive $zip -DestinationPath $tools -Force
}
$env:UV_PROJECT_ENVIRONMENT = Join-Path $PSScriptRoot '.venv-win'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $tools 'python'
& $uv sync --frozen --extra desktop --python 3.12
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
$python = Join-Path $env:UV_PROJECT_ENVIRONMENT 'Scripts\python.exe'
& $python -m giga_dictation download
if ($LASTEXITCODE -ne 0) { throw 'Model download failed' }
& $python -m giga_dictation doctor
if ($LASTEXITCODE -ne 0) { throw 'Environment check failed' }
Write-Host 'Ready. Start run-windows.cmd'
