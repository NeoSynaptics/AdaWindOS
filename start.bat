@echo off
REM AdaWindOS startup script for Windows
REM Usage:
REM   start.bat          - text mode (terminal chat)
REM   start.bat --ui     - web UI mode (browser chat)

cd /d "%~dp0"

REM Load .env if exists
if exist .env (
    for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
        set "%%a=%%b"
    )
)

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [!!] Python not found. Install Python 3.11+ from python.org
    pause
    exit /b 1
)

REM Check DEEPSEEK_API_KEY
if "%DEEPSEEK_API_KEY%"=="" (
    echo [!!] DEEPSEEK_API_KEY not set.
    echo     Create a .env file with: DEEPSEEK_API_KEY=your_key_here
    echo     Or set it: set DEEPSEEK_API_KEY=your_key_here
    pause
    exit /b 1
)

echo === AdaWindOS ===
echo [OK] Python found
echo [OK] DeepSeek API key configured

REM Run Ada
if "%1"=="--ui" (
    echo Starting Ada with UI on http://localhost:8765 ...
    python -m ada.main --ui
) else (
    echo Starting Ada (text mode^)...
    python -m ada.main
)
