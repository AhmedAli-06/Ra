@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Ra .exe builder

set "PY="
python --version >nul 2>&1 && set "PY=python"
if not defined PY ( py -3 --version >nul 2>&1 && set "PY=py -3" )
if not defined PY (
  echo Python not found - install it from https://www.python.org/downloads/  first.
  pause
  exit /b 1
)

"%PY%" -m pip show pyinstaller >nul 2>&1 || (
  echo Installing PyInstaller...
  "%PY%" -m pip install pyinstaller || ( echo PyInstaller install failed. & pause & exit /b 1 )
)

echo.
echo This copies your API key from .env into ~\.ra so the built exe can use it.
echo The key is NOT embedded in the exe - it is read from ~\.ra\.env at runtime.
if exist .env (
  copy /y .env "%USERPROFILE%\.ra\.env" >nul
  echo Copied .env -^* %USERPROFILE%\.ra\.env
) else (
  copy /y .env.example "%USERPROFILE%\.ra\.env" >nul
  echo Copied .env.example -^* %USERPROFILE%\.ra\.env
  echo   IMPORTANT: open that file and add your API key, then rebuild.
)

echo.
echo Building Ra.exe  (takes a few minutes)...
cd hud
"%PY%" -m PyInstaller --noconfirm --clean ra.spec
if errorlevel 1 (
  echo.
  echo BUILD FAILED - see the messages above.
  pause
  exit /b 1
)
cd ..
echo.
echo Done:  hud\dist\Ra.exe
echo   - Double-click to launch the assistant.
echo   - Debug output goes to ~\.ra\ra.log
pause