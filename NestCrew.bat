@echo off
title NestCrew (close this window to quit)
cd /d "%~dp0"
if not exist "data\installed.ok" (
  echo NestCrew isn't fully installed yet - starting the installer...
  start "NestCrew installer" "Install NestCrew.bat"
  exit /b
)
rem start Ollama in the background if it isn't running
rem (NestCrew also starts Ollama itself if needed)
if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" set "PATH=%LOCALAPPDATA%\Programs\Ollama;%PATH%"
rem new features may need a small package (Excel support)
".venv\Scripts\python.exe" -c "import openpyxl" >nul 2>nul || ".venv\Scripts\python.exe" -m pip install -q "openpyxl>=3.1"
rem already running? just open the window
powershell -NoProfile -Command "try{Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8765/ -TimeoutSec 2 | Out-Null; exit 0}catch{exit 1}" >nul 2>nul
if not errorlevel 1 (
  start "" "http://127.0.0.1:8765"
  exit /b 0
)
echo NestCrew is running. Keep this window open; close it to quit.
".venv\Scripts\nestcrew.exe" app
