@echo off
setlocal

cd /d "%~dp0"

echo ========================================================
echo  Building wmm-menu.exe with PyInstaller
echo ========================================================

python --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Python is not installed or not in PATH.
    pause
    exit /b 1
)

python -c "import yaml, customtkinter, PIL, PyInstaller" >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo Installing required packages from requirements.txt...
    python -m pip install -r requirements.txt
    if %ERRORLEVEL% NEQ 0 (
        echo ERROR: Failed to install dependencies.
        pause
        exit /b 1
    )
)

echo Running PyInstaller...
set ICON_ARGS=
if exist "app.ico" (
    set ICON_ARGS=--icon app.ico --add-data "app.ico;."
)
python -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onefile ^
    --windowed ^
    --collect-data customtkinter ^
    %ICON_ARGS% ^
    --name wmm-menu ^
    wmm-menu.py

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ERROR: Build failed.
    pause
    exit /b 1
)

echo.
echo ========================================================
echo  Build succeeded!
echo  Executable: %~dp0dist\wmm-menu.exe
echo ========================================================
endlocal
