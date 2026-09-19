# M11 安装器（installer/）开发与验证说明

一键安装器主程序：tkinter 向导 GUI，纯 Python 标准库 + Win32（ctypes/winreg），
目标机 Win10 全程 0 网络、0 PowerShell 依赖（子进程只允许 cmd.exe / 直接 exe）。

## 文件清单与本目录归属

| 文件 | 归属 | 说明 |
|---|---|---|
| `installer_app.py` | 安装器代理（本文件所在线） | 向导主程序：欢迎声明→选 AI 工具→环境检查→安装进度→完成，5 页 |
| `components_spec.json` | 安装器代理创建，**R1 线 COMPONENTS.md 回填** | 组件事实数据：文件名/SHA256/静默参数/目标目录/验证方式/PATH 追加项 |
| `SPEC_README.md` | 安装器代理 | spec 字段约定 + 当前 TODO 占位清单 |
| `installer.spec` | 打包/收口代理 | PyInstaller onefile 配置（打包前需先组装 `payload/`） |
| `build_installer.py` | 打包/收口代理 | 维护机构建脚本：payload_cache 哈希校验 → payload/ 组装 → PyInstaller → 自检 |
| `payload_cache/` | 另一代理（R1） | 离线组件包下载缓存（.gitignore） |
| `COMPONENTS.md` | 另一代理（R1） | 各组件 URL/版本/SHA256/静默参数实测结论——spec 的回填来源 |

安装器代码**只读 `components_spec.json`** 取组件参数，不硬编码版本号；
spec 里标 TODO 的占位字段以 R1 的 COMPONENTS.md 为准回填。

## 本地开发模式运行

```bat
cd exam_kit\installer
python installer_app.py          :: 图形向导（dev 模式：kit 文件读 ..\，
                                 ::  组件包直接读 payload_cache\，不复制不修改）
python installer_app.py --debug  :: 日志附加子进程原始输出
```

开发模式定位逻辑（`locate_payload()`）：frozen 时读 `sys._MEIPASS/payload/`；
否则若存在 `..\exam_server.py` 与 `payload_cache\` 即开发模式；都没有则报白话错误。

## --stage 干跑验证（不碰系统，自动化用）

```bat
python installer_app.py --stage %TEMP%\m11_stage --agent kimi
python installer_app.py --stage %TEMP%\m11_stage2 --agent opencode
echo %ERRORLEVEL%    :: 0=通过
```

干跑只做四件事：解压考试系统文件到指定目录 + 建「考试工作目录」+ 写 config.json
+ 生成「启动考试系统.lnk」（放在 stage 目录内，**不写真实桌面**）。
**跳过全部组件安装，不读写注册表、不改 PATH、不广播 WM_SETTINGCHANGE**。
结束时打印产物清单（逐项 [√]/[×]）并做 config.json 内容回读校验。

已验证结论（2026-09-05，本机）：
- `--stage ... --agent kimi` 与 `--agent opencode` 退出码均 0，产物清单全 [√]；
- .lnk 为合法 ShellLink（ctypes IShellLinkW 生成），内含 UTF-16 目标
  `启动考试系统.bat`；
- config.json：`agent` 正确、`agent_workdir` 指向 stage 目录下的「考试工作目录」、
  kit 默认值（`kimi_web_cmd`/`opencode_web_cmd`/`exam_port=8899`）原样保留；
- GUI 冒烟：Welcome/Agent/Check/Finish 四页真实渲染通过（ProgressPage 需真实
  安装引擎驱动，未在冒烟中触发，回调逻辑为纯 Label/进度条更新）。

## 组件安装逻辑速览（正式模式）

步骤顺序：解压 kit → 写 config → Python 3.12（静默装）→ Node.js（zip 剥根目录）→
Git（PortableGit 自解压）→ 所选 AI 工具（kimi：解压+生成 kimi.cmd shim+`--version`
自检；opencode：解压）→ PATH（winreg 读 HKCU\Environment，去重追加，写回
REG_EXPAND_SZ，ctypes 广播 WM_SETTINGCHANGE，绝不覆盖已有条目）→ 桌面快捷方式
→ 收尾清理。每步安装前重新探测，已装自动跳过；每步失败弹「重试/取消」对话框，
日志落 `<安装目录>\安装日志.txt`（白话+技术双栏）。

## 手动打包（不用脚本时）

```bat
pip install pyinstaller
:: 1) 组装 payload\：payload\kit\ ← exam_kit 根目录 kit 文件；
::    payload\ 下放 payload_cache 的 6 个组件包
:: 2)
pyinstaller installer.spec --noconfirm
:: 产物 dist\招新考试系统-安装程序.exe（onefile，约 222MB）
```

日常构建请直接用上一节的 `python build_installer.py`（含哈希校验与自检）。

## 打包（构建与产物）

维护机一条命令完成校验→组装→打包→自检（`build_installer.py`，本目录归属）：

```bat
cd exam_kit\installer
python build_installer.py
```

- **前置**：维护机需 `pip install pyinstaller`（脚本检测到缺失会自动装）；联网只用于
  装依赖，目标机全程离线。
- **payload_cache 校验**：脚本对 6 个离线包逐一核对 SHA256（与 components_spec.json），
  缺失/不符时**不自动下载**，打印 COMPONENTS.md 里的官方 URL 手动下载重放即可。
- **payload/ 组装**：`payload\kit\` ← exam_kit 根目录 kit 文件（清单从 installer_app.py
  的 REQUIRED/OPTIONAL_KIT_FILES 解析，自动对齐）+ 6 个离线包原样 + components_spec.json。
- **打包**：`pyinstaller installer.spec --noconfirm`，产物 `dist\招新考试系统-安装程序.exe`。

实测记录（2026-09-05，本机 Python 3.14 + PyInstaller 6.19.0）：

| 项 | 值 |
|---|---|
| exe 体积 | 233,052,127 bytes（222.3 MB） |
| PyInstaller 耗时 | 约 16 秒（离线包原样进 CArchive，不重新压缩，无 onefile 超限问题） |
| exe 启动自解压 | 先解 222MB 到 `%TEMP%\_MEIxxxx` 再进向导，**首次启动需等十几秒**，属预期 |

选项：`--kit-only` 只校验+组装 payload/；`--no-kit` 跳过组装直接重打包
（payload/ 已在位时用，例如仅改了 installer_app.py 不涉及 kit/config）。

**坑**：
- kit 的 config.json / exam_server.py 若在构建后被其它线改动，重跑一遍
  `python build_installer.py` 即可（重新组装 payload/ 并重打，不算返工）。
- 构建产物目录 `build/ dist/ payload/` 均已加入 exam_kit/.gitignore，不入库。
- 打包后 exe 为 windowed（无控制台），从 cmd 干跑时看不到白话日志输出，以退出码
  和 stage 目录产物树为准（开发态 `python installer_app.py --stage ...` 才有完整输出）。

干跑产物树（`--stage` 两次实测，kimi / opencode 均退出码 0）：

```
<stage>\
├─ exam_server.py / cleaner.py / config.json / 启动考试系统.bat / README.md
├─ config.json        agent=<选择> agent_workdir=<stage>\考试工作目录，其余 kit 默认
├─ 启动考试系统.lnk   （干跑放在 stage 目录内，不写真实桌面）
└─ 考试工作目录\请在此文件夹里使用 AI 工具作答.txt
```

## 已知边界 / 存疑项

- spec 占位 TODO 见 `SPEC_README.md`（git 自解压参数写法、kimi shim 方案等，待 R1 回填）。
- 干净 Win10 全新机无 VM 可验，交付时需向用户说明；本机验证覆盖
  `--stage` 干跑 + GUI 页面渲染 + py_compile。
- 本机 8000 端口被系统保留与安装器无关（kit 的 exam_port=8899，未改动）。
