@echo off
REM Start Chrome with remote debugging enabled (Windows).
REM Usage: start_chrome.bat [port]

set "PORT=9222"
if not "%~1"=="" set "PORT=%~1"

set "CHROME="
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
if exist "C:\Program Files\Chromium\Application\chrome.exe" set "CHROME=C:\Program Files\Chromium\Application\chrome.exe"

if "%CHROME%"=="" (
    echo Error: Chrome not found. Install Chrome or set CHROME path.
    exit /b 1
)

set "USER_DATA_DIR=%LOCALAPPDATA%\chrome-debug-profile"
echo Starting Chrome with remote debugging on port %PORT%...
echo.
echo Steps:
echo   1. Log into Facebook in the opened Chrome.
echo   2. Go to your private group page.
echo   3. In another terminal:
echo      uv:   uv run python collector.py --port %PORT%
echo      venv: .venv\Scripts\activate ^&^& python collector.py --port %PORT%
echo.

"%CHROME%" --remote-debugging-port=%PORT% --user-data-dir="%USER_DATA_DIR%" --no-first-run --no-default-browser-check "https://www.facebook.com"
