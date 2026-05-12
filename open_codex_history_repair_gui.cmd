@echo off
chcp 65001 >nul
cd /d "%~dp0"
python codex_history_repair_gui.py
pause
