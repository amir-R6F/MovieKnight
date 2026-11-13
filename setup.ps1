#Requires -Version 5.1
<#
    LANWatch setup - PowerShell 5.1+ equivalent of setup.bat.

    Creates .venv, installs the app + dev dependencies with network-resilient
    pip options (--default-timeout=120 --retries 5), and falls back to mirrors
    if pypi.org is unreachable. Safe to run many times, needs no admin rights
    and changes nothing globally.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$PythonUrl = "https://www.python.org/downloads/windows/"
$VenvDir   = Join-Path $PSScriptRoot ".venv"
$VenvPy    = Join-Path $VenvDir "Scripts\python.exe"
$AppReq    = Join-Path $PSScriptRoot "lanwatch\requirements.txt"
$DevReq    = Join-Path $PSScriptRoot "requirements-dev.txt"

$PipBase = @(
    "--default-timeout=120",
    "--retries", "5",
    "--trusted-host", "pypi.org",
    "--trusted-host", "files.pythonhosted.org"
)

$Mirrors = @(
    [pscustomobject]@{ Name = "Tsinghua"; Url = "https://pypi.tuna.tsinghua.edu.cn/simple"; Host = "pypi.tuna.tsinghua.edu.cn" },
    [pscustomobject]@{ Name = "Runflare"; Url = "https://mirror.runflare.com/pypi/simple";        Host = "mirror.runflare.com" },
    [pscustomobject]@{ Name = "Aliyun";   Url = "https://mirrors.aliyun.com/pypi/simple";         Host = "mirrors.aliyun.com" }
)

function Write-Step {
    param([string]$Text)
    Write-Host ""
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Write-Ok {
    param([string]$Text)
    Write-Host "[OK] $Text" -ForegroundColor Green
}

function Write-WarnLine {
    param([string]$Text)
    Write-Host "[WARN] $Text" -ForegroundColor Yellow
}

function Write-Err {
    param([string]$Text)
    Write-Host "[ERROR] $Text" -ForegroundColor Red
}

function Wait-BeforeExit {
    # Same purpose as "pause" in setup.bat: keep the window open for a double-click user.
    try {
        if ([Environment]::UserInteractive -and $Host.Name -eq "ConsoleHost") {
            Write-Host ""
            [void](Read-Host "Press Enter to close this window")
        }
    }
    catch {
        # No console to read from (redirected stdin); nothing to pause for.
    }
}

function Invoke-Checked {
    <#
        Run a native command, treat a non-zero exit code as a failure and never
        let it abort the whole script. Returns $true on success, $false otherwise.
    #>
    param(
        [Parameter(Mandatory)][string]$Exe,
        [string[]]$Arguments = @(),
        [Parameter(Mandatory)][string]$What
    )
    try {
        $ErrorActionPreference = "Stop"
        & $Exe @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw ("{0} exited with code {1}." -f $What, $LASTEXITCODE)
        }
        return $true
    }
    catch {
        Write-WarnLine ("{0} failed: {1}" -f $What, $_.Exception.Message)
        return $false
    }
}

Write-Host "============================================================"
Write-Host " LANWatch - setup (PowerShell)"
Write-Host "============================================================"
Write-Host " Everything is installed into a local .venv folder."
Write-Host " No admin rights, no PATH changes, safe to run again."
Write-Host "============================================================"

# ---------- 1) Python must exist and be 3.11 or newer ----------
Write-Step "Checking Python"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Err "Python was not found. LANWatch needs Python 3.11 or newer."
    Write-Err "Download it from: $PythonUrl"
    Write-Err 'During installation tick "Add python.exe to PATH".'
    Write-Err 'If "py -3 --version" works but "python" does not, reinstall Python with the PATH option enabled.'
    Wait-BeforeExit
    exit 1
}

& python --version
if ($LASTEXITCODE -ne 0) {
    Write-Err "Running 'python' failed. Reinstall Python 3.11+ from: $PythonUrl"
    Wait-BeforeExit
    exit 1
}

& python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Err "Your Python is older than 3.11. Install Python 3.11 or newer from: $PythonUrl"
    Write-Err "Then run setup.ps1 again."
    Wait-BeforeExit
    exit 1
}

# ---------- 2) Virtual environment ----------
Write-Step "Preparing the virtual environment (.venv)"

if (-not (Test-Path -LiteralPath $VenvPy)) {
    $created = Invoke-Checked -Exe "python" -Arguments @("-m", "venv", $VenvDir) -What "Creating the virtual environment"
    if (-not $created) {
        Write-Err "Could not create .venv in: $PSScriptRoot"
        Write-Err "If a broken .venv folder already exists, delete it and run this script again."
        Write-Err "Also check that you have write permission in that folder."
        Wait-BeforeExit
        exit 1
    }
}

if (-not (Test-Path -LiteralPath $VenvPy)) {
    Write-Err "The folder .venv exists but $VenvPy is missing."
    Write-Err "Delete the .venv folder and run setup.ps1 again."
    Wait-BeforeExit
    exit 1
}

$activate = Join-Path $VenvDir "Scripts\Activate.ps1"
if (Test-Path -LiteralPath $activate) {
    try {
        & $activate
    }
    catch {
        Write-WarnLine ("Activation script could not be loaded: {0}" -f $_.Exception.Message)
    }
}
Write-Ok "Virtual environment ready: $VenvDir"

# ---------- 3) pip ----------
Write-Step "Upgrading pip"
$pipUpgraded = Invoke-Checked -Exe $VenvPy -Arguments (@("-m", "pip", "install", "--upgrade", "pip") + $PipBase) -What "Upgrading pip"
if (-not $pipUpgraded) {
    Write-WarnLine "Continuing with the pip that ships inside .venv."
}

# ---------- 4) Dependencies: pypi.org first, then mirrors ----------
Write-Step "Installing dependencies from pypi.org"
$reqArgs = @("-r", $AppReq, "-r", $DevReq)
$installed = Invoke-Checked -Exe $VenvPy -Arguments (@("-m", "pip", "install") + $PipBase + $reqArgs) -What "Installing from pypi.org"

if (-not $installed) {
    Write-WarnLine "pypi.org did not work; trying fallback mirrors. This can take a while on a slow network."
    foreach ($mirror in $Mirrors) {
        Write-Step ("Trying mirror: {0} ({1})" -f $mirror.Name, $mirror.Url)
        $mirrorArgs = @("-m", "pip", "install") + $PipBase + @("-i", $mirror.Url, "--trusted-host", $mirror.Host) + $reqArgs
        $installed = Invoke-Checked -Exe $VenvPy -Arguments $mirrorArgs -What ("Installing from {0}" -f $mirror.Name)
        if ($installed) {
            Write-Ok ("Installed from {0}." -f $mirror.Name)
            break
        }
        Write-WarnLine ("Mirror {0} did not work; trying the next option." -f $mirror.Name)
    }
}

if (-not $installed) {
    Write-Err "Installing dependencies failed on pypi.org and on every mirror."
    Write-Host "         What to try next:"
    Write-Host "           - Check your internet connection and disable VPN for a moment."
    Write-Host "           - Run setup.ps1 again; it is safe to repeat and resumes progress."
    Write-Host '           - If pip keeps timing out, create %APPDATA%\pip\pip.ini as described in README.md.'
    Write-Host "           - Last resort, install by hand to see the full pip error:"
    Write-Host ("             `"{0}`" -m pip install -r `"{1}`"" -f $VenvPy, $AppReq)
    Wait-BeforeExit
    exit 1
}

Write-Ok "Dependencies installed."

# ---------- 5) Verify the imports the app needs ----------
Write-Step "Verifying the installation"
$missing = @()
foreach ($module in @("websockets", "zeroconf", "customtkinter")) {
    & $VenvPy -c "import $module" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Err ("Python module '{0}' could not be imported." -f $module)
        $missing += $module
    }
    else {
        Write-Ok ("{0} imports correctly." -f $module)
    }
}

# python-mpv-jsonipc installs the module "python_mpv_jsonipc" (that is what
# player.py imports). Some builds also expose "mpv", so accept either one.
& $VenvPy -c "import python_mpv_jsonipc" 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Ok "python-mpv-jsonipc imports correctly."
}
else {
    & $VenvPy -c "import mpv" 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "python-mpv-jsonipc imports correctly."
    }
    else {
        Write-Err 'The mpv binding could not be imported (tried "python_mpv_jsonipc" and "mpv").'
        $missing += "python-mpv-jsonipc"
    }
}

if ($missing.Count -gt 0) {
    Write-Err ("Some modules are missing after installation: {0}" -f ($missing -join ", "))
    Write-Host "         Run setup.ps1 again (safe to repeat). If the same module keeps failing,"
    Write-Host "         install the packages by hand to see the full pip error:"
    Write-Host ("           `"{0}`" -m pip install websockets zeroconf customtkinter python-mpv-jsonipc" -f $VenvPy)
    Wait-BeforeExit
    exit 1
}

Write-Host ""
Write-Ok "Setup complete. Double-click run.bat to start LANWatch."
Wait-BeforeExit
exit 0
