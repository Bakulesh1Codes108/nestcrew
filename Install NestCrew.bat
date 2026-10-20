@echo off
title NestCrew installer
cd /d "%~dp0"
echo.
echo  ===  NestCrew installer  ===
echo  Needs internet once (Python packages + model). Works offline afterwards.
echo.

rem ---- find Python 3.10+ ----
set "PY="
where py >nul 2>nul && py -3 -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && python -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo  [!] Python 3.10 or newer was not found.
  echo      Install it from https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^)
  echo      then double-click this installer again.
  pause & exit /b 1
)
echo  [ok] Using %PY%

rem ---- virtual environment + packages ----
if not exist ".venv\Scripts\python.exe" (
  echo  [..] Creating virtual environment
  %PY% -m venv .venv || (echo [!] venv failed & pause & exit /b 1)
)
if exist ".venv\Scripts\nestcrew.exe" (
  echo  [ok] NestCrew already installed - code updates apply automatically
) else (
  echo  [..] Installing NestCrew ^(a few minutes the first time^)
  ".venv\Scripts\python.exe" -m pip install -q --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -q -e ".[dev,media]" || (echo [!] pip install failed & pause & exit /b 1)
)
if not exist ".env" copy ".env.example" ".env" >nul
echo  [ok] Packages installed

rem ---- Ollama + model ----
rem Find Ollama even if it was installed a moment ago (PATH not refreshed yet)
where ollama >nul 2>nul || if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" set "PATH=%LOCALAPPDATA%\Programs\Ollama;%PATH%"
where ollama >nul 2>nul
if errorlevel 1 (
  echo  [..] Installing Ollama ^(local model runtime^)
  winget install --id Ollama.Ollama -e --accept-source-agreements --accept-package-agreements
  set "PATH=%LOCALAPPDATA%\Programs\Ollama;%PATH%"
)
where ollama >nul 2>nul || (echo [!] Ollama not found. Install it from https://ollama.com/download and run this again. & pause & exit /b 1)
rem make sure the Ollama service is up before pulling
powershell -NoProfile -Command "try{Invoke-WebRequest -UseBasicParsing http://127.0.0.1:11434/api/tags -TimeoutSec 2 | Out-Null}catch{Start-Process ollama -ArgumentList 'serve' -WindowStyle Hidden; Start-Sleep 4}" >nul 2>nul
set RAM=16
for /f %%R in ('powershell -NoProfile -Command "[math]::Floor((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB)"') do set RAM=%%R
set "MODEL=gemma4:e4b"
if %RAM% LSS 16 set "MODEL=qwen3-vl:2b-instruct"
if %RAM% LSS 8 set "MODEL=gemma3:1b"
echo  [..] %RAM% GB RAM detected - downloading model %MODEL% ^(skipped if you already have it^)
ollama pull %MODEL%
".venv\Scripts\python.exe" -c "import re,pathlib;p=pathlib.Path('config.yaml');t=p.read_text(encoding='utf-8');p.write_text(re.sub(r'(?m)^(  model: )\S+',r'\g<1>%MODEL%',t,count=1),encoding='utf-8')"
echo  [ok] Model ready and set in config.yaml
echo  [..] Downloading tiny "eyes" models for screens and video ^(SmolVLM2 256M + 500M, under 1 GB^)
ollama pull hf.co/ggml-org/SmolVLM2-256M-Video-Instruct-GGUF:Q8_0
ollama pull hf.co/ggml-org/SmolVLM2-500M-Video-Instruct-GGUF:Q8_0
echo  [..] Downloading System 1 decision model ^(46 MB^)
ollama pull all-minilm:22m
echo  [..] Installing Laya - free open-source System One decision model ^(~1 GB incl. PyTorch, one time^)
".venv\Scripts\python.exe" -c "import laya" >nul 2>nul || ".venv\Scripts\python.exe" -m pip install -q "laya[serve]"
".venv\Scripts\python.exe" -c "import laya; laya.load('convaiinnovations/laya', subfolder='typed-decisions'); print('  [ok] Laya ready (works offline from now on)')" || echo  [!] Laya could not be set up - NestCrew will use MiniLM instead. You can retry from Models.
echo  [ok] More models: open NestCrew ^> Models tab

rem ---- desktop + start menu shortcut ----
powershell -NoProfile -Command ^
 "$s=(New-Object -ComObject WScript.Shell);" ^
 "foreach($d in @([Environment]::GetFolderPath('Desktop'),[Environment]::GetFolderPath('Programs'))){" ^
 "$l=$s.CreateShortcut((Join-Path $d 'NestCrew.lnk'));$l.TargetPath='%~dp0NestCrew.bat';" ^
 "$l.WorkingDirectory='%~dp0';$l.WindowStyle=7;$l.IconLocation='%SystemRoot%\System32\imageres.dll,109';$l.Description='NestCrew - local AI agents';$l.Save()}"
powershell -NoProfile -Command "Remove-Item -LiteralPath (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Start NestCrew.bat') -ErrorAction SilentlyContinue"
echo  [ok] Shortcut added to your Desktop and Start menu

".venv\Scripts\nestcrew.exe" doctor
echo.
if not exist data mkdir data
echo ok> data\installed.ok
echo  All done! Open "NestCrew" from your Desktop.
pause
