@echo off
setlocal EnableExtensions DisableDelayedExpansion
echo ============================================================================
echo GaN FET Characterization - Release Packaging Tool
echo ============================================================================
echo.

set "GAN_PACKAGE_MODE=patch"
set "GAN_PACKAGE_VERSION="
set "GAN_PACKAGE_NO_NETWORK=0"

if /I "%~1"=="--no-bump" (
    set "GAN_PACKAGE_MODE=none"
    shift
) else if /I "%~1"=="--minor" (
    set "GAN_PACKAGE_MODE=minor"
    shift
) else if /I "%~1"=="--major" (
    set "GAN_PACKAGE_MODE=major"
    shift
) else if /I "%~1"=="--set-version" (
    if "%~2"=="" (
        echo Missing X.Y.Z value after --set-version.
        exit /b 2
    )
    set "GAN_PACKAGE_MODE=set"
    set "GAN_PACKAGE_VERSION=%~2"
    shift
    shift
)

if /I "%~1"=="--no-network" (
    set "GAN_PACKAGE_NO_NETWORK=1"
    shift
)
if not "%~1"=="" (
    echo Unsupported or extra packaging argument: %~1
    echo Usage: PACKAGE-GAN-FET.bat [--no-bump^|--minor^|--major^|--set-version X.Y.Z] [--no-network]
    exit /b 2
)

if "%GAN_PACKAGE_MODE%"=="set" (
    powershell.exe -NoProfile -Command "if ($env:GAN_PACKAGE_VERSION -notmatch '^\d+\.\d+\.\d+$') { exit 2 }"
    if errorlevel 1 (
        echo Version must have the form X.Y.Z.
        exit /b 2
    )
)

if "%GAN_PACKAGE_MODE%"=="set" goto package_set
if "%GAN_PACKAGE_MODE%"=="none" goto package_none
goto package_bump

:package_set
if "%GAN_PACKAGE_NO_NETWORK%"=="1" (
    python "%~dp0scripts\package_windows.py" --set-version "%GAN_PACKAGE_VERSION%" --no-network
) else (
    python "%~dp0scripts\package_windows.py" --set-version "%GAN_PACKAGE_VERSION%"
)
goto package_done

:package_none
if "%GAN_PACKAGE_NO_NETWORK%"=="1" (
    python "%~dp0scripts\package_windows.py" --no-network
) else (
    python "%~dp0scripts\package_windows.py"
)
goto package_done

:package_bump
if "%GAN_PACKAGE_NO_NETWORK%"=="1" (
    python "%~dp0scripts\package_windows.py" --bump %GAN_PACKAGE_MODE% --no-network
) else (
    python "%~dp0scripts\package_windows.py" --bump %GAN_PACKAGE_MODE%
)

:package_done
set "PACKAGE_EXIT_CODE=%ERRORLEVEL%"
if not "%PACKAGE_EXIT_CODE%"=="0" (
    echo.
    echo Packaging failed with exit code %PACKAGE_EXIT_CODE%. Review the error above.
    pause
    exit /b %PACKAGE_EXIT_CODE%
)

echo.
echo Release ZIP and SHA256 sidecar created in:
echo   %~dp0dist
if "%GAN_PACKAGE_NO_NETWORK%"=="0" (
    echo Published to:
    echo   C:\Bench_Software\Projects\Matthew_De_Jesus_Python_Sandbox\GaN-FET-Releases
) else (
    echo Network publishing was skipped.
)
pause
exit /b 0
