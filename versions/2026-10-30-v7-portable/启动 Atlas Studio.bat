@echo off
chcp 65001 >nul 2>&1
title Atlas Studio
cd /d "%~dp0"
rem ===============================================================
rem  Atlas Studio launcher
rem  - finds a working Python automatically (any drive / any folder)
rem  - locates ComfyUI by scanning all drives
rem  - reads host+port from config.json
rem  - opens the correct URL
rem  Works after copying this folder to another PC.
rem ===============================================================

set "HERE=%~dp0"
set "PY="
set "PORT=8125"
set "HOST=127.0.0.1"

rem ---- read port / host from config.json ----
if exist "%HERE%config.json" (
  for /f "tokens=2 delims=:,}" %%A in ('findstr /i "\"port\"" "%HERE%config.json" 2^>nul') do set "PORT=%%~A"
  for /f "tokens=2 delims=:,}" %%A in ('findstr /i "\"host\"" "%HERE%config.json" 2^>nul') do set "HOST=%%~A"
)
rem %%~A already strips quotes; just drop stray spaces
set "PORT=%PORT: =%"
set "HOST=%HOST: =%"
set "PORT=%PORT:"=%"
set "HOST=%HOST:"=%"
if "%PORT%"=="" set "PORT=8125"
if "%HOST%"=="" set "HOST=127.0.0.1"

echo.
echo   Atlas Studio - Pose + Costume drag-and-drop generator
echo   ----------------------------------------------------
echo   Port %PORT%   Host %HOST%
echo.

rem ===============================================================
rem  Step 1 - find a Python interpreter
rem ===============================================================
call :find_python
if not defined PY (
  echo   [X] No usable Python found.
  echo.
  echo       Fix it with one of these:
  echo        1^) Install ComfyUI portable version - it ships with its own Python
  echo        2^) Set an environment variable, then run this .bat again:
  echo             set ATLAS_PYTHON=D:\ComfyUI\python_embeded\python.exe
  echo.
  pause
  exit /b 1
)
echo   Python  : %PY%

rem ===============================================================
rem  Step 2 - locate ComfyUI (scans every drive)
rem ===============================================================
echo.
echo   Locating ComfyUI ...
"%PY%" "%HERE%paths.py" "%HERE%" > "%TEMP%\atlas_paths.txt" 2>&1
findstr /v "^ComfyUI" "%TEMP%\atlas_paths.txt"
for /f "tokens=2 delims==" %%A in ('findstr /b /i "comfy_root=" "%TEMP%\atlas_paths.txt"') do set "COMFY_ROOT=%%A"
if not defined COMFY_ROOT set "COMFY_ROOT="

if not defined COMFY_ROOT (
  echo.
  echo   [!] ComfyUI was not found on this computer.
  echo.
  echo       Set an environment variable, then run this .bat again:
  echo         set ATLAS_COMFY_ROOT=D:\ComfyUI_windows_portable
  echo.
  echo       That is the folder containing ComfyUI and python_embeded.
  echo.
  pause
  exit /b 1
)
echo   ComfyUI  : %COMFY_ROOT%

rem ---- is ComfyUI server up? ----
echo.
echo   Checking ComfyUI on 127.0.0.1:8188 ...
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 4 'http://127.0.0.1:8188/system_stats' -ErrorAction Stop | Out-Null; Write-Host '   ComfyUI : ONLINE' } catch { Write-Host '   ComfyUI : NOT RUNNING - start ComfyUI first' }" 2>nul

rem ===============================================================
rem  Step 3 - start the Studio server
rem ===============================================================
if not exist "%HERE%studio_server.py" (
  echo   [X] studio_server.py not found next to this .bat
  pause
  exit /b 1
)

netstat -ano | findstr ":%PORT% " | findstr "LISTENING" >nul
if not errorlevel 1 goto open

echo.
echo   Starting server ...
rem 用 pushd 切到脚本目录再起进程，避免带空格路径的引号问题；
rem 服务窗口要留着（关掉就停服务），所以用 cmd /k。
pushd "%HERE%"
start "Atlas Server" cmd /k ""%PY%" -u "%HERE%studio_server.py""
popd

set /a n=0
:wait
set /a n+=1
netstat -ano | findstr ":%PORT% " | findstr "LISTENING" >nul
if not errorlevel 1 goto open
if %n% geq 60 goto failed
ping -n 2 127.0.0.1 >nul
goto wait

:open
ping -n 2 127.0.0.1 >nul
echo.
echo   UI  : http://127.0.0.1:%PORT%/
if /i "%HOST%"=="0.0.0.0" (
  echo.
  echo   Other devices on the same network can open:
  for /f "tokens=2 delims=:" %%A in ('ipconfig ^| findstr /c:"IPv4"') do (
    set "LANIP=%%A"
    goto :gotip
  )
  :gotip
  set "LANIP=%LANIP: =%"
  if defined LANIP echo       http://!LANIP!:%PORT%/
)
echo.
start "" "http://127.0.0.1:%PORT%/"
echo   Keep the "Atlas Server" window open - closing it stops the server.
echo.
ping -n 4 127.0.0.1 >nul
exit /b 0

:failed
echo.
echo   [X] Server did not start within 60 seconds.
echo       Check the "Atlas Server" window for the error message.
pause
exit /b 1


rem ===============================================================
rem  Python discovery, in order:
rem    1) ATLAS_PYTHON environment variable
rem    2) config.json  "python" field
rem    3) ComfyUI portable builds on any drive  (ComfyUI*\python_embeded)
rem    4) system python / py launcher on PATH
rem ===============================================================
:find_python
if defined ATLAS_PYTHON (
  if exist "%ATLAS_PYTHON%" (
    set "PY=%ATLAS_PYTHON%"
    goto :eof
  )
)

if exist "%HERE%config.json" (
  for /f "tokens=2 delims=:,}" %%A in ('findstr /i "\"python\"" "%HERE%config.json" 2^>nul') do (
    set "CFGPY=%%~A"
    goto :trycfgpy
  )
  :trycfgpy
  set "CFGPY=%CFGPY: =%"
  set "CFGPY=%CFGPY:"=%"
  if defined CFGPY if exist "%CFGPY%" (
    set "PY=%CFGPY%"
    goto :eof
  )
)

for %%D in (C D E F G H) do (
  for /f "delims=" %%P in ('dir /b /ad "%%D:\ComfyUI*" 2^>nul') do (
    if exist "%%D:\%%P\python_embeded\python.exe" (
      set "PY=%%D:\%%P\python_embeded\python.exe"
      goto :foundpy
    )
  )
)
:foundpy

if not defined PY (
  for /f "delims=" %%P in ('where python 2^>nul') do (
    if not defined PY set "PY=%%P"
  )
)
if not defined PY (
  for /f "delims=" %%P in ('where py 2^>nul') do (
    if not defined PY set "PY=%%P"
  )
)
goto :eof
