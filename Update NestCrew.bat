@echo off
title Update NestCrew
cd /d "%~dp0"
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not exist "nestcrew\updater.py" (
  echo Getting the updater from GitHub...
  powershell -NoProfile -Command "$ErrorActionPreference='Stop'; $z=Join-Path $env:TEMP 'nestcrew-update.zip'; $d=Join-Path $env:TEMP 'nestcrew-update'; Invoke-WebRequest -UseBasicParsing 'https://github.com/Bakulesh1Codes108/nestcrew/archive/refs/heads/main.zip' -OutFile $z; if(Test-Path $d){Remove-Item -Recurse -Force $d}; Expand-Archive $z $d; Copy-Item (Join-Path $d 'nestcrew-main\nestcrew\updater.py') 'nestcrew\updater.py'"
  if errorlevel 1 (
    echo Could not reach GitHub - check the internet connection and try again.
    pause
    exit /b 1
  )
)
"%PY%" -m nestcrew.updater
echo.
echo Your settings, chats, workflows and files were kept.
pause
