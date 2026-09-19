# 生成 GBK 编码的 启动考试系统.bat（避免 UTF-8+chcp 65001 的 cmd 解析错位 bug）
# 命令 token 全 ASCII；中文仅出现在 title/echo 等展示性字符串；禁止 chcp。
# 读取 config 用 python 直接写 mbcs 编码临时 bat 再 call，WORKDIR 用 if not exist 兜底。
import sys

BAT = r'''@echo off
rem Alethic Insight exam kit launcher (pure cmd, no PowerShell).
rem IMPORTANT: keep this file ANSI/GBK encoded; keep command tokens ASCII; never add chcp.
setlocal EnableExtensions
title Alethic Insight 社团招新机试
cd /d "%~dp0"

set "CONFIG=%~dp0config.json"
if not exist "%CONFIG%" (
    echo.
    echo [失败] 缺少 config.json，请确认「招新考试系统」文件夹已完整解压，不要只解压部分文件。
    echo.
    pause
    exit /b 1
)

rem ---------- 1. locate python ----------
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
    echo [失败] 没有找到 Python。请先运行安装程序完成安装，或联系组织者。
    echo.
    pause
    exit /b 1
)

rem ---------- 2. make sure AlethicTools resolve even if user PATH is not refreshed yet ----------
if exist "%LOCALAPPDATA%\Programs\AlethicTools" set "PATH=%LOCALAPPDATA%\Programs\AlethicTools\opencode;%LOCALAPPDATA%\Programs\AlethicTools\node;%LOCALAPPDATA%\Programs\AlethicTools\git\cmd;%LOCALAPPDATA%\Programs\AlethicTools\kimi\node_modules\.bin;%PATH%"

rem ---------- 3. read config via python into a temp mbcs env bat, then call it ----------
set "AGENT=kimi"
set "WEBCMD=kimi web"
set "WORKDIR=%~dp0"
set "PORT=8899"
set "ENVBAT=%TEMP%\exam_env_%RANDOM%.bat"
"%PYEXE%" %PYARGS% -c "import json;c=json.load(open(r'%CONFIG%',encoding='utf-8'));a=c.get('agent','kimi');a=a if a in ('kimi','opencode') else 'kimi';w=c.get(a+'_web_cmd') or (a+' web');d=c.get('agent_workdir') or '';p=str(int(c.get('exam_port',8899)));q=chr(34);L=['set '+q+'AGENT='+a+q,'set '+q+'WEBCMD='+w+q,'set '+q+'WORKDIR='+d+q,'set '+q+'PORT='+p+q];open(r'%ENVBAT%','w',encoding='mbcs',errors='replace').write(chr(10).join(L))"
if exist "%ENVBAT%" call "%ENVBAT%"
del "%ENVBAT%" >nul 2>nul
if not defined WORKDIR set "WORKDIR=%~dp0"
if not exist "%WORKDIR%" set "WORKDIR=%~dp0"

rem ---------- 4. start exam server (opens browser automatically) ----------
echo 正在启动考试系统，请稍候…
start "机试服务" "%~dp0启动机试服务.bat"

set /a TRIES=0
:waitloop
"%PYEXE%" %PYARGS% -c "import socket,sys;s=socket.socket();s.settimeout(0.5);sys.exit(0 if s.connect_ex(('127.0.0.1',%PORT%))==0 else 1)" >nul 2>nul
if not errorlevel 1 goto server_ready
set /a TRIES+=1
if %TRIES% geq 20 goto server_fail
ping -n 2 127.0.0.1 >nul
goto waitloop

:server_fail
echo.
echo [提示] 考试系统 20 秒内没有启动成功。请查看弹出的「机试服务」窗口里的报错信息；
echo        若看不懂，请截图联系组织者。AI 作答工具窗口仍会照常尝试打开。
echo.

:server_ready
if %TRIES% lss 20 echo 考试系统已启动，浏览器将自动打开机试页面（http://127.0.0.1:%PORT%）。

rem ---------- 5. launch AI tool web UI in workdir (new window) ----------
set "WEBCMD_EXE="
for /f "tokens=1" %%W in ("%WEBCMD%") do set "WEBCMD_EXE=%%W"
where %WEBCMD_EXE% >nul 2>nul
if errorlevel 1 (
    echo.
    echo [提示] 没有找到 "%WEBCMD_EXE%" 命令，AI 作答工具可能没有安装成功。
    echo        机试页面不受影响；需要作答时请联系组织者。
    echo.
    goto done
)
if not exist "%WORKDIR%" set "WORKDIR=%~dp0"
start "%AGENT% 作答工具" /D "%WORKDIR%" cmd /k "%WEBCMD%"

:done
echo.
echo 启动完成。本窗口可以关闭（请保留「机试服务」窗口）。
ping -n 9 127.0.0.1 >nul
exit /b 0
'''

WRAPPER = r'''@echo off
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
'''

bad = [c for c in BAT if ord(c) > 127]
out = sys.argv[1] if len(sys.argv) > 1 else "启动考试系统.bat"
with open(out, "wb") as f:
    f.write(BAT.replace("\n", "\r\n").encode("gbk"))
wrapper = sys.argv[2] if len(sys.argv) > 2 else "启动机试服务.bat"
with open(wrapper, "wb") as f:
    f.write(WRAPPER.replace("\n", "\r\n").encode("gbk"))
print(f"written {out} + {wrapper} (GBK, CRLF, non-ascii chars: {len(set(bad))})")
