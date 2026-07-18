$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

Write-Host "Preparing FeedbackCollector build environment..." -ForegroundColor Cyan

python --version
if ($LASTEXITCODE -ne 0) {
    throw "Python was not found on PATH."
}

python -m pip install -r requirements-dev.txt
if ($LASTEXITCODE -ne 0) {
    throw "Dependency installation failed."
}

Write-Host ""
Write-Host "Build environment ready." -ForegroundColor Green
Write-Host "Run .\Build.ps1 to create dist\FeedbackCollector\." -ForegroundColor White
Write-Host "Do not copy .env into the distribution." -ForegroundColor Yellow
