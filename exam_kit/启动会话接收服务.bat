@echo off
rem 会话接收服务窗口（考场局域网交卷分流）：双击启动后保持窗口开着，关窗即停。
rem 考试机 config.json 里 upload_mode 改为 "lan"、lan_receiver_base 填本机局域网地址。
setlocal EnableExtensions
title Alethic Insight 机试会话接收服务
cd /d "%~dp0"
set "PYEXE="
set "PYARGS="
py -3.12 -c "print('ok')" >/dev/null 2>/dev/null && (set "PYEXE=py" & set "PYARGS=-3.12")
if not defined PYEXE (
    python -c "print('ok')" >/dev/null 2>/dev/null && set "PYEXE=python"
)
if not defined PYEXE (
    if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)
if not defined PYEXE (
    echo.
    echo [失败] 没有找到 Python，请联系组织者安装后再启动。
    echo.
    pause
    exit /b 1
)
echo 正在启动会话接收服务，请保持本窗口开着（考场期间勿关闭）。
echo 接收列表页：http://localhost:8897/  （考试机 config.json 的 lan_receiver_base 填本机局域网 IP:8897）
echo.
"%PYEXE%" %PYARGS% "%~dp0session_receiver.py" --port 8897
echo.
echo 会话接收服务已停止。如非有意关闭，请重新双击启动并检查组织者。
pause >/dev/null
