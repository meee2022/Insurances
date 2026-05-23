@echo off
title Insurance Eligibility Checker - AL JAZEERAH HEALTH CENTER
cd /d "%~dp0"
echo.
echo ============================================
echo   Insurance Eligibility Checker
echo   AL JAZEERAH HEALTH CENTER
echo ============================================
echo.

if not exist "webapp.py" goto no_files

set "PYCMD=python"
python --version >nul 2>&1
if not errorlevel 1 goto have_python
set "PYCMD=py"
py --version >nul 2>&1
if not errorlevel 1 goto have_python
goto no_python

:have_python
echo Using Python command: %PYCMD%
%PYCMD% --version
echo.

set "PY=.venv\Scripts\python.exe"
if exist "%PY%" goto have_venv
echo First-time setup: creating Python environment, please wait...
%PYCMD% -m venv .venv
if not exist "%PY%" goto venv_failed
"%PY%" -m pip install --upgrade pip

:have_venv
"%PY%" -c "import fastapi, uvicorn, playwright, PIL, pytesseract" >nul 2>&1
if not errorlevel 1 goto have_deps
echo Installing dependencies, please wait. This needs internet.
"%PY%" -m pip install -r requirements.txt
if errorlevel 1 goto deps_failed

:have_deps
if exist "%LOCALAPPDATA%\ms-playwright" goto run_server
echo Installing browser, about 150 MB. This needs internet, please wait...
"%PY%" -m playwright install chromium

:run_server
echo.
echo Starting server. Opening http://localhost:8000 in your browser.
echo  *** KEEP THIS WINDOW OPEN while using the program ***
start "" "http://localhost:8000"
"%PY%" webapp.py
echo.
echo Server stopped.
pause
exit /b 0

:no_files
echo ERROR: webapp.py was not found.
echo Please EXTRACT the zip first, then run start.bat from inside the tamer folder.
echo.
pause
exit /b 1

:no_python
echo ERROR: Python was not found on this computer.
echo Install Python 3 from  https://www.python.org/downloads/
echo During install, tick the box  Add Python to PATH
echo Then run start.bat again.
echo.
pause
exit /b 1

:venv_failed
echo ERROR: Could not create the Python environment.
echo.
pause
exit /b 1

:deps_failed
echo ERROR: Could not install dependencies. Check your internet connection and try again.
echo.
pause
exit /b 1
