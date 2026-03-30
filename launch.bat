@echo off
title Spotify Kiosk
cd /d "%~dp0"

setlocal enabledelayedexpansion

REM Defaults
set "ENV_FILE=.env"
set "HOST=127.0.0.1"
set "PORT=8080"

REM If a .env file exists at repo root, parse HOST and PORT
if exist "%ENV_FILE%" (
	for /f "usebackq tokens=1* delims==" %%A in (`type "%ENV_FILE%" ^| findstr /R /V "^[ ]*#" ^| findstr /R /V "^[ ]*$"`) do (
		set "key=%%A"
		set "val=%%B"
		REM Trim leading spaces from key
		for /f "tokens=* delims= " %%K in ("!key!") do set "key=%%K"
		REM Trim leading spaces from val
		for /f "tokens=* delims= " %%V in ("!val!") do set "val=%%V"
		REM Remove surrounding double quotes from val if present
		set "val=!val:"=!"
		if /I "!key!"=="HOST" set "HOST=!val!"
		if /I "!key!"=="PORT" set "PORT=!val!"
	)
)

echo Using HOST=%HOST% PORT=%PORT%

echo Lancement du serveur Flask...
start /B python app.py

echo Attente que Flask soit pret...
set /A ATTEMPTS=0
:wait
timeout /t 1 /nobreak > nul
curl -s http://%HOST%:%PORT% > nul 2>&1
if errorlevel 1 (
	set /A ATTEMPTS+=1
	if %ATTEMPTS% GEQ 20 (
		echo Erreur: le serveur ne repond pas apres %ATTEMPTS% tentatives. Abandon.
		endlocal
		exit /b 1
	)
	goto wait
)

echo Flask est pret ! Lancement Edge...
start "" "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --kiosk http://%HOST%:%PORT% --edge-kiosk-type=fullscreen --no-first-run
endlocal
exit /b 0