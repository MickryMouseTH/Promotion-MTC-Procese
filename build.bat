@echo off
REM Build a single-file executable for Promotion-MTC-Procese (Windows).
REM Usage: build.bat
setlocal

REM Always run from the script's own directory.
cd /d "%~dp0"

set "APP_NAME=Promotion-MTC-Procese"
set "ENTRY=Promotion-MTC-Procese.py"

echo ==^> Installing build dependencies...
python -m pip install --upgrade pip || goto :error
python -m pip install -r requirements.txt pyinstaller || goto :error

echo ==^> Cleaning previous build artifacts...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist "%APP_NAME%.spec" del /q "%APP_NAME%.spec"

echo ==^> Building single-file executable with PyInstaller...
python -m PyInstaller ^
    --onefile ^
    --clean ^
    --name "%APP_NAME%" ^
    --hidden-import pyodbc ^
    --collect-submodules cryptography ^
    "%ENTRY%" || goto :error

echo.
echo ==^> Done. Executable is at: dist\%APP_NAME%.exe
echo     Place config/key/logs next to the executable when you run it.
goto :eof

:error
echo.
echo Build failed. See the error messages above.
exit /b 1
