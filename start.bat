@echo off
title Break Tracker
echo ============================================
echo   🔧 Break Tracker - Starting...
echo ============================================
echo.

REM Locate Python with streamlit installed (prefer Microsoft Store Python)
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

REM Fallback: check WindowsApps path (Microsoft Store Python)
set "WINAPPS=%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe"
if exist "%WINAPPS%" (
    "%WINAPPS%" -c "import streamlit" 2>nul
    if not errorlevel 1 (
        set PYTHON_CMD="%WINAPPS%"
        goto :found
    )
)

REM Fallback: check Pythoncore path
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
echo [OK] Using: %PYTHON_CMD%
%PYTHON_CMD% --version
echo.
echo [OK] Starting server at http://localhost:8501
echo.
%PYTHON_CMD% -m streamlit run app.py
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Streamlit exited with error code %ERRORLEVEL%.
    pause
)

pause
