# 安装包沙箱验收清单（供全新 Win10 沙箱手工验证）

产物：`exam_kit/installer/dist/招新考试系统-安装程序.exe`（222MB，含全部离线组件，安装全程断网可测）

## A. 安装前（沙箱准备）

- [ ] 全新 Win10（无 Python/Node/Git/kimi/opencode），建议断网以验证 0 外部依赖
- [ ] 把 exe 拷进沙箱（U 盘/共享目录均可），双击运行
- [ ] 若 Windows SmartScreen 弹蓝色提示 → 点「更多信息」→「仍要运行」（欢迎页声明里也写了这一步）

## B. 安装向导

- [ ] 欢迎页：能看到三条声明（考试系统不进注册表/删文件夹即卸载；Kimi Code 为 Moonshot 第三方、需自备模型；opencode 自带免费模型）
- [ ] 工具选择页：默认 Kimi Code；**建议本次选 opencode**（沙箱无模型账号，opencode 免装账号就能起 Web 界面）
- [ ] 环境检查页：cmd 为绿色必需项；PowerShell 显示检查结果但不影响安装；各组件显示「待安装」
- [ ] 点开始安装：进度条逐步走完无报错（Python → Node → Git → opencode → PATH → 快捷方式）
- [ ] 若中途失败：弹窗应有白话原因 +「重试该步」；重试能续上

## C. 安装后核对（开 cmd 逐条跑）

- [ ] 桌面出现文件夹 `招新考试系统-删除文件夹即完成卸载`，内有：exam_server.py、cleaner.py、config.json、启动考试系统.bat、README.md、考试工作目录\、（安装日志.txt）
- [ ] 桌面出现快捷方式「启动考试系统」
- [ ] `python --version` → 3.12.x（新开 cmd 才能见到 PATH）
- [ ] `node --version` → v24.20.0；`git --version` → 2.55.0
- [ ] `pwsh --version` → 7.6.5（PowerShell 7 便携版，送你的）
- [ ] `opencode --version` → 1.18.29
- [ ] （Win11 + Windows Terminal）终端下拉里出现「PowerShell 7 (Alethic)」配置；设置→启动→默认配置文件可手动选它
- [ ] config.json 里 `agent` = 本次选择；`opencode_web_cmd` = `opencode web --port 8898`；`agent_workdir` = 安装目录\考试工作目录

## D. 启动考试系统（核心路径）

- [ ] 双击桌面「启动考试系统」：浏览器自动打开本地考试页（127.0.0.1:8899），另弹一个 cmd 窗口跑 `opencode web --port 8898` 且浏览器打开 opencode Web 界面
- [ ] 若装完立即启动时报「没有找到 opencode 命令」提示条：说明 PATH 尚未刷新，**重开一次启动器即恢复**（bat 已内置 AlethicTools 前置，正常不会出现）
- [ ] 考试页：声明条可见；学号+密码大输入框（此时连不上生产网就显示白话报错，属正常）
- [ ] 断网状态下交卷按钮失败提示为白话，无 traceback

## E. 卸载承诺

- [ ] 删除桌面文件夹 + 快捷方式 → 考试系统彻底消失（不残留开始菜单/注册表项；Python/Node/Git 属于赠品保留属预期）

## F. 二次运行（修复模式）

- [ ] 再跑一次 exe：应提示目录已存在，选修复模式可重解压覆盖，不重复装组件（探测跳过）

## 已知边界（不是 bug）

- opencode 免费模型首次使用需联网注册 OpenCode Zen 账号并 `opencode auth login`（免费额度 $0 型号，限时活动）——声明文案已注明
- Kimi Code 需自备模型/会员账号；`kimi web` 固定端口 58627
- 生产云端地址已在 config（alethicinsight.com/recruitment）；沙箱联考需放网

翻车反馈格式：安装目录下 `安装日志.txt`（白话+技术两栏）+ 沙箱截图。
