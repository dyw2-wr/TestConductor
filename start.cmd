@echo off
setlocal EnableExtensions
chcp 65001 >nul

rem Always run from the directory containing this script.
cd /d "%~dp0" || goto :cd_error

rem Prefer a project-local virtual environment when one exists.
if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
) else if exist "venv\Scripts\python.exe" (
    set "PYTHON_EXE=%CD%\venv\Scripts\python.exe"
) else (
    set "PYTHON_EXE=python"
)

set "BIND_ADDRESS=%~1"
if not defined BIND_ADDRESS set "BIND_ADDRESS=127.0.0.1:8000"
set "BROWSER_ADDRESS=%BIND_ADDRESS:0.0.0.0=127.0.0.1%"
set "HEALTH_URL=http://%BROWSER_ADDRESS%/health/"
set "ADMIN_URL=http://%BROWSER_ADDRESS%/admin/"

echo [TestConductor] Python: %PYTHON_EXE%
"%PYTHON_EXE%" --version >nul 2>&1 || goto :python_error

echo [TestConductor] Checking Django configuration...
"%PYTHON_EXE%" manage.py check || goto :django_error

echo [TestConductor] Applying database migrations...
"%PYTHON_EXE%" manage.py migrate --noinput || goto :migration_error

rem If the service is already running, just open the management page.
powershell.exe -NoProfile -Command ^
    "try { $response = Invoke-WebRequest -UseBasicParsing -Uri '%HEALTH_URL%' -TimeoutSec 2; if ($response.StatusCode -eq 200) { exit 0 } } catch {}; exit 1" >nul 2>&1
if not errorlevel 1 (
    echo [TestConductor] Service is already running at http://%BROWSER_ADDRESS%/
    start "" "%ADMIN_URL%"
    exit /b 0
)

echo.
echo [TestConductor] Starting service at http://%BIND_ADDRESS%/
echo [TestConductor] The admin page will open after the health check succeeds.
echo [TestConductor] Press Ctrl+C to stop.
echo.

rem Wait for Django to become healthy before opening the browser.
start "" /b powershell.exe -NoProfile -WindowStyle Hidden -Command ^
    "$health='%HEALTH_URL%'; $admin='%ADMIN_URL%'; for ($i=0; $i -lt 60; $i++) { try { $response=Invoke-WebRequest -UseBasicParsing -Uri $health -TimeoutSec 1; if ($response.StatusCode -eq 200) { Start-Process $admin; exit 0 } } catch {}; Start-Sleep -Milliseconds 500 }; exit 1"

rem --insecure lets Django serve collected admin/project static assets for this
rem local launcher even when DJANGO_DEBUG=false. Production must use a real
rem static-file server instead of this script.
"%PYTHON_EXE%" manage.py runserver --insecure "%BIND_ADDRESS%"
if errorlevel 1 goto :server_error
exit /b 0

:cd_error
echo [TestConductor] Failed to enter the project directory.
goto :failed

:python_error
echo [TestConductor] Python was not found. Install Python or create .venv first.
goto :failed

:django_error
echo [TestConductor] Django configuration check failed. Review the message above.
goto :failed

:migration_error
echo [TestConductor] Database migration failed. Review the message above.
goto :failed

:server_error
echo [TestConductor] Django stopped unexpectedly. Review the message above.
goto :failed

:failed
echo.
pause
exit /b 1
