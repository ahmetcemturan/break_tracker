@echo off
title Pausen-Tracker
echo ============================================
echo   🔧 Pausen-Tracker - Wird gestartet...
echo ============================================
echo.

REM Python mit installiertem Streamlit finden (Microsoft Store Python bevorzugen)
set PYTHON_CMD=python
where python 2>nul | findstr /V /C:"msys64" /C:"Inkscape" >nul
if %ERRORLEVEL% EQU 0 (
    for /f "usebackq delims=" %%i in (`where python 2^>nul ^| findstr /V /C:"msys64" /C:"Inkscape"`) do (
        "%%i" -c "import streamlit" 2>nul
        if not errorlevel 1 (
            set PYTHON_CMD="%%i"
            goto :found
        )
    )
)

REM Fallback: WindowsApps-Pfad prüfen (Microsoft Store Python)
set "WINAPPS=%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe"
if exist "%WINAPPS%" (
    "%WINAPPS%" -c "import streamlit" 2>nul
    if not errorlevel 1 (
        set PYTHON_CMD="%WINAPPS%"
        goto :found
    )
)

REM Fallback: Pythoncore-Pfad prüfen
set "PYCOREDIR=%LOCALAPPDATA%\Python"
if exist "%PYCOREDIR%" (
    for /r "%PYCOREDIR%" %%f in (python.exe) do (
        "%%f" -c "import streamlit" 2>nul
        if not errorlevel 1 (
            set PYTHON_CMD="%%f"
            goto :found
        )
    )
)

:found
echo [OK] Verwende: %PYTHON_CMD%
%PYTHON_CMD% --version
echo.
echo [OK] Server wird gestartet unter http://localhost:8501
echo.
%PYTHON_CMD% -m streamlit run app.py
if %ERRORLEVEL% NEQ 0 (
    echo [FEHLER] Streamlit wurde mit Fehlercode %ERRORLEVEL% beendet.
    pause
)

pause
