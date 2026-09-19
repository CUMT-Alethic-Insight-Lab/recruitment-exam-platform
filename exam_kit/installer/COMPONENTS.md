# M11 安装器组件事实清单（R1 研究线产出，实测验证日期 2026-09-05）

本文是安装器开发代理（波次 A）的**唯一事实来源**。所有文件已下载到
`exam_kit/installer/payload_cache/`（该目录不入库，见 exam_kit/.gitignore），
全部哈希为自算 SHA256，其中 Node.js / opencode 已与官方发布哈希交叉比对一致。

payload_cache 总体积约 319 MB（6 个文件），单 EXE 体积相应上浮（见 §6）。

---

## 1. Python 3.12 完整版（官方安装器，每用户静默）

| 项 | 值 |
|---|---|
| 版本 | 3.12.10（3.12 系列最后一个提供 Windows 二进制安装器的版本，后续 3.12.x 仅源码安全更新） |
| URL | `https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe` |
| 文件名 | python-3.12.10-amd64.exe |
| 大小 | 26,964,224 bytes |
| SHA256 | `67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb` |
| MD5（自算） | `5eddb0b6f12c852725de071ae681dde4`（python.org 发布页抓不到官方哈希做交叉校验，下载走 HTTPS 官方域） |
| 验证方式 | 下载成功 + 哈希记录；按任务要求**未在本机执行安装** |

静默安装参数（官方文档 docs.python.org/3.12/using/windows.html 确认）：

```
python-3.12.10-amd64.exe /quiet InstallAllUsers=0 PrependPath=1 Include_test=0
```

- 无需管理员（InstallAllUsers=0），装到 `%LOCALAPPDATA%\Programs\Python\Python312`。
- `PrependPath=1` 自动追加用户 PATH；`Include_test=0` 不装测试套件。
- 可加 `Include_launcher=0`（若不想装 py 启动器，文档示例中每用户安装常配此项）。

## 2. Node.js Windows x64 zip 便携版（LTS）

| 项 | 值 |
|---|---|
| 版本 | v24.20.0（v24 LTS 线最新，2026-09-05 查询 nodejs.org/dist/latest-v24.x） |
| URL | `https://nodejs.org/dist/v24.20.0/node-v24.20.0-win-x64.zip` |
| 文件名 | node-v24.20.0-win-x64.zip |
| 大小 | 37,539,751 bytes |
| SHA256 | `6cac9ffbca8f6a47091e4b5c772e0606049c3871cb67d900c0cedde630e545ba` |
| 官方哈希比对 | 与 `https://nodejs.org/dist/v24.20.0/SHASUMS256.txt` 完全一致 ✅ |
| 验证方式 | 解压到临时目录实测：`node.exe --version` → v24.20.0；`npm.cmd --version` → 11.19.0 ✅ |

部署：

- 解压即用，zip 根目录为 `node-v24.20.0-win-x64/`，node.exe/npm.cmd/npx.cmd 全在其根层。
- 建议解压到 `%LOCALAPPDATA%\Programs\AlethicTools\node\`（可去掉版本号目录）。
- PATH 追加：`%LOCALAPPDATA%\Programs\AlethicTools\node`（一个条目即可覆盖 node 与 npm）。
- npm.cmd 为纯 cmd 批处理，不依赖 PowerShell。

## 3. Git PortableGit 64 位自解压包

| 项 | 值 |
|---|---|
| 版本 | 2.55.0.windows.5（git-for-windows latest，2026-09-05 查询 GitHub API） |
| URL | `https://github.com/git-for-windows/git/releases/download/v2.55.0.windows.5/PortableGit-2.55.0.5-64-bit.7z.exe` |
| 文件名 | PortableGit-2.55.0.5-64-bit.7z.exe |
| 大小 | 58,960,208 bytes |
| SHA256 | `5aa8a20f6e9abb2c755f0e73c91c687701a46b309ad84a0ca6509380fa4ae290` |
| 验证方式 | 自解压到临时目录实测：`bin\git.exe --version` → git version 2.55.0.windows.5 ✅（cmd\git.exe 同样可用） |

部署（cmd 原生，无需管理员）：

```
PortableGit-2.55.0.5-64-bit.7z.exe -o "%LOCALAPPDATA%\Programs\AlethicTools\git" -y
```

- `-o` 指定目标目录（**必须写在 -o 与路径之间无空格**，7-Zip SFX 语法），`-y` 全部确认。
- PATH 追加：`%LOCALAPPDATA%\Programs\AlethicTools\git\cmd`（官方推荐，bin 也可但推荐 cmd）。

## 4. opencode Windows 原生二进制

| 项 | 值 |
|---|---|
| 版本 | v1.18.29（2026-09-04 发布，GitHub releases/latest，非 prerelease） |
| URL | `https://github.com/anomalyco/opencode/releases/download/v1.18.29/opencode-windows-x64.zip` |
| 文件名 | opencode-windows-x64.zip |
| 大小 | 60,655,378 bytes |
| SHA256 | `b32618aa3d1415f6e4f473aec248edef25759203fb707d7d968359d86d4a35ee` |
| 官方哈希比对 | 与 GitHub release API 资产 `digest: sha256:b32618aa…` 完全一致 ✅ |
| 验证方式 | 解压到临时目录，在**仅含 System32 的 PATH**（无 Node/无任何运行时）下运行 `opencode.exe --version` → 1.18.29 ✅ |

关键事实：

- **zip 内只有一个文件 `opencode.exe`**（实测无 bin/ 子目录），单文件即整个程序。
- **不依赖 Node.js**（Go/编译型单二进制，已在无 Node PATH 下实测）。
- 同 tag 另有 `opencode-windows-x64-baseline.zip`（官方 issue 确认内容与普通版相同，取普通版即可）。

Web UI（官方文档 opencode.ai/docs/web/ + /docs/cli/）：

- **`opencode web` 存在**：启动本地 HTTP 服务并**自动打开默认浏览器**。
- ⚠️ 与 `kimi web`（固定 58627）不同：**opencode web 默认随机端口**，绑定 127.0.0.1。
  安装器启动脚本要固定端口需显式传参：`opencode web --port 4096`。
- 其它 flag：`--hostname`（默认 127.0.0.1，`0.0.0.0` 开局域网）、`--mdns`、`--cors`。
- 认证：环境变量 `OPENCODE_SERVER_PASSWORD`（用户名默认 `opencode`，可用
  `OPENCODE_SERVER_USERNAME` 覆盖）。纯本地单人使用无需设置。
- 相关命令：`opencode serve`（无界面 HTTP API）、`opencode attach <url>`（TUI 连后端）。

免费模型（官方 Zen 文档 opencode.ai/docs/zen/，**安装器声明文案请照此表述**）：

- opencode 的免费模型是 OpenCode Zen 上标价 $0 的限时型号：Big Pickle、
  MiMo-V2.5 Free、Ling 3.0 Flash Fin Free、Nemotron 3 Ultra Free、
  Nemotron 3.5 Lightning Free、Muse Spark 1.3/1.2 Contributor Free。
- **需要登录才能用**：免费模型同样走 OpenCode Zen 通道，需注册/登录 Zen 账号拿
  API key，然后在 `opencode auth login` 选 opencode（或 TUI 内 `/connect`）粘贴。
  模型本身 $0（限时），但**首次配置需要联网**，且限时期内数据政策较宽松
  （如 Big Pickle/MiMo Free 期间数据可能用于改进模型）。
- 建议声明文案："opencode 自带限时免费模型（OpenCode Zen 提供），但首次使用需
  联网注册并登录 OpenCode Zen 获取 API key（`opencode auth login`），之后使用免费模型不收费。"

会话存储与导出（v1.18.29 本机隔离 USERPROFILE 实测）：

- 数据根目录：`%USERPROFILE%\.local\share\opencode\`（auth.json 也在其中）。
- ⚠️ **v1.18.29 会话已改为 SQLite 存储**：实测该目录下是 `opencode.db`/`-shm`/`-wal`
  + `log/` + `repos/`，**不再有旧版 `storage/session/*.json` 目录**。
  考试机适配代理（波次 E）自动发现会话时不要依赖旧 storage/ 布局；推荐路径：
  `opencode session list --format json` 列会话 → `opencode export <sessionID>` 导出。
- **`opencode export [sessionID]` 存在**（不传 ID 时交互选择），输出 JSON，
  有 `--sanitize` 脱敏 flag；`opencode import <file|share-url>` 可导入。
  `export_session_example/platform_flow-md.json` 即该导出格式的样例。

## 5. Kimi Code 0.40.1 离线预装（方案 B，产物已入 payload_cache）

结论：**目标机 `npm install -g <tgz>` 必须联网**，必须用方案 B 整目录拷贝。

- `npm pack @moonshot-ai/kimi-code@0.40.1` → moonshot-ai-kimi-code-0.40.1.tgz
  （20,180,600 bytes）。其 package.json：
  - bin：`kimi` → `dist/main.mjs`
  - **dependencies（未 bundled）：`qrcode@^1.5.4`、`ws@^8.18.0`** → 装 tgz 还要联网拉
  - optionalDependencies：`@mariozechner/clipboard@^0.3.9`、`node-pty@^1.1.0`
    （node-pty 为 N-API 原生模块，随包带预编译产物）

方案 B 产物：

| 项 | 值 |
|---|---|
| 文件名 | kimi-code-0.40.1-offline.zip |
| 大小 | 38,274,017 bytes |
| SHA256 | `e5a88c0806f035c8119cd61aac8b113d20ef73c375852b377217d882fab83396` |
| 内容 | `node_modules/` 共 1200 个文件：`@moonshot-ai/kimi-code` + qrcode/ws/yargs 等 35 个包 + `node_modules/.bin/{kimi, kimi.cmd, kimi.ps1, qrcode*}` |
| 制作方式 | 构建机 `npm install --prefix <tmp> @moonshot-ai/kimi-code@0.40.1` 后对整个 `node_modules/` 打 zip |

实测验证（关键）：

- 把 zip 解到**另一个干净目录**，PATH 仅含「payload_cache 里的 Node v24.20.0 解压目录
  + System32」（无全局 node_modules、无 npm 缓存），
  运行 `node_modules\.bin\kimi.cmd --version` → **0.40.1** ✅。

目标机部署：

1. 拷贝 `node_modules/` 整目录到例如 `%LOCALAPPDATA%\Programs\AlethicTools\kimi\node_modules`。
2. PATH 追加：`%LOCALAPPDATA%\Programs\AlethicTools\kimi\node_modules\.bin`。
3. 依赖 Node 在 PATH（.cmd shim 运行时调 `node`，Node 由本清单第 2 项组件提供）。
4. ⚠️ `.bin` 里三个 shim：**`kimi.cmd` 是纯 cmd 可用**（启动脚本一律用 `kimi.cmd`
   或直接 `kimi`，前提 cmd 的 PATHEXT 会选 .cmd）；`kimi.ps1` 需 PowerShell，
   目标机可能没有，不要依赖；`kimi`（sh 脚本）在 Windows 无用。
5. 本 zip 在 npm 源 npmmirror 镜像下载，npm 校验 tarball integrity，与官方 npmjs 内容一致。
6. 局限：node-pty 预编译产物按 N-API 构建，理论上跨 Node 版本可用；本次仅在
   Node v24.20.0 运行时实测通过，目标机 Node 若非 24.x 建议重测 `kimi --version`。

## 6. PowerShell 7 便携版（win-x64 ZIP，M11 组件扩展追加，2026-09-06）

| 项 | 值 |
|---|---|
| 版本 | 7.6.5（PowerShell/PowerShell releases/latest，2026-09-06 查询 GitHub API，非 prerelease） |
| URL | `https://github.com/PowerShell/PowerShell/releases/download/v7.6.5/PowerShell-7.6.5-win-x64.zip` |
| 文件名 | PowerShell-7.6.5-win-x64.zip |
| 大小 | 106,319,290 bytes |
| SHA256 | `32eb8f6cdce08f86e987d625a2733e54ac3e289ae7e1621b14c0b5bcec2434ea` |
| 官方哈希比对 | 与 GitHub release API 资产 `digest: sha256:32eb8f6c…` 完全一致 ✅ |
| 验证方式 | 解压到临时目录，在隔离 PATH 下运行 `pwsh.exe --version` → PowerShell 7.6.5 ✅ |

关键事实（决定安装方式）：

- **ZIP 内 661 个条目全部在根层**：`pwsh.exe` 直接在 zip 根，没有版本号包裹目录
  → 安装方式为 `zip_flat` 原样解压（与 opencode 同类，**不是** node 的 zip_strip_root）。
- 纯便携目录，不写注册表、不需要管理员；目标目录
  `%LOCALAPPDATA%\Programs\AlethicTools\pwsh`。
- PATH 追加：`%LOCALAPPDATA%\Programs\AlethicTools\pwsh`（pwsh.exe 单文件入口即可用）。
- Windows 11 终端入口：安装器额外写 fragment 文件
  `%LOCALAPPDATA%\Microsoft\Windows Terminal\Fragments\AlethicInsight\pwsh.json`
  （profiles.entry，name=「PowerShell 7 (Alethic)」）。fragment 是官方无侵入机制，
  **不触碰用户的 settings.json、不改默认配置文件**；想设为默认由用户自己在
  终端 → 设置 → 启动 → 默认配置文件 里选择。Win10 / 无终端机器写 fragment
  失败属常态，该步骤任何异常只记日志不中断安装。

## 7. 各组件 PATH 汇总（安装器 setx 追加用户 PATH）

```
%LOCALAPPDATA%\Programs\Python\Python312\Scripts\
%LOCALAPPDATA%\Programs\Python\Python312\        (PrependPath=1 时安装器自己加，安装器代理勿重复)
%LOCALAPPDATA%\Programs\AlethicTools\node\
%LOCALAPPDATA%\Programs\AlethicTools\git\cmd\
%LOCALAPPDATA%\Programs\AlethicTools\kimi\node_modules\.bin\
%LOCALAPPDATA%\Programs\AlethicTools\opencode\   (放 opencode.exe 的目录)
%LOCALAPPDATA%\Programs\AlethicTools\pwsh\       (放 pwsh.exe 的目录)
```

## 8. 启动指令对照（启动考试系统.bat 用）

| 工具 | 指令 | 端口 | 自动开浏览器 |
|---|---|---|---|
| Kimi Code | `kimi web` | 固定 58627 | 是（官方文档证实） |
| opencode | `opencode web` | **随机端口，需 `--port` 固定** | 是 |

## 9. 遗留不确定项

- python.org 发布页（/downloads/release/python-31210/）本次抓取不到文件哈希表，
  Python 安装器哈希为自算（HTTPS 官方域下载），未做第三方交叉比对。
- PortableGit SHA256 无官方哈希页可比对（git-for-windows release 不附 digest 文件），
  同为 HTTPS 官方 GitHub release 下载后自算。
- opencode 免费模型清单是限时活动，出货前建议再刷一次 opencode.ai/docs/zen/ 确认仍在。
- 未在真实全新 Win10 上验证（无 VM），仅本机临时目录干净环境模拟。
