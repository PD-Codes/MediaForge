@echo off
setlocal
title MediaForge Diagnostics Suite (Windows)

:MENU
cls
echo ========================================================================
echo            MediaForge Diagnostics and Testing Suite (Windows)
echo ========================================================================
echo.
echo   [1] Hardware Encoder, NVENC and VAAPI Diagnostics
echo   [2] Open Diagnostics Log Directory
echo   [0] Exit
echo.
echo ========================================================================
set /p choice="Select an option (0-2): "

if "%choice%"=="1" goto TEST_ENCODING
if "%choice%"=="2" goto OPEN_LOGS
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

:OPEN_LOGS
if not exist "%~dp0Log" mkdir "%~dp0Log"
start "" "%~dp0Log"
goto MENU

:EXIT_MENU
endlocal
exit /b 0
