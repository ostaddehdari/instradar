param(
    [switch]$SkipChrome,
    [switch]$SkipNode,
    [switch]$SkipPlaywright
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host ""
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host " Instradar - Windows Dependency Installer" -ForegroundColor Cyan
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host ""

function Command-Exists {
    param([string]$Command)

    return [bool](Get-Command $Command -ErrorAction SilentlyContinue)
}

function Install-WithWinget {
    param(
        [string]$Id,
        [string]$Name
    )

    if (-not (Command-Exists "winget")) {
        Write-Warning "winget is not available. Install $Name manually."
        return $false
    }

    Write-Host "Installing $Name..." -ForegroundColor Yellow

    winget install `
        --id $Id `
        -e `
        --accept-package-agreements `
        --accept-source-agreements

    return $true
}

# ------------------------------------------------------
# Python
# ------------------------------------------------------

Write-Host "[1/8] Checking Python..." -ForegroundColor Green

$PythonCommand = $null
$PythonArguments = @()

if (Command-Exists "py") {
    try {
        & py -3.12 --version *> $null
        if ($LASTEXITCODE -eq 0) {
            $PythonCommand = "py"
            $PythonArguments = @("-3.12")
        }
    }
    catch {
    }
}

if (-not $PythonCommand -and (Command-Exists "python")) {
    try {
        $VersionOutput = & python --version 2>&1

        if ($VersionOutput -match "Python 3\.") {
            $PythonCommand = "python"
            $PythonArguments = @()
        }
    }
    catch {
    }
}

if (-not $PythonCommand) {
    Write-Host "Python 3 was not found." -ForegroundColor Yellow

    if (-not (Install-WithWinget `
        -Id "Python.Python.3.12" `
        -Name "Python 3.12")) {

        throw "Python installation failed. Install Python 3.12 and run this installer again."
    }

    # refresh common Python launcher path
    $env:PATH = "$env:LOCALAPPDATA\Programs\Python\Python312;$env:LOCALAPPDATA\Programs\Python\Python312\Scripts;$env:PATH"

    if (Command-Exists "py") {
        $PythonCommand = "py"
        $PythonArguments = @("-3.12")
    }
    elseif (Command-Exists "python") {
        $PythonCommand = "python"
        $PythonArguments = @()
    }
    else {
        throw "Python was installed but is not yet visible in PATH. Reopen PowerShell and run install_windows.bat."
    }
}

Write-Host "Python:" -NoNewline
& $PythonCommand @PythonArguments --version

# ------------------------------------------------------
# Virtual environment
# ------------------------------------------------------

Write-Host ""
Write-Host "[2/8] Creating virtual environment..." -ForegroundColor Green

$Venv = Join-Path $PSScriptRoot ".venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"
$VenvPip = Join-Path $Venv "Scripts\pip.exe"

if (-not (Test-Path $VenvPython)) {
    & $PythonCommand @PythonArguments -m venv $Venv
}

if (-not (Test-Path $VenvPython)) {
    throw "Virtual environment creation failed."
}

Write-Host ""
Write-Host "[3/8] Updating pip / setuptools / wheel..." -ForegroundColor Green

& $VenvPython -m pip install --upgrade pip setuptools wheel

# ------------------------------------------------------
# Python requirements
# ------------------------------------------------------

Write-Host ""
Write-Host "[4/8] Installing Python dependencies..." -ForegroundColor Green

$RequirementCandidates = @(
    "requirements-windows.txt",
    "requirements.txt",
    "requirements\base.txt",
    "requirements\production.txt",
    "requirements\prod.txt",
    "requirements-freeze.txt"
)

$RequirementFile = $null

foreach ($Candidate in $RequirementCandidates) {
    $Full = Join-Path $PSScriptRoot $Candidate

    if (Test-Path $Full) {
        $RequirementFile = $Full
        break
    }
}

if ($RequirementFile) {

    Write-Host "Using: $RequirementFile"

    # Remove packages known to be Unix-only when they are direct entries.
    $InstallDir = Join-Path $PSScriptRoot ".installer"
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

    $GeneratedRequirements = Join-Path $InstallDir "requirements-windows.generated.txt"

    $Lines = Get-Content $RequirementFile

    $FilteredLines = foreach ($Line in $Lines) {

        $Trimmed = $Line.Trim()

        if (
            $Trimmed -match "^(uvloop|systemd-python|python-systemd)(\[.*?\])?([=<>!~].*)?$"
        ) {
            Write-Host "Skipping Unix-only package: $Trimmed" -ForegroundColor DarkYellow
            continue
        }

        $Line
    }

    $FilteredLines | Set-Content `
        -Path $GeneratedRequirements `
        -Encoding UTF8

    & $VenvPython -m pip install -r $GeneratedRequirements

}
elseif (Test-Path (Join-Path $PSScriptRoot "pyproject.toml")) {

    Write-Host "requirements.txt not found; using pyproject.toml."
    & $VenvPython -m pip install -e .

}
else {
    Write-Warning "No Python requirements file was found."
}

# ------------------------------------------------------
# Node dependencies
# ------------------------------------------------------

Write-Host ""
Write-Host "[5/8] Checking Node.js dependencies..." -ForegroundColor Green

$PackageJson = Join-Path $PSScriptRoot "package.json"

if ((Test-Path $PackageJson) -and (-not $SkipNode)) {

    if (-not (Command-Exists "node")) {

        Install-WithWinget `
            -Id "OpenJS.NodeJS.LTS" `
            -Name "Node.js LTS" | Out-Null

        $env:PATH = "$env:ProgramFiles\nodejs;$env:PATH"
    }

    if (Command-Exists "npm") {

        if (Test-Path (Join-Path $PSScriptRoot "package-lock.json")) {
            Write-Host "Running npm ci..."
            & npm ci
        }
        else {
            Write-Host "Running npm install..."
            & npm install
        }
    }
    else {
        Write-Warning "npm not found after Node.js installation."
    }
}
else {
    Write-Host "No package.json or Node installation skipped."
}

# ------------------------------------------------------
# Google Chrome
# ------------------------------------------------------

Write-Host ""
Write-Host "[6/8] Checking Google Chrome..." -ForegroundColor Green

if (-not $SkipChrome) {

    $ChromeCandidates = @(
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
    )

    $ChromeFound = $false

    foreach ($Chrome in $ChromeCandidates) {
        if ($Chrome -and (Test-Path $Chrome)) {
            $ChromeFound = $true
            Write-Host "Chrome found: $Chrome"
            break
        }
    }

    if (-not $ChromeFound) {
        Install-WithWinget `
            -Id "Google.Chrome" `
            -Name "Google Chrome" | Out-Null
    }
}

# ------------------------------------------------------
# Playwright support if project uses it
# ------------------------------------------------------

Write-Host ""
Write-Host "[7/8] Browser dependency check..." -ForegroundColor Green

if (-not $SkipPlaywright) {

    $UsesPlaywright = $false

    if ($RequirementFile) {
        if (Select-String `
            -Path $RequirementFile `
            -Pattern "playwright" `
            -Quiet `
            -ErrorAction SilentlyContinue) {

            $UsesPlaywright = $true
        }
    }

    if ($UsesPlaywright) {
        Write-Host "Playwright detected; installing Chromium..."
        & $VenvPython -m playwright install chromium
    }
}

# ------------------------------------------------------
# pip integrity check
# ------------------------------------------------------

Write-Host ""
Write-Host "[8/8] Checking installed Python packages..." -ForegroundColor Green

& $VenvPython -m pip check

Write-Host ""
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host " Installation completed" -ForegroundColor Green
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Virtual environment:"
Write-Host "  $Venv"
Write-Host ""
Write-Host "Activate manually with:"
Write-Host "  .\.venv\Scripts\Activate.ps1" -ForegroundColor Yellow
Write-Host ""

if (Test-Path (Join-Path $PSScriptRoot "manage.py")) {

    Write-Host "Django project detected." -ForegroundColor Cyan
    Write-Host ""
    Write-Host "After configuring .env / database, run:"
    Write-Host "  .\.venv\Scripts\python.exe manage.py migrate"
    Write-Host "  .\.venv\Scripts\python.exe manage.py collectstatic --noinput"
    Write-Host ""
}

Write-Host "NOTE:"
Write-Host "PostgreSQL / Redis credentials and production .env are intentionally"
Write-Host "not included in the source archive."
Write-Host ""
