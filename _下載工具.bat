@echo off
chcp 65001 > nul
cd /d "%~dp0"
if exist "%~dp0影音下載工具.exe" (
  start "" "%~dp0影音下載工具.exe"
  exit /b
)
if exist "%~dp0downloader_ui.py" (
  start "" pythonw -B -X utf8 "%~dp0downloader_ui.py"
  exit /b
)
echo 找不到影音下載工具.exe 或 downloader_ui.py。
pause
