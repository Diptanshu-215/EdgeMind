@echo off
REM EdgeMind tests (PowerShell:  .\test.bat  ...)
REM   .\test.bat                 every feature, against a real isolated stack
REM   .\test.bat --list          list the features
REM   .\test.bat offline sync    only those features
REM   .\test.bat --fast          component tests only (no servers, ~30 s)
cd /d %~dp0
if not exist .venv (
  echo No .venv yet: run  .\run.bat  once first to install EdgeMind.
  exit /b 1
)
call .venv\Scripts\activate
python -c "import pytest" 2>nul || pip install -r requirements-dev.txt
python tests\run_tests.py %*
