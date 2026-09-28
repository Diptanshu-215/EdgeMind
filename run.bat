@echo off
REM Windows: in PowerShell run  .\run.bat   (or double-click)
cd /d %~dp0
if not exist .venv (
  echo Creating virtual environment...
  python -m venv .venv || goto :nopython
  call .venv\Scripts\activate
  python -m pip install --upgrade pip
  pip install -r requirements.txt || goto :failed
) else (
  call .venv\Scripts\activate
)
python launch.py
goto :eof

:nopython
echo Python was not found. Install Python 3.10-3.12 from python.org and tick "Add python.exe to PATH".
pause
goto :eof

:failed
echo Installing packages failed. Delete the .venv folder and run again, or paste the error to your teammate.
pause
