@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal
echo Codex UI history index repair
echo.
echo Make sure Codex is fully closed. If it is still open, close Codex and run this file again.
echo.
call :find_python
if errorlevel 1 goto :python_missing

"%PYTHON_EXE%" %PYTHON_ARGS% "%~dp0codex_history_keeper.py" --repair-ui-index --apply-repair --provider-mode current
set "REPAIR_EXIT=%ERRORLEVEL%"
echo.
if not "%REPAIR_EXIT%"=="0" (
  echo Repair failed. The usual reason is that Codex is still running.
) else (
  echo Repair completed. You can open Codex again now.
)
echo.
pause
exit /b %REPAIR_EXIT%

:find_python
set "PYTHON_EXE="
set "PYTHON_ARGS="
for %%V in (3.13 3.12 3.11) do (
  py -%%V -c "import sys; sys.exit(0)" >nul 2>nul
  if not errorlevel 1 (
    set "PYTHON_EXE=py"
    set "PYTHON_ARGS=-%%V"
    exit /b 0
  )
)
where python >nul 2>nul
if not errorlevel 1 (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
  if not errorlevel 1 (
    set "PYTHON_EXE=python"
    set "PYTHON_ARGS="
    exit /b 0
  )
)
exit /b 1

:python_missing
echo Python 3.11+ was not found.
echo.
echo This tool can try to install Python automatically with winget.
choice /C YN /M "Install Python 3.13 now"
if errorlevel 2 goto :install_declined
call :install_python
if errorlevel 1 exit /b 1
call :find_python
if errorlevel 1 goto :python_not_ready
"%PYTHON_EXE%" %PYTHON_ARGS% "%~dp0codex_history_keeper.py" --repair-ui-index --apply-repair --provider-mode current
set "REPAIR_EXIT=%ERRORLEVEL%"
echo.
if not "%REPAIR_EXIT%"=="0" (
  echo Repair failed. The usual reason is that Codex is still running.
) else (
  echo Repair completed. You can open Codex again now.
)
echo.
pause
exit /b %REPAIR_EXIT%

:install_declined
echo Installation was cancelled.
echo Please install Python 3.11+ from https://www.python.org/downloads/windows/
echo and then run this repair tool again.
echo.
pause
exit /b 1

:python_not_ready
echo Python installation appears to have finished, but this window cannot find Python yet.
echo Please close this window, open this repair tool again, or open a new terminal and retry.
echo If needed, restart Windows so PATH and the py launcher refresh completely.
echo.
pause
exit /b 1

:install_python
where winget >nul 2>nul
if errorlevel 1 (
  echo winget is not available on this system.
  echo Please install Python 3.13 from https://www.python.org/downloads/windows/
  echo and then run this repair tool again.
  echo.
  pause
  exit /b 1
)
echo Installing Python 3.13 with winget...
winget install --id Python.Python.3.13 --exact --accept-package-agreements --accept-source-agreements
if errorlevel 1 (
  echo Automatic installation failed.
  echo Please install Python 3.13 manually from https://www.python.org/downloads/windows/
  echo and then run this repair tool again.
  echo.
  pause
  exit /b 1
)
echo Installation completed. Retrying...
exit /b 0
