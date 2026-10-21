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
".venv\Scripts\python.exe" -c "import send2trash" >nul 2>nul || ".venv\Scripts\python.exe" -m pip install -q "send2trash>=1.8"
".venv\Scripts\python.exe" -c "import qrcode" >nul 2>nul || ".venv\Scripts\python.exe" -m pip install -q "qrcode>=7.4"
rem already running? just open the window
powershell -NoProfile -Command "try{Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8765/ -TimeoutSec 2 | Out-Null; exit 0}catch{exit 1}" >nul 2>nul
if not errorlevel 1 (
  start "" "http://127.0.0.1:8765"
  exit /b 0
)
echo NestCrew is running. Keep this window open; close it to quit.
".venv\Scripts\nestcrew.exe" app 2> "data\last-start.log"
if errorlevel 1 (
  echo.
  echo NestCrew stopped with a problem:
  type "data\last-start.log"
  echo.
  echo These details are saved in data\last-start.log
  pause
)
