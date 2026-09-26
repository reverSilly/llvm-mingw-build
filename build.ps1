<#
.SYNOPSIS
    Windows wrapper around build.py.

.DESCRIPTION
    Pulls the latest LLVM and builds it with the latest winlibs MinGW-w64 GCC.
    This is the entry point to use on Windows (where the winlibs binaries are
    actually executable).

.EXAMPLE
    .\build.ps1 info

.EXAMPLE
    .\build.ps1 all --scope core --archive zip,7z

.EXAMPLE
    .\build.ps1 all --scope everything --jobs 8

.NOTES
    Pass build.py's own flags with two dashes (--scope, not -Scope): PowerShell
    would otherwise try to bind them as parameters of this wrapper.
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$BuildArgs = @()
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

function Find-Python {
    foreach ($cand in @('python', 'py', 'python3')) {
        $cmd = Get-Command $cand -ErrorAction SilentlyContinue
        if ($cmd) {
            if ($cand -eq 'py') { return @('py', '-3') }
            return @($cmd.Source)
        }
    }
    throw "No Python interpreter found on PATH. Install Python 3.8+ from python.org."
}

$py = Find-Python

# Make pip-installed console scripts (cmake.exe / ninja.exe) visible.
$scripts = Join-Path ([System.IO.Path]::GetDirectoryName($py[0])) 'Scripts'
if ((Test-Path $scripts) -and ($env:PATH -notlike "*$scripts*")) {
    $env:PATH = "$scripts;$env:PATH"
}
$userScripts = Join-Path $env:APPDATA 'Python\Python3*\Scripts'
foreach ($d in (Get-Item $userScripts -ErrorAction SilentlyContinue)) {
    if ($env:PATH -notlike "*$($d.FullName)*") { $env:PATH = "$($d.FullName);$env:PATH" }
}

# $py is @('python') or @('py','-3'); keep any launcher prefix separate so that
# PowerShell never evaluates a reversed array slice.
$pyExe = $py[0]
$pyPrefix = @()
if ($py.Length -gt 1) { $pyPrefix = $py[1..($py.Length - 1)] }

$script = Join-Path $here 'build.py'
Write-Verbose "Running: $pyExe $($pyPrefix -join ' ') $script $($BuildArgs -join ' ')"
& $pyExe @pyPrefix $script @BuildArgs
exit $LASTEXITCODE
