@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
if exist "%~dp0backend\.venv\Scripts\python.exe" (
  "%~dp0backend\.venv\Scripts\python.exe" -c "import sys;sys.exit(sys.version_info[:2] < (3,11))" >nul 2>nul
  if not errorlevel 1 goto use_venv
)
py -3 -c "import sys;sys.exit(sys.version_info[:2] < (3,11))" >nul 2>nul
if not errorlevel 1 goto use_py
python -c "import sys;sys.exit(sys.version_info[:2] < (3,11))" >nul 2>nul
if not errorlevel 1 goto use_python
echo BeamWeft 需要 Python 3.11 或更高版本。请从 https://www.python.org/downloads/ 安装并勾选 Add python.exe to PATH，安装后重新打开此文件。
goto failed

:use_venv
"%~dp0backend\.venv\Scripts\python.exe" "%~dp0backend\scripts\desktop_launcher.py" %*
goto finished

:use_py
py -3 "%~dp0backend\scripts\desktop_launcher.py" %*
goto finished

:use_python
python "%~dp0backend\scripts\desktop_launcher.py" %*

:finished
if not errorlevel 1 exit /b 0
:failed
pause
exit /b 1
