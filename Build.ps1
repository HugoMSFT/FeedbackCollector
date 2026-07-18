$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

Write-Host "Building FeedbackCollector..." -ForegroundColor Cyan

python --version
if ($LASTEXITCODE -ne 0) {
    throw "Python was not found on PATH."
}

python -m pip install -r requirements-dev.txt
if ($LASTEXITCODE -ne 0) {
    throw "Dependency installation failed."
}

python build_package.py
if ($LASTEXITCODE -ne 0) {
    throw "Build failed."
}

Write-Host "Build complete: $ProjectRoot\dist\FeedbackCollector\" -ForegroundColor Green
Write-Host "The distribution intentionally excludes .env." -ForegroundColor Yellow
