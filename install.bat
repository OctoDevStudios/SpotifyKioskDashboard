@echo off
title Spotify Kiosk - Installation
cd /d "%~dp0"

echo === Installation du Spotify Kiosk (Windows) ===

echo Installation des dependances (system-wide) via pip...

REM Prefer using python -m pip if python is available
where python >nul 2>&1
if %ERRORLEVEL% == 0 (
    echo Utilisation de python -m pip
    python -m pip install --upgrade pip
    python -m pip install -r requirements.txt
    if %ERRORLEVEL% NEQ 0 (
        echo Erreur lors de l'installation via python. Tentative via pip...
        pip install -r requirements.txt
    )
) else (
    echo Python introuvable dans le PATH, tentative via pip directly...
    pip install -r requirements.txt
    if %ERRORLEVEL% NEQ 0 (
        echo Erreur: ni python ni pip n'ont permis d'installer les dependances. Installez Python et re-essayez.
        pause
        exit /b 1
    )
)

echo.
echo Installation terminee. Les paquets sont installes globalement.
echo Lancez le kiosk avec `launch.bat` ou `python app.py`.
echo.
pause
