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

echo === AdaWindOS Preflight ===

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [!!] Python not found. Install Python 3.11+ from python.org
    pause
    exit /b 1
)
echo [OK] Python found

REM Check DEEPSEEK_API_KEY
if "%DEEPSEEK_API_KEY%"=="" (
    echo [!!] DEEPSEEK_API_KEY not set.
    echo     Create a .env file with: DEEPSEEK_API_KEY=your_key_here
    pause
    exit /b 1
)
echo [OK] DeepSeek API key configured

REM Check Docker + PostgreSQL
docker ps >nul 2>&1
if errorlevel 1 (
    echo [!!] Docker not running. Start Docker Desktop first.
    pause
    exit /b 1
)

docker ps --format "{{.Names}}" | findstr /i postgres >nul 2>&1
if errorlevel 1 (
    echo [>>] Starting PostgreSQL via Docker...
    docker compose up -d
    timeout /t 3 /nobreak >nul
    echo [OK] PostgreSQL started
) else (
    echo [OK] PostgreSQL running
)

echo ===========================
echo.

REM Run Ada
if "%1"=="--ui" (
    echo Starting Ada with UI on http://localhost:8765 ...
    python -m ada.main --ui
) else (
    echo Starting Ada (text mode^)...
    python -m ada.main
)
