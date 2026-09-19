@echo off
rem 机试服务窗口（由「启动考试系统.bat」启动）。窗口常驻：关闭本窗口即停止服务；
rem 出错时打印报错并停在提示符（不闪退）。纯 cmd，禁 chcp，命令 token 全 ASCII。
setlocal EnableExtensions
title Alethic Insight 招新机试服务
cd /d "%~dp0"
set "PYEXE="
set "PYARGS="
py -3.12 -c "print('ok')" >nul 2>nul && (set "PYEXE=py" & set "PYARGS=-3.12")
if not defined PYEXE (
    python -c "print('ok')" >nul 2>nul && set "PYEXE=python"
)
if not defined PYEXE (
    if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)
if not defined PYEXE (
    echo.
    echo [失败] 没有找到 Python，请先运行安装程序完成安装，或联系组织者。
    echo.
    pause
    exit /b 1
)
echo 启动考试系统服务，请保持本窗口开着；考试结束时关闭本窗口即停止服务。
echo.
"%PYEXE%" %PYARGS% "%~dp0exam_server.py"
echo.
echo 【考试系统已停止】按任意键关闭本窗口。
pause >nul
