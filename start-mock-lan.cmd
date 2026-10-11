@echo off
rem aod-bridge/start-mock-lan.cmd - double-click launcher for start-mock-lan.ps1
rem Usage: start-mock-lan.cmd [port] [token] [python.exe]
setlocal
cd /d "%~dp0"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8790"
set "TOKEN=%~2"
if "%TOKEN%"=="" set "TOKEN=devtoken123"
set "PYEXE=%~3"

if defined PYEXE goto run
python --version >nul 2>nul && set "PYEXE=python"
if not defined PYEXE py -3 --version >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE if exist "C:\Program Files\KiCad\10.0\bin\python.exe" set "PYEXE=C:\Program Files\KiCad\10.0\bin\python.exe"
if not defined PYEXE if exist "C:\Program Files\PyManager\python.exe" set "PYEXE=C:\Program Files\PyManager\python.exe"
if not defined PYEXE for %%P in ("%LOCALAPPDATA%\Programs\Python\Python313\python.exe" "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%LOCALAPPDATA%\Programs\Python\Python311\python.exe") do if not defined PYEXE if exist "%%~fP" set "PYEXE=%%~fP"

if not defined PYEXE (
  echo.
  echo [x] No usable Python 3 found.
  echo     Install Python 3.8+ and check "Add python.exe to PATH",
  echo     or pass the full path:  start-mock-lan.cmd 8790 mytoken "C:\path\to\python.exe"
  echo.
  pause
  exit /b 1
)

:run
echo Using Python: %PYEXE%
echo.
if "%~3"=="" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-mock-lan.ps1" -Port %PORT% -Token "%TOKEN%"
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-mock-lan.ps1" -Port %PORT% -Token "%TOKEN%" -Python "%~3"
)
echo.
echo [relay stopped] Press any key to close.
pause >nul
