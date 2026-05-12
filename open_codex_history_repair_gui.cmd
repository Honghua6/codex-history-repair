@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

call :find_python
if errorlevel 1 goto :python_missing

call :check_tkinter
if errorlevel 1 goto :tkinter_missing

"%PYTHON_EXE%" %PYTHON_ARGS% "%~dp0codex_history_repair_gui.py"
pause
exit /b 0

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

:check_tkinter
"%PYTHON_EXE%" %PYTHON_ARGS% -c "import tkinter" >nul 2>nul
exit /b %errorlevel%

:python_missing
echo Python 3.11+ was not found.
echo.
echo This tool can try to install Python automatically with winget.
choice /C YN /M "Install Python 3.13 now"
if errorlevel 2 goto :install_declined_gui
call :install_python
if errorlevel 1 exit /b 1
call :find_python
if errorlevel 1 goto :python_not_ready
call :check_tkinter
if errorlevel 1 goto :tkinter_missing
"%PYTHON_EXE%" %PYTHON_ARGS% "%~dp0codex_history_repair_gui.py"
pause
exit /b 0

:tkinter_missing
echo Python was found, but tkinter is not available in that installation.
echo.
echo The easiest fix is to install the official Python 3.13 package with tkinter included.
choice /C YN /M "Install Python 3.13 now"
if errorlevel 2 goto :install_declined_tk
call :install_python
if errorlevel 1 exit /b 1
call :find_python
if errorlevel 1 goto :python_not_ready
call :check_tkinter
if errorlevel 1 (
  echo tkinter is still unavailable after installation.
  echo Please open Python's installer and make sure Tcl/Tk is included.
  echo.
  pause
  exit /b 1
)
"%PYTHON_EXE%" %PYTHON_ARGS% "%~dp0codex_history_repair_gui.py"
pause
exit /b 0

:install_declined_gui
echo Installation was cancelled.
echo Please install Python 3.11+ from https://www.python.org/downloads/windows/
echo and then run this launcher again.
echo.
pause
exit /b 1

:python_not_ready
echo Python installation appears to have finished, but this window cannot find Python yet.
echo Please close this window, open the launcher again, or open a new terminal and retry.
echo If needed, restart Windows so PATH and the py launcher refresh completely.
echo.
pause
exit /b 1

:install_declined_tk
echo Installation was cancelled.
echo Please install the official Python 3.13 package from https://www.python.org/downloads/windows/
echo and make sure Tcl/Tk is included.
echo.
pause
exit /b 1

:install_python
where winget >nul 2>nul
if errorlevel 1 (
  echo winget is not available on this system.
  echo Please install Python 3.13 from https://www.python.org/downloads/windows/
  echo and keep Tcl/Tk selected.
  echo.
  pause
  exit /b 1
)
echo Installing Python 3.13 with winget...
winget install --id Python.Python.3.13 --exact --accept-package-agreements --accept-source-agreements
if errorlevel 1 (
  echo Automatic installation failed.
  echo Please install Python 3.13 manually from https://www.python.org/downloads/windows/
  echo and keep Tcl/Tk selected.
  echo.
  pause
  exit /b 1
)
echo Installation completed. Retrying...
exit /b 0
