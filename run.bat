@echo off
setlocal enabledelayedexpansion
title Ra Launcher
chcp 65001 >nul
cd /d "%~dp0"

set "PY="
python --version >nul 2>&1 && set "PY=python"
if not defined PY ( py -3 --version >nul 2>&1 && set "PY=py -3" )
if not defined PY (
  echo Python was not found.
  echo Install it from https://www.python.org/downloads/  - tick "Add to PATH" and run this again.
  pause
  exit /b 1
)

if "%~1" neq "" set "C=%~1"
if "%~1" == "" goto menu

:route
if "%C%"=="1" goto start
if "%C%"=="2" goto ask
if "%C%"=="3" goto demo
if "%C%"=="4" goto tests
if "%C%"=="5" goto install
if "%C%"=="6" goto buildexe
if "%C%"=="7" exit /b 0
echo Unknown option "%C%".
pause
exit /b 1

:menu
cls
echo.
echo  ==========================================
echo        Ra  -  Launcher
echo  ==========================================
echo.
echo   [1] Start Ra   (voice + HUD window)
echo   [2] Ask Ra a question   (text)
echo   [3] Quick demo   (index docs, then ask)
echo   [4] Run all tests
echo   [5] Install / repair dependencies
echo   [6] Build the .exe
echo   [7] Exit
echo.
set "C="
set /p "C=Choose 1-7: "
if not defined C exit /b 0
goto route

:start
if exist "%~dp0hud\dist\Ra.exe" (
  echo Starting Ra  (packaged app - voice + HUD window)...
  echo Wake word: "ra".  Close the window to quit.
  start "" "%~dp0hud\dist\Ra.exe"
  goto menu
)
call :check_deps
if errorlevel 2 (
  echo.
  echo Core packages ^(numpy, sounddevice^) are missing - Ra cannot start without them.
  set /p "OK=Install them now?  [Y/n]: "
  if /i "!OK!"=="n" goto menu
  call :install_deps
  if errorlevel 1 ( echo. & echo Install failed. Open a console here and run:  pip install -r requirements.txt & pause & goto menu )
)
call :check_deps
if errorlevel 1 (
  echo.
  echo Voice packages ^(edge-tts, faster-whisper, etc.^) are not installed.
  set /p "OK=Install them now?  [Y/n - n = open in text-only mode]: "
  if /i "!OK!" neq "n" (
    call :install_deps
    if errorlevel 1 ( echo. & echo Install failed, continuing in text-only mode. & set "TXTONLY=1" )
  ) else set "TXTONLY=1"
)
echo.
if "!TXTONLY!"=="1" (
  echo Starting Ra in TEXT-ONLY mode ^(no mic / no speaker^). Type below and press Enter.
  set "RA_CONTINUOUS_LISTEN=0"
) else (
  echo Starting Ra...  Wake word: "ra".  Close the window to quit.
)
echo.
"%PY%" run_app.py
echo.
echo Ra closed.
pause
goto menu

:ask
set "QUERY="
set /p "QUERY=Your question: "
if not defined QUERY goto menu
echo.
"%PY%" ra_cli.py ask "%QUERY%"
echo.
pause
goto menu

:demo
set "DOCS="
if exist "%~dp0docs" set "DOCS=%~dp0docs"
if not defined DOCS set /p "DOCS=Folder to index: "
echo.
echo Indexing %DOCS% ...
"%PY%" ra_cli.py rag index --dir "%DOCS%"
"%PY%" ra_cli.py rag stats
echo.
echo Now asking Ra:  what does this project do?
echo.
"%PY%" ra_cli.py ask "what does this project do?"
echo.
pause
goto menu

:tests
echo.
echo == Ra tests ==
"%PY%" -m pytest
if errorlevel 1 set "BAD=1"
echo.
if defined BAD (
  echo Some tests FAILED - see messages above.
) else (
  echo All test suites PASSED.
)
pause
goto menu

:install
call :install_deps
if errorlevel 1 ( echo. & echo Install failed - open a console here and run:  pip install -r requirements.txt )
pause
goto menu

:buildexe
call build_exe.bat
pause
goto menu

:install_deps
if not exist requirements.txt ( echo requirements.txt not found. & exit /b 1 )
"%PY%" -m pip install --upgrade pip >nul 2>&1
"%PY%" -m pip install -r requirements.txt
exit /b %errorlevel%

:check_deps
"%PY%" -c "import numpy, sounddevice" >nul 2>&1 || exit /b 2
"%PY%" -c "import edge_tts, playsound3, faster_whisper" >nul 2>&1 || exit /b 1
exit /b 0