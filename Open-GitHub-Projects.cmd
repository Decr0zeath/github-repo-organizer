@echo off
setlocal
cd /d "%~dp0"
python -c "import sys; sys.exit(sys.version_info < (3, 7))" >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_COMMAND=python"
    goto run_server
)
py -3 -c "import sys; sys.exit(sys.version_info < (3, 7))" >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_COMMAND=py -3"
    goto run_server
)
echo Python 3.7 or newer is required (https://www.python.org/downloads/).
echo The GitHub CLI must be installed and signed in with gh auth login.
pause
exit /b 1

:run_server
%PYTHON_COMMAND% serve.py --open
set "SERVER_EXIT_CODE=%errorlevel%"
if not "%SERVER_EXIT_CODE%"=="0" pause
exit /b %SERVER_EXIT_CODE%
