@echo off
chcp 65001 >nul 2>&1
title Atlas Studio - Package
cd /d "%~dp0"
rem ===============================================================
rem  把工程打包成可以直接发给朋友的 zip
rem  结构:  Atlas-Studio\app, data, skill, 说明
rem ===============================================================

set "ROOT=%~dp0"
set "OUT=%ROOT%dist"

echo.
echo   Atlas Studio - package for sharing
echo   -----------------------------------
echo.

if not exist "%OUT%" mkdir "%OUT%"

set "STAGE=%OUT%\Atlas-Studio"
if exist "%STAGE%" rmdir /s /q "%STAGE%"
mkdir "%STAGE%"

echo   Copying app ...
rem robocopy: 0=无变化 1=有复制 2=多余文件 3=有错误; >=8 才是真失败
robocopy "%ROOT%app" "%STAGE%\app" /E /XD work __pycache__ /XF "*.pyc" /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 (
  echo   [X] copy app failed
  pause
  exit /b 1
)

echo   Copying data ...
robocopy "%ROOT%data" "%STAGE%\data" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 (
  echo   [X] copy data failed
  pause
  exit /b 1
)

echo   Copying skill ...
robocopy "%ROOT%skill" "%STAGE%\skill" /E /XD __pycache__ /XF "*.pyc" /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 (
  echo   [X] copy skill failed
  pause
  exit /b 1
)

copy /y "%ROOT%给朋友的使用说明.md" "%STAGE%使用说明.md" >nul 2>&1
copy /y "%ROOT%给朋友的使用说明.md" "%OUT%使用说明.md" >nul 2>&1

echo   Cleaning personal paths and making zip ...
rem 素材库有 2GB 多，用 Python 的 zipfile 流式打包（PowerShell 的 Compress-Archive 会卡住）
set "PACKPY="
if exist "%ComfyUI_Mie_V33%\python_embeded\python.exe" set "PACKPY=%ComfyUI_Mie_V33%\python_embeded\python.exe"
if not defined PACKPY for /f "delims=" %%P in ('where python 2^>nul') do if not defined PACKPY set "PACKPY=%%P"
if defined PACKPY (
  "%PACKPY%" "%ROOT%pack.py"
) else (
  echo   [!] No Python found for packaging. Folder is ready at:
  echo         %STAGE%
  pause
  exit /b 0
)

if exist "%OUT%\Atlas-Studio.zip" (
  echo.
  echo   [OK] Package created:
  echo         %OUT%\Atlas-Studio.zip
  echo.
  echo   Send that zip to your friend. They unzip it and double-click
  echo   Atlas-Studio\app\启动 Atlas Studio.bat
) else (
  echo.
  echo   [!] zip creation failed - the folder is still ready at:
  echo         %STAGE%
)
echo.
pause
exit /b 0
