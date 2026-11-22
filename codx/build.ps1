<#
  codx - build / install script (Windows 11, PowerShell 5.1+)

  Usage:
      powershell -ExecutionPolicy Bypass -File .\build.ps1
      powershell -ExecutionPolicy Bypass -File .\build.ps1 -Autostart
      powershell -ExecutionPolicy Bypass -File .\build.ps1 -InstallTor

  What it does:
    1. Locates a Python 3.10+ interpreter.
    2. Byte-compiles every module (a real compile gate, not a promise).
    3. Runs the self-test: config, warp-cli, tor, bridge port.
    4. Creates a "codx" shortcut on the Desktop.
    5. -InstallTor downloads the Tor Expert Bundle into %LOCALAPPDATA%\codx\tor.
    6. -Autostart registers a logon scheduled task running --headless.
#>
[CmdletBinding()]
param(
    [switch]$Autostart,
    [switch]$InstallTor,
    [switch]$NoShortcut
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Write-Step($text) { Write-Host "==> $text" -ForegroundColor Cyan }
function Write-Ok($text)   { Write-Host "    $text" -ForegroundColor Green }
function Write-Warn2($text) { Write-Host "    $text" -ForegroundColor Yellow }

# ---------------------------------------------------------------- 1. python
Write-Step 'Locating Python 3.10+'
$python = $null
$pythonExe = $null
$pythonPre = @()

foreach ($candidate in @('py -3.14', 'py -3.13', 'py -3.12', 'py -3.11', 'py -3.10', 'python')) {
    $parts = $candidate.Split(' ')
    $exe = $parts[0]
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    $pre = @()
    if ($parts.Length -gt 1) { $pre = $parts[1..($parts.Length - 1)] }
    try {
        $verText = & $exe @($pre + @('-c', "import sys;print('%d.%d'%sys.version_info[:2])")) 2>$null
        if (-not $verText) { continue }
        $bits = $verText.Trim().Split('.')
        if ([int]$bits[0] -ge 3 -and [int]$bits[1] -ge 10) {
            $python = $candidate
            $pythonExe = (Get-Command $exe).Source
            $pythonPre = $pre
            Write-Ok "$candidate (Python $verText)"
            break
        }
    } catch { continue }
}
if (-not $python) {
    throw 'No Python 3.10+ found. Install it with: winget install -e --id Python.Python.3.12'
}

function Invoke-Py {
    param([string[]]$PyArgs)
    & $pythonExe @($pythonPre + $PyArgs)
    if ($LASTEXITCODE -ne 0) { throw "python exited with $LASTEXITCODE" }
}

# ---------------------------------------------------------------- 2. compile
Write-Step 'Byte-compiling sources'
Invoke-Py @('-m', 'compileall', '-q', 'codx')
Write-Ok 'all modules compile'

# ---------------------------------------------------------------- 3. selftest
Write-Step 'Running self-test'
# python writes log lines to stderr. Under $ErrorActionPreference='Stop' PowerShell
# promotes a native command's stderr to a terminating error, so the merge is done
# with the preference relaxed and restored immediately afterwards. The build result
# is the process exit code, not the console stream.
$selftestLog = Join-Path $env:TEMP ("codx-selftest-{0}.log" -f $PID)
$selftestArgs = $pythonPre + @('-m', 'codx', '--selftest')
$selftestProc = Start-Process -FilePath $pythonExe -ArgumentList $selftestArgs `
    -WorkingDirectory $root -NoNewWindow -Wait -PassThru `
    -RedirectStandardOutput $selftestLog -RedirectStandardError "$selftestLog.err"
$selftestExit = $selftestProc.ExitCode
foreach ($f in @($selftestLog, "$selftestLog.err")) {
    if (Test-Path $f) {
        Get-Content $f | ForEach-Object { Write-Host "    $_" }
        Remove-Item $f -Force -ErrorAction SilentlyContinue
    }
}
if ($selftestExit -eq 0) {
    Write-Ok 'self-test passed'
} else {
    Write-Warn2 "self-test reported warnings (exit $selftestExit) - see lines above"
}

# ---------------------------------------------------------------- 4. tor
if ($InstallTor) {
    Write-Step 'Installing Tor Expert Bundle'
    $torDir = Join-Path $env:LOCALAPPDATA 'codx\tor'
    New-Item -ItemType Directory -Force -Path $torDir | Out-Null
    if (Test-Path (Join-Path $torDir 'tor.exe')) {
        Write-Ok "tor.exe already present at $torDir"
    } else {
        $index = 'https://dist.torproject.org/torbrowser/'
        Write-Host "    reading $index"
        $html = (Invoke-WebRequest -Uri $index -UseBasicParsing).Content
        $versions = [regex]::Matches($html, 'href="(\d+\.\d+\.\d+)/"') |
                    ForEach-Object { $_.Groups[1].Value } |
                    Sort-Object { [version]$_ } -Descending
        $ver = $null
        $url = $null
        foreach ($candidate in $versions) {
            $probe = "$index$candidate/tor-expert-bundle-windows-x86_64-$candidate.tar.gz"
            try {
                $head = Invoke-WebRequest -Uri $probe -Method Head -UseBasicParsing -ErrorAction Stop
                if ($head.StatusCode -eq 200) { $ver = $candidate; $url = $probe; break }
            } catch { continue }
        }
        if (-not $url) { throw 'no Windows expert bundle found in the Tor distribution index' }
        $tmp = Join-Path $env:TEMP "tor-expert-bundle-windows-x86_64-$ver.tar.gz"
        Write-Host "    downloading $url"
        Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing
        Write-Host '    extracting'
        tar -xzf $tmp -C $torDir
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
        if (-not (Test-Path (Join-Path $torDir 'tor.exe'))) {
            $nested = Get-ChildItem -Path $torDir -Recurse -Filter 'tor.exe' -ErrorAction SilentlyContinue |
                      Select-Object -First 1
            if ($nested) {
                $nestedDir = $nested.DirectoryName
                Write-Host "    flattening $nestedDir"
                Get-ChildItem -Path $nestedDir -Force | ForEach-Object {
                    $target = Join-Path $torDir $_.Name
                    if ($target -ne $_.FullName) { Move-Item -Path $_.FullName -Destination $target -Force }
                }
            }
        }
        if (Test-Path (Join-Path $torDir 'tor.exe')) {
            Write-Ok "tor.exe installed at $torDir"
        } else {
            throw "extraction did not produce tor.exe in $torDir"
        }
    }
} else {
    $found = $null
    foreach ($p in @(
        "$env:ProgramFiles\Tor Browser\Browser\TorBrowser\Tor\tor.exe",
        "${env:ProgramFiles(x86)}\Tor Browser\Browser\TorBrowser\Tor\tor.exe",
        "$env:LOCALAPPDATA\Tor Browser\Browser\TorBrowser\Tor\tor.exe",
        "$env:LOCALAPPDATA\codx\tor\tor.exe"
    )) { if (Test-Path $p) { $found = $p; break } }
    if ($found) { Write-Ok "tor.exe found: $found" }
    else { Write-Warn2 'tor.exe not found - WARP works alone; run with -InstallTor for the fallback' }
}

# ---------------------------------------------------------------- 5. shortcut
if (-not $NoShortcut) {
    Write-Step 'Creating Desktop shortcut'
    $desktop = [Environment]::GetFolderPath('Desktop')
    $lnk = Join-Path $desktop 'codx.lnk'
    $shell = New-Object -ComObject WScript.Shell
    $sc = $shell.CreateShortcut($lnk)
    $sc.TargetPath = $pythonExe
    if ($pythonPre.Length -gt 0) {
        $sc.Arguments = ($pythonPre -join ' ') + ' -m codx'
    } else {
        $sc.Arguments = '-m codx'
    }
    $sc.WorkingDirectory = $root
    $sc.Description = 'codx - IP rotation manager'
    $sc.IconLocation = "$env:SystemRoot\System32\shell32.dll,13"
    $sc.Save()
    Write-Ok "shortcut: $lnk"
}

# ---------------------------------------------------------------- 6. autostart
if ($Autostart) {
    Write-Step 'Registering logon task (headless)'
    $argList = @()
    if ($pythonPre.Length -gt 0) { $argList += $pythonPre }
    $argList += @('-m', 'codx', '--headless')
    $action = New-ScheduledTaskAction -Execute $pythonExe -Argument ($argList -join ' ') -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    Register-ScheduledTask -TaskName 'codx' -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    Write-Ok 'scheduled task "codx" registered'
}

Write-Host ''
Write-Host 'Build complete.' -ForegroundColor Green
Write-Host ("  run the window : {0} {1} -m codx" -f $pythonExe, ($pythonPre -join ' '))
Write-Host ("  run headless   : {0} {1} -m codx --headless" -f $pythonExe, ($pythonPre -join ' '))
Write-Host ("  self-test      : {0} {1} -m codx --selftest" -f $pythonExe, ($pythonPre -join ' '))
