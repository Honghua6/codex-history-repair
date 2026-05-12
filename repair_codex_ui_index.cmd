@echo off
chcp 65001 >nul
cd /d "%~dp0.."
echo Codex UI history index repair
echo.
echo Make sure Codex is fully closed. If it is still open, close Codex and run this file again.
echo.
python "%~dp0codex_history_keeper.py" --repair-ui-index --apply-repair --provider-mode current
echo.
if errorlevel 1 (
  echo Repair failed. The usual reason is that Codex is still running.
) else (
  echo Repair completed. You can open Codex again now.
)
echo.
pause
