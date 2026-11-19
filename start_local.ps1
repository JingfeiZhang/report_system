$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) {
    throw 'Missing .venv. Create it and install requirements.local.lock.txt first.'
}
$env:PYTHONUTF8 = '1'
Write-Host 'Open http://127.0.0.1:5000/ and start the system from the configuration panel.'
& $projectPython -X utf8 app.py
exit $LASTEXITCODE
