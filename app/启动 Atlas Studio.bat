@echo off
title Atlas Studio
cd /d "%~dp0"
rem ---- change this if ComfyUI lives somewhere else ----
set PY=E:\ComfyUI_Mie_V33\python_embeded\python.exe

echo.
echo   Atlas Studio - Pose + Costume drag-and-drop generator
echo   ----------------------------------------------------
echo   UI   : http://127.0.0.1:8125/
echo   Need : ComfyUI running on 127.0.0.1:8188
echo.

if not exist "%PY%" goto nopy
if not exist "studio_server.py" goto noserver

netstat -ano | findstr ":8125 " | findstr "LISTENING" >nul
if not errorlevel 1 goto open

echo   Starting server...
start "Atlas Server" cmd /k %PY% -u studio_server.py

set /a n=0
:wait
set /a n+=1
netstat -ano | findstr ":8125 " | findstr "LISTENING" >nul
if not errorlevel 1 goto open
if %n% geq 45 goto failed
ping -n 2 127.0.0.1 >nul
goto wait

:open
ping -n 2 127.0.0.1 >nul
start "" "http://127.0.0.1:8125/"
echo   Opened http://127.0.0.1:8125/
echo   Keep the "Atlas Server" window open - closing it stops the server.
ping -n 4 127.0.0.1 >nul
exit /b 0

:failed
echo.
echo   [X] Server did not start. Check the "Atlas Server" window for the error.
pause
exit /b 1

:nopy
echo   [X] python not found: %PY%
echo       Edit this bat and point PY to your ComfyUI python_embeded folder.
pause
exit /b 1

:noserver
echo   [X] studio_server.py not found in this folder.
pause
exit /b 1
