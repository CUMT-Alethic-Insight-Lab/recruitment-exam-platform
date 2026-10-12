# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：招新考试系统一键安装器（onefile，tkinter GUI，无控制台窗口）

打包前置条件（由后续打包/收口代理执行，见 installer_README.md）：
  1. pip install pyinstaller
  2. 组装 payload/ 目录（脚本所在目录下）：
       payload/kit/      ← exam_kit/ 根目录的考试机 kit 文件
                           （exam_server.py / cleaner.py / config.json /
                            启动考试系统.bat / README.md）
       payload/ 下的组件离线包 ← exam_kit/installer/payload_cache/ 的全部文件
                           （清单以 components_spec.json 为准，当前 7 个）
  3. pyinstaller installer.spec --noconfirm
  4. 产物：dist/招新考试系统-安装程序.exe（onefile，约 250MB）

约定：installer_app.py 运行时从 sys._MEIPASS/payload/ 读取全部内置资源
（开发模式回退读 exam_kit/ 根目录 + payload_cache/），组件参数全部来自
components_spec.json，本 spec 不涉及任何版本号。
"""

a = Analysis(
    ['installer_app.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('payload', 'payload'),                 # kit + 全部离线组件包
        ('components_spec.json', '.'),          # 组件事实数据（R1 回填）
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='招新考试系统-安装程序',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # 组件包已高度压缩，UPX 只会拖慢启动还易误报毒
    upx_exclude=[],
    runtime_tmpdir=None,    # onefile 默认解压到 %TEMP%\_MEIxxxx，退出自动清理
    console=False,          # 纯 GUI，零基础新生不看黑窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,              # TODO(打包代理): 可替换为社团 logo 的 .ico
)
