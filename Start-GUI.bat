@echo off
REM Double-click to start the NISAR vs Sentinel-1 app (Windows). First run installs everything (a few minutes).
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Setting up Python environment, please wait...
  py -3 -m venv .venv || python -m venv .venv
  .venv\Scripts\python.exe -m pip install --upgrade pip
  .venv\Scripts\python.exe -m pip install -e ".[app]"
)
.venv\Scripts\sarcompare.exe gui
pause
