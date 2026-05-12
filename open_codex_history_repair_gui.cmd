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
for %%P in ("%LocalAppData%\Programs\Python\Python313\python.exe" "%LocalAppData%\Programs\Python\Python312\python.exe" "%LocalAppData%\Programs\Python\Python311\python.exe") do (
  if exist "%%~fP" (
    "%%~fP" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
    if not errorlevel 1 (
      set "PYTHON_EXE=%%~fP"
      set "PYTHON_ARGS="
      exit /b 0
    )
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
echo This tool can try to install Python automatically.
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
echo You can run this launcher again whenever you are ready to install Python.
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
echo You can run this launcher again whenever you are ready to install Python with tkinter.
echo.
pause
exit /b 1

:install_python
where winget >nul 2>nul
if errorlevel 1 (
  call :install_python_from_web
  exit /b %errorlevel%
)
echo Installing Python 3.13 with winget...
winget install --id Python.Python.3.13 --exact --accept-package-agreements --accept-source-agreements
if errorlevel 1 (
  echo winget installation failed. Trying a direct download from python.org...
  call :install_python_from_web
  exit /b %errorlevel%
)
echo Installation completed. Retrying...
exit /b 0

:install_python_from_web
set "PYTHON_INSTALLER_PATH=%TEMP%\codex-history-repair-python-3.13-amd64.exe"
echo Downloading the official Python installer from python.org...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ProgressPreference='SilentlyContinue';" ^
  "$releasePage = 'https://www.python.org/downloads/latest/python3.13/';" ^
  "$pattern = 'https://www\.python\.org/ftp/python/3\.13\.[0-9]+/python-3\.13\.[0-9]+-amd64\.exe';" ^
  "$response = Invoke-WebRequest -UseBasicParsing $releasePage;" ^
  "$url = [regex]::Match($response.Content, $pattern).Value;" ^
  "if (-not $url) { throw 'No official Python 3.13 Windows installer link was found.' }" ^
  "Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile '%PYTHON_INSTALLER_PATH%';" ^
  "$item = Get-Item '%PYTHON_INSTALLER_PATH%' -ErrorAction Stop;" ^
  "if ($item.Length -le 0) { throw 'Downloaded installer is empty.' }"
if errorlevel 1 (
  echo Automatic download failed.
  echo Please check your network connection and try running this launcher again.
  echo.
  pause
  exit /b 1
)
echo Running the downloaded installer...
"%PYTHON_INSTALLER_PATH%" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_pip=1 Include_tcltk=1 Shortcuts=0
set "INSTALL_EXIT=%ERRORLEVEL%"
if not "%INSTALL_EXIT%"=="0" (
  echo Automatic installation failed with exit code %INSTALL_EXIT%.
  echo Please run this launcher again and try once more.
  echo.
  pause
  exit /b 1
)
if exist "%PYTHON_INSTALLER_PATH%" del /q "%PYTHON_INSTALLER_PATH%" >nul 2>nul
echo Installation completed. Retrying...
exit /b 0
