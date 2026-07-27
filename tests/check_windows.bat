@echo off
setlocal
title MediaForge Diagnostics Suite (Windows)

set "REPO_DIR=%~dp0.."

:MENU
cls
echo ========================================================================
echo            MediaForge Diagnostics and Testing Suite (Windows)
echo ========================================================================
echo.
echo   [1] Hardware Encoder, NVENC and VAAPI Diagnostics
echo   [2] Run the Test Suite (pytest)
echo   [3] Run the Repository Checks (static assets, translations, line endings)
echo   [4] Run Everything CI Runs (checks + tests)
echo   [5] Open Diagnostics Log Directory
echo   [0] Exit
echo.
echo ========================================================================
set /p choice="Select an option (0-5): "

if "%choice%"=="1" goto TEST_ENCODING
if "%choice%"=="2" goto RUN_TESTS
if "%choice%"=="3" goto RUN_CHECKS
if "%choice%"=="4" goto RUN_ALL
if "%choice%"=="5" goto OPEN_LOGS
if "%choice%"=="0" goto EXIT_MENU

echo Invalid selection. Please press any key to try again.
pause >nul
goto MENU

:TEST_ENCODING
cls
echo Starting Hardware Encoder and NVENC Diagnostics...
echo.
python "%~dp0encoding\check_nvenc.py"
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Failed to execute diagnostic script. Make sure Python 3 is installed and in PATH.
    pause
)
goto MENU

:RUN_TESTS
cls
echo Running the test suite...
echo.
call :DO_TESTS
echo.
pause
goto MENU

:RUN_CHECKS
cls
echo Running the repository checks...
echo.
call :DO_CHECKS
echo.
pause
goto MENU

:RUN_ALL
cls
echo Running the repository checks, then the test suite...
echo.
call :DO_CHECKS
echo.
call :DO_TESTS
echo.
pause
goto MENU

:DO_TESTS
python -c "import pytest" >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] pytest is not installed. Install the test extra first:
    echo         python -m pip install -e ".[test]"
    exit /b 1
)
pushd "%REPO_DIR%"
python -m pytest -q
popd
exit /b 0

:DO_CHECKS
pushd "%REPO_DIR%"
python .github\scripts\check_repo.py
popd
exit /b 0

:OPEN_LOGS
if not exist "%~dp0Log" mkdir "%~dp0Log"
start "" "%~dp0Log"
goto MENU

:EXIT_MENU
endlocal
exit /b 0
