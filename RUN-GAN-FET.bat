@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem ============================================================================
rem GaN FET Characterization - Unified Interactive Launcher & Installer
rem ============================================================================
set "GAN_RELEASE_SHARE=C:\Bench_Software\Projects\Matthew_De_Jesus_Python_Sandbox\GaN-FET-Releases"
set "GAN_DEFAULT_INSTALL_DIR=%LOCALAPPDATA%\GaN-FET-Characterisation"
rem ============================================================================

set "GAN_UNIFIED_BOOTSTRAP=%~f0"
set "GAN_LAUNCH_MODE="

if "%~1"=="" goto launch
if not "%~2"=="" goto invalid_args
if /I "%~1"=="--simulate" set "GAN_LAUNCH_MODE=simulate"
if /I "%~1"=="-s" set "GAN_LAUNCH_MODE=simulate"
if /I "%~1"=="--diagnose" set "GAN_LAUNCH_MODE=diagnose"
if /I "%~1"=="--setup" set "GAN_LAUNCH_MODE=setup"
if /I "%~1"=="--update" set "GAN_LAUNCH_MODE=update"
if /I "%~1"=="--help" set "GAN_LAUNCH_MODE=help"
if /I "%~1"=="-h" set "GAN_LAUNCH_MODE=help"
if /I "%~1"=="/?" set "GAN_LAUNCH_MODE=help"
if not defined GAN_LAUNCH_MODE goto invalid_args
goto launch

:invalid_args
echo Unsupported arguments. Run RUN-GAN-FET.bat --help for valid fixed flags.
exit /b 2

:launch
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$marker = '#' + '<GAN_POWERSHELL>'; $lines = @(Get-Content -LiteralPath $env:GAN_UNIFIED_BOOTSTRAP); $index = 0; while ($index -lt $lines.Count -and $lines[$index] -ne $marker) { $index++ }; if ($index -ge $lines.Count - 1) { Write-Host 'The embedded launcher is missing.' -ForegroundColor Red; exit 2 }; $source = $lines[($index + 1)..($lines.Count - 1)] -join [Environment]::NewLine; & ([ScriptBlock]::Create($source))"
set "GAN_LAUNCHER_EXIT_CODE=%ERRORLEVEL%"

if not "%GAN_LAUNCHER_EXIT_CODE%"=="0" (
    echo.
    echo GaN FET Test Runner setup or launch failed. Review the error above.
    pause
    exit /b %GAN_LAUNCHER_EXIT_CODE%
)

exit /b 0

#<GAN_POWERSHELL>

$ErrorActionPreference = 'Stop'

$LaunchMode = [string]$env:GAN_LAUNCH_MODE
$WantSimulate = $LaunchMode -eq 'simulate'
$WantDiagnose = $LaunchMode -eq 'diagnose'
$WantSetup = $LaunchMode -eq 'setup'
$WantUpdate = $LaunchMode -eq 'update'
$WantHelp = $LaunchMode -eq 'help'
$AppArgs = switch ($LaunchMode) {
    'simulate' { @('--simulate') }
    'diagnose' { @('--diagnose') }
    default { @() }
}

function Write-Banner {
    param([string]$Title)
    Write-Host "`n============================================================================" -ForegroundColor Cyan
    Write-Host " $Title" -ForegroundColor Cyan
    Write-Host "============================================================================`n" -ForegroundColor Cyan
}

if ($WantHelp) {
    Write-Banner "GaN FET Characterization - Unified Launcher Help"
    Write-Host "Usage:" -ForegroundColor Yellow
    Write-Host "  RUN-GAN-FET.bat [options]`n" -ForegroundColor White
    Write-Host "Available Commands:" -ForegroundColor Yellow
    Write-Host "  RUN-GAN-FET.bat              Normal launch (Interactive Menu / Live Hardware)"
    Write-Host "  RUN-GAN-FET.bat --simulate   Run Simulation Mode (Virtual SCPI instruments)"
    Write-Host "  RUN-GAN-FET.bat --diagnose   Run hardware self-test diagnostic connectivity check"
        Write-Host "  RUN-GAN-FET.bat --setup      Run environment bootstrap & create desktop shortcut"
    Write-Host "  RUN-GAN-FET.bat --update     Force check and install latest release update`n"
    exit 0
}

function Prompt-YesNo {
    param(
        [Parameter(Mandatory)][string]$PromptMessage,
        [bool]$DefaultYes = $true
    )
    $ChoiceHint = if ($DefaultYes) { "[Y/n]" } else { "[y/N]" }
    while ($true) {
        $Response = Read-Host "$PromptMessage $ChoiceHint"
        if ([string]::IsNullOrWhiteSpace($Response)) {
            return $DefaultYes
        }
        $Lower = $Response.Trim().ToLower()
        if ($Lower -eq 'y' -or $Lower -eq 'yes') { return $true }
        if ($Lower -eq 'n' -or $Lower -eq 'no') { return $false }
        Write-Host "Please enter 'y' for Yes or 'n' for No." -ForegroundColor Yellow
    }
}

function Get-InstalledDirectory {
    $BootstrapPath = [IO.Path]::GetFullPath([string]$env:GAN_UNIFIED_BOOTSTRAP)
    $BootstrapDir = (Split-Path -Parent $BootstrapPath).TrimEnd('\')
    return $BootstrapDir
}

function Get-InstalledVersion {
    param([string]$InstallDir)
    $VersionSource = Join-Path $InstallDir 'gan_fet\__init__.py'
    if (Test-Path -LiteralPath $VersionSource -PathType Leaf) {
        $Content = Get-Content -LiteralPath $VersionSource -Raw
        $Match = [regex]::Match($Content, '(?m)^__version__\s*=\s*["''](\d+\.\d+\.\d+)["'']\s*$')
        if ($Match.Success) {
            return [version]$Match.Groups[1].Value
        }
    }
    return [version]'0.0.0'
}

function Get-LatestNetworkRelease {
    param([string]$ReleaseShare)
    if (-not (Test-Path -LiteralPath $ReleaseShare -PathType Container)) {
        return $null
    }
    $Candidates = Get-ChildItem -LiteralPath $ReleaseShare -Filter 'GaN-FET-Characterisation-v*-windows.zip' -File -ErrorAction SilentlyContinue
    $Best = $null
    $BestVer = [version]'0.0.0'
    foreach ($Item in $Candidates) {
        $Match = [regex]::Match($Item.Name, 'GaN-FET-Characterisation-v(\d+\.\d+\.\d+)-windows\.zip')
        if ($Match.Success) {
            $Ver = [version]$Match.Groups[1].Value
            if ($Ver -gt $BestVer) {
                $BestVer = $Ver
                $Best = $Item
            }
        }
    }
    if ($null -ne $Best) {
        return @{ Zip = $Best.FullName; Version = $BestVer }
    }
    return $null
}

function New-DesktopShortcut {
    param([string]$InstallDir)
    try {
        $DesktopPath = [Environment]::GetFolderPath('Desktop')
        if ([string]::IsNullOrWhiteSpace($DesktopPath) -or -not (Test-Path -LiteralPath $DesktopPath)) {
            $DesktopPath = Join-Path $env:USERPROFILE 'Desktop'
        }
        $ShortcutPath = Join-Path $DesktopPath 'GaN Device Test Runner.lnk'
        $TargetPath = Join-Path $InstallDir 'RUN-GAN-FET.bat'

        $WshShell = New-Object -ComObject WScript.Shell
        $Shortcut = $WshShell.CreateShortcut($ShortcutPath)
        $Shortcut.TargetPath = $TargetPath
        $Shortcut.WorkingDirectory = $InstallDir
        $Shortcut.Description = "GaN Device Test Runner - Unified Launcher"
        $Shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll, 14"
        $Shortcut.Save()
        Write-Host "`nDesktop shortcut created: $ShortcutPath" -ForegroundColor Green
    } catch {
        Write-Host "`nNote: Could not create Desktop shortcut: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

function Ensure-VirtualEnvironment {
    param([string]$InstallDir)
    $VenvPy = Join-Path $InstallDir '.venv\Scripts\python.exe'
    Set-Location -LiteralPath $InstallDir
    $UvExe = Get-Command 'uv' -ErrorAction SilentlyContinue
    if (-not (Test-Path -LiteralPath $VenvPy -PathType Leaf)) {
        Write-Host "`nSetting up Python virtual environment in $InstallDir..." -ForegroundColor Cyan
        if ($null -ne $UvExe) {
            & $UvExe.Source venv .venv
        } else {
            python -m venv .venv
        }
        if ($LASTEXITCODE -ne 0) { throw "Virtual environment creation failed." }
    }

    Write-Host "`nSynchronizing application dependencies..." -ForegroundColor Cyan
    if ($null -ne $UvExe) {
        & $UvExe.Source pip install --python $VenvPy -e $InstallDir "pyserial>=3.5"
    } else {
        & $VenvPy -m pip install -e $InstallDir "pyserial>=3.5"
    }
    if ($LASTEXITCODE -ne 0) { throw "Dependency synchronization failed." }
    Write-Host "Application environment is ready." -ForegroundColor Green
}

function Invoke-UpdateFromNetwork {
    param([string]$InstallDir, $LatestRel)
    Write-Host "`nInstalling update v$($LatestRel.Version) from network share..." -ForegroundColor Green

    $TempStage = Join-Path $env:TEMP ("gan-stage-" + [guid]::NewGuid().ToString('N'))
    $TempBackup = Join-Path $env:TEMP ("gan-backup-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $TempStage -Force | Out-Null
    New-Item -ItemType Directory -Path $TempBackup -Force | Out-Null
    $ExtractRoot = Join-Path $TempStage 'payload'
    New-Item -ItemType Directory -Path $ExtractRoot -Force | Out-Null
    $ManagedPaths = @(
        'gan_fet',
        'scripts',
        'docs',
        'README.md',
        'RUN-GAN-FET.bat',
        'PACKAGE-GAN-FET.bat',
        'pyproject.toml'
    )

    try {
        $NetworkSha = "$($LatestRel.Zip).sha256"
        if (-not (Test-Path -LiteralPath $NetworkSha -PathType Leaf)) {
            throw "Release checksum sidecar is missing: $NetworkSha"
        }
        $LocalZip = Join-Path $TempStage 'release.zip'
        $LocalSha = Join-Path $TempStage 'release.zip.sha256'
        Copy-Item -LiteralPath $LatestRel.Zip -Destination $LocalZip
        Copy-Item -LiteralPath $NetworkSha -Destination $LocalSha

        $ShaLine = (Get-Content -LiteralPath $LocalSha -TotalCount 1).Trim()
        $ShaMatch = [regex]::Match($ShaLine, '^(?<hash>[0-9A-Fa-f]{64})(?:\s+.+)?$')
        if (-not $ShaMatch.Success) {
            throw "Release checksum sidecar has an invalid format."
        }
        $ExpectedHash = $ShaMatch.Groups['hash'].Value.ToUpperInvariant()
        $ActualHash = (Get-FileHash -LiteralPath $LocalZip -Algorithm SHA256).Hash.ToUpperInvariant()
        if ($ActualHash -ne $ExpectedHash) {
            throw "Release SHA256 verification failed. The update was not installed."
        }

        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $StagePrefix = [IO.Path]::GetFullPath($ExtractRoot).TrimEnd('\') + '\'
        $Archive = [IO.Compression.ZipFile]::OpenRead($LocalZip)
        try {
            foreach ($Entry in $Archive.Entries) {
                $EntryName = $Entry.FullName.Replace('/', '\')
                if ([IO.Path]::IsPathRooted($EntryName) -or $EntryName.Contains(':')) {
                    throw "Unsafe rooted or alternate-stream path in release archive: $($Entry.FullName)"
                }
                $Candidate = [IO.Path]::GetFullPath((Join-Path $ExtractRoot $EntryName))
                if (-not $Candidate.StartsWith($StagePrefix, [StringComparison]::OrdinalIgnoreCase)) {
                    throw "Unsafe path in release archive: $($Entry.FullName)"
                }
            }
        }
        finally {
            $Archive.Dispose()
        }

        Expand-Archive -LiteralPath $LocalZip -DestinationPath $ExtractRoot -Force
        $PayloadRoot = $ExtractRoot
        $SubDirs = Get-ChildItem -LiteralPath $ExtractRoot -Directory
        if ($SubDirs.Count -eq 1 -and (Test-Path -LiteralPath (Join-Path $SubDirs[0].FullName 'pyproject.toml'))) {
            $PayloadRoot = $SubDirs[0].FullName
        }
        if (-not (Test-Path -LiteralPath (Join-Path $PayloadRoot 'gan_fet\__init__.py') -PathType Leaf)) {
            throw "Release payload is missing its canonical version file."
        }
        $PayloadVersion = Get-InstalledVersion -InstallDir $PayloadRoot
        if ($PayloadVersion -ne $LatestRel.Version) {
            throw "Release filename version does not match its payload."
        }

        foreach ($RelativePath in $ManagedPaths) {
            $CurrentPath = Join-Path $InstallDir $RelativePath
            if (Test-Path -LiteralPath $CurrentPath) {
                Copy-Item -LiteralPath $CurrentPath -Destination $TempBackup -Recurse -Force
            }
        }

        try {
            foreach ($RelativePath in $ManagedPaths) {
                $CurrentPath = Join-Path $InstallDir $RelativePath
                $NewPath = Join-Path $PayloadRoot $RelativePath
                if (Test-Path -LiteralPath $CurrentPath) {
                    Remove-Item -LiteralPath $CurrentPath -Recurse -Force
                }
                if (Test-Path -LiteralPath $NewPath) {
                    Copy-Item -LiteralPath $NewPath -Destination $CurrentPath -Recurse -Force
                }
            }
            Ensure-VirtualEnvironment -InstallDir $InstallDir
        }
        catch {
            Write-Host "Update failed; restoring the previous application files..." -ForegroundColor Yellow
            foreach ($RelativePath in $ManagedPaths) {
                $CurrentPath = Join-Path $InstallDir $RelativePath
                $BackupPath = Join-Path $TempBackup $RelativePath
                if (Test-Path -LiteralPath $CurrentPath) {
                    Remove-Item -LiteralPath $CurrentPath -Recurse -Force
                }
                if (Test-Path -LiteralPath $BackupPath) {
                    Copy-Item -LiteralPath $BackupPath -Destination $CurrentPath -Recurse -Force
                }
            }
            try { Ensure-VirtualEnvironment -InstallDir $InstallDir } catch {}
            throw
        }

        Write-Host "`nSuccessfully updated to v$($LatestRel.Version)!" -ForegroundColor Green
    }
    finally {
        Remove-Item -LiteralPath $TempStage -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $TempBackup -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# ============================================================================
# MAIN WORKFLOW
# ============================================================================

$InstallDir = Get-InstalledDirectory
$ReleaseShare = [IO.Path]::GetFullPath([string]$env:GAN_RELEASE_SHARE)

Ensure-VirtualEnvironment -InstallDir $InstallDir
$VenvPython = Join-Path $InstallDir '.venv\Scripts\python.exe'
$CurrentVer = Get-InstalledVersion -InstallDir $InstallDir

# ----------------------------------------------------------------------------
# PRE-MENU UPDATE CHECK WITH STANDALONE Y/N PROMPT
# ----------------------------------------------------------------------------
$LatestRel = Get-LatestNetworkRelease -ReleaseShare $ReleaseShare
if ($WantUpdate -and $null -eq $LatestRel) {
    throw "No valid release was found at $ReleaseShare"
}
if ($null -ne $LatestRel -and ($WantUpdate -or $LatestRel.Version -gt $CurrentVer)) {
    Write-Banner "*** UPDATE AVAILABLE ***"
    Write-Host " A newer version (v$($LatestRel.Version)) is available on the network share." -ForegroundColor Yellow
    Write-Host " Current installed version: v$CurrentVer`n" -ForegroundColor White

    $DoUpdate = $WantUpdate -or (Prompt-YesNo "Would you like to install the latest network update now?" -DefaultYes $true)
    if ($DoUpdate) {
        Invoke-UpdateFromNetwork -InstallDir $InstallDir -LatestRel $LatestRel
        $CurrentVer = Get-InstalledVersion -InstallDir $InstallDir
    } else {
        Write-Host "`nSkipping update. Proceeding with v$CurrentVer..." -ForegroundColor Gray
    }
}

# ----------------------------------------------------------------------------
# CLI DIRECT LAUNCH FLAGS
# ----------------------------------------------------------------------------
if ($WantSimulate) {
    Write-Banner "Launching GaN FET Test Runner in Simulation Mode..."
    Set-Location -LiteralPath $InstallDir
    & $VenvPython -m gan_fet @AppArgs
    exit $LASTEXITCODE
}

if ($WantDiagnose) {
    Write-Banner "Running Hardware Diagnostics..."
    Set-Location -LiteralPath $InstallDir
    & $VenvPython -m gan_fet @AppArgs
    exit $LASTEXITCODE
}

if ($WantSetup) {
    New-DesktopShortcut -InstallDir $InstallDir
    Write-Host "`nSetup complete!" -ForegroundColor Green
    exit 0
}

# ----------------------------------------------------------------------------
# INTERACTIVE CONTROL MENU
# ----------------------------------------------------------------------------
Write-Banner "GaN Device Test Runner v$CurrentVer - Control Panel"
Write-Host "  Installed Version: v$CurrentVer" -ForegroundColor White
Write-Host "  Installation Path: $InstallDir`n" -ForegroundColor White

Write-Host "Select an option [1-5, default 1]:" -ForegroundColor Yellow
Write-Host "  [1] Run GaN FET Test Runner          (Live Bench Hardware)" -ForegroundColor Green
Write-Host "  [2] Run Simulation Mode             (Virtual SCPI Instruments for Safe Testing)" -ForegroundColor Cyan
Write-Host "  [3] Run Hardware Diagnostics        (--diagnose self-test)"
Write-Host "  [4] Create Desktop Shortcut         (Generate 'GaN Device Test Runner.lnk')"
Write-Host "  [5] Exit`n"

$Choice = Read-Host "Select option [default 1]"
if ([string]::IsNullOrWhiteSpace($Choice)) { $Choice = '1' }

switch ($Choice.Trim()) {
    '2' {
        Write-Banner "Launching GaN FET Test Runner in Simulation Mode..."
        Set-Location -LiteralPath $InstallDir
        & $VenvPython -m gan_fet --simulate
        $AppExitCode = $LASTEXITCODE
    }
    '3' {
        Write-Banner "Running Hardware Diagnostics..."
        Set-Location -LiteralPath $InstallDir
        & $VenvPython -m gan_fet --diagnose
        $AppExitCode = $LASTEXITCODE
    }
    '4' {
        New-DesktopShortcut -InstallDir $InstallDir
        $AppExitCode = 0
    }
    '6' {
        Write-Host "Exiting." -ForegroundColor Yellow
        exit 0
    }
    '1' {
        Write-Banner "Launching GaN FET Test Runner..."
        Set-Location -LiteralPath $InstallDir
        & $VenvPython -m gan_fet
        $AppExitCode = $LASTEXITCODE
    }
    Default {
        Write-Host "Invalid menu choice. No hardware application was launched." -ForegroundColor Red
        $AppExitCode = 2
    }
}
exit $AppExitCode
