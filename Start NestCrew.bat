@echo off
rem Desktop launcher for NestCrew (replaced by a proper shortcut after install)
cd /d "%USERPROFILE%\Documents\NestCrew"
if not exist "data\installed.ok" (
  start "NestCrew installer" "Install NestCrew.bat"
  exit /b
)
start "NestCrew" /min NestCrew.bat
