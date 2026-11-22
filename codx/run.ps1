<#
  codx — launcher.

      powershell -ExecutionPolicy Bypass -File .\run.ps1
      powershell -ExecutionPolicy Bypass -File .\run.ps1 -Headless
      powershell -ExecutionPolicy Bypass -File .\run.ps1 -SelfTest
      powershell -ExecutionPolicy Bypass -File .\run.ps1 -Config C:\path\config.json
#>
[CmdletBinding()]
param(
    [switch]$Headless,
    [switch]$SelfTest,
    [string]$Config = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$python = $null
foreach ($candidate in @('py -3.14', 'py -3.13', 'py -3.12', 'py -3.11', 'py -3.10', 'python')) {
    $parts = $candidate.Split(' ')
    if (-not (Get-Command $parts[0] -ErrorAction SilentlyContinue)) { continue }
    try {
        $ver = & $parts[0] @($parts[1..($parts.Length - 1)]) -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
        if (-not $ver) { continue }
        $major, $minor = $ver.Trim().Split('.')
        if ([int]$major -ge 3 -and [int]$minor -ge 10) { $python = $candidate; break }
    } catch { continue }
}
if (-not $python) { throw 'No Python 3.10+ found. Run: winget install -e --id Python.Python.3.12' }

$parts = $python.Split(' ')
$exe = $parts[0]
$pre = @()
if ($parts.Length -gt 1) { $pre = $parts[1..($parts.Length - 1)] }

$args = @('-m', 'codx')
if ($Headless) { $args += '--headless' }
if ($SelfTest) { $args += '--selftest' }
if ($Config)   { $args += @('--config', $Config) }

& $exe @($pre + $args)
exit $LASTEXITCODE
