@echo off
setlocal

rem Starts the MeterGate local stack in separate PowerShell windows.
rem Run from any location by double-clicking this file or running: .\start-all.bat

set "ROOT_DIR=%~dp0"
set "API_DIR=%ROOT_DIR%apps\api"
set "ORBITINTEL_DIR=%ROOT_DIR%apps\orbitintel"
set "WEB_DIR=%ROOT_DIR%apps\web"

if not exist "%ROOT_DIR%.env" (
  echo Missing %ROOT_DIR%.env
  echo Copy .env.example to .env and configure it before starting the stack.
  exit /b 1
)

where docker >nul 2>nul || goto :missing_docker
where uv >nul 2>nul || goto :missing_uv
where npm >nul 2>nul || goto :missing_npm
where zrok2 >nul 2>nul || goto :missing_zrok2

pushd "%ROOT_DIR%"
echo Starting PostgreSQL and Redis...
docker compose up -d --wait
if errorlevel 1 goto :setup_failed

echo Preparing MeterGate dependencies and database...
pushd "%API_DIR%"
uv sync --frozen --dev
if errorlevel 1 goto :setup_failed
uv run alembic upgrade head
if errorlevel 1 goto :setup_failed
uv run python -m app.scripts.seed_dev
if errorlevel 1 goto :setup_failed
uv run python -m app.scripts.backfill_entitlement_outbox
if errorlevel 1 goto :setup_failed
popd

echo Preparing OrbitIntel dependencies...
pushd "%ORBITINTEL_DIR%"
uv sync --frozen --all-groups
if errorlevel 1 goto :setup_failed
popd

echo Preparing Next.js dependencies...
pushd "%WEB_DIR%"
npm install
if errorlevel 1 goto :setup_failed
popd

echo Opening service windows...
start "MeterGate API" powershell.exe -NoExit -NoProfile -Command "Set-Location -LiteralPath '%API_DIR%'; uv run fastapi dev app/main.py"
start "MeterGate Webhook Worker" powershell.exe -NoExit -NoProfile -Command "Set-Location -LiteralPath '%API_DIR%'; uv run python -m app.workers.razorpay_webhooks"
start "MeterGate Entitlement Worker" powershell.exe -NoExit -NoProfile -Command "Set-Location -LiteralPath '%API_DIR%'; uv run python -m app.workers.entitlements"
start "MeterGate Refund Worker" powershell.exe -NoExit -NoProfile -Command "Set-Location -LiteralPath '%API_DIR%'; uv run python -m app.workers.refunds"
start "OrbitIntel Merchant" powershell.exe -NoExit -NoProfile -Command "Set-Location -LiteralPath '%ORBITINTEL_DIR%'; uv run uvicorn app.main:app --host 127.0.0.1 --port 8100"
start "MeterGate Web" powershell.exe -NoExit -NoProfile -Command "Set-Location -LiteralPath '%WEB_DIR%'; $env:NEXT_PUBLIC_API_URL = 'http://localhost:8000'; npm run dev"
start "MeterGate zrok2" powershell.exe -NoExit -NoProfile -Command "Set-Location -LiteralPath '%ROOT_DIR%'; zrok2 share public localhost:8000 --headless"

popd
echo.
echo MeterGate services were launched in separate PowerShell windows.
echo Web: http://localhost:3000
echo API: http://localhost:8000/docs
exit /b 0

:missing_docker
echo Required command not found: docker
exit /b 1

:missing_uv
echo Required command not found: uv
exit /b 1

:missing_npm
echo Required command not found: npm
exit /b 1

:missing_zrok2
echo Required command not found: zrok2
exit /b 1

:setup_failed
echo Setup failed. No service windows were started.
popd
exit /b 1
