# components_spec.json 说明

`components_spec.json` 是 M11 安装器主程序（`installer_app.py`）唯一的组件事实数据来源：
安装器**只读这个文件**取文件名 / SHA256 / 静默参数 / 目标目录 / 验证方式 / PATH 追加项，
代码里不硬编码任何版本号。

## 文件归属与数据来源

- 数值已按 R1 线 `COMPONENTS.md`（实测验证日期 2026-09-05）**回填核对完毕**；
  五个 SHA256 与安装器代理对 `payload_cache/` 的自算哈希一致。
- 后续 COMPONENTS.md 更新（如 opencode 版本刷新）时，直接覆盖本 JSON 对应字段即可，
  安装器代码无需改动。

## 字段约定

- 所有路径支持 `%LOCALAPPDATA%` 等环境变量占位，安装器运行时 `os.path.expandvars` 展开。
- `kind`：`exe_installer`（静默装完即走）｜`zip_strip_root`（zip 剥一层根目录解压）｜
  `zip_flat`（原样解压）｜`self_extractor`（带参数运行自解压 exe，`{target}` 占位符）。
- `verify_files`：装完/探测时的落盘文件校验；`verify_cmds` 用 `cmd.exe /d /c` 探测，
  `version_regex` 非空时要求输出正则匹配（python 用 `3\.12`）。
- `post_install_check`：装完后的真实运行自检命令模板（`{target}` 展开为该组件
  target_dir），探测时安装器把各组件 `path_append` 目录**临时**附加进子进程 PATH
  （不改系统）；失败视为该步失败、可重试。当前用于 kimi（`.bin\kimi.cmd --version`）
  与 opencode（`opencode.exe --version`）。
- `path_append`：安装成功后追加进 HKCU\Environment 的 Path 的目录
  （REG_EXPAND_SZ、去重、绝不覆盖已有条目）。Python 的 PATH 由其安装器
  PrependPath=1 自己写，`path_append` 故意为空。
- `agent_component`：向导选择的 agent（kimi/opencode）→ 对应组件键；
  安装器只装所选的那个。
- `needs_shim`：kimi 曾设计由安装器生成顶层 kimi.cmd；R1 实测方案 B 直接用包内
  `node_modules\.bin\kimi.cmd`，现 `needs_shim=false`（installer_app.py 保留 shim
  生成代码作为后备，只有 spec 置 true 才启用）。

## 关键回填记录（COMPONENTS.md → spec）

- Git：目录名小写 `git`；`-o<路径>` 单 token（7z SFX 语法，无空格）；PATH 加 `git\cmd`。
- Python：静默参数 `/quiet InstallAllUsers=0 PrependPath=1 Include_test=0`（官方文档确认）。
- Kimi：方案 B（离线 zip 顶层 node_modules/ → `AlethicTools\kimi\`，PATH 指向
  `node_modules\.bin`；kimi.ps1 需 PowerShell，不可依赖）。
- opencode：单文件 opencode.exe，不依赖 Node；`opencode web` 默认随机端口
  （固定端口需 kit 侧 `--port`，安装器不改 web 指令）。

## 遗留留意项（非阻塞）

- opencode 免费模型是 OpenCode Zen 限时 $0 型号，出货前建议再刷
  opencode.ai/docs/zen/ 确认清单仍在（COMPONENTS.md §10）。
- Python / PortableGit 哈希为自算（官方页无第三方可比对哈希），HTTPS 官方域下载。
