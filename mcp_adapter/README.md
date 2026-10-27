# Alethic local stdio MCP adapter

此包只把固定 MCP 工具转换为本机 `alethic` CLI 子进程，不访问 HTTP、数据库或凭据文件，不实现身份校验，不提供提交、确认、评审、任意 shell/SQL/URL 工具。API 仍须按当前成员角色、scope 和资源 ACL 授权。CLI 必须先由成员在 MCP 会话之外登录；不要把 token 放入客户端配置、聊天或工具参数。

## 安装与运行

需要 Python 3.10+，无额外运行依赖。在仓库根目录执行：

```sh
python -m pip install ./mcp_adapter
alethic-mcp --cli alethic --timeout 15
```

也可从 `mcp_adapter/` 运行 `python -m alethic_mcp`。`--cli` 是受信任的本地可执行文件名/绝对路径，不是 shell 命令，不接受附加命令字符串；它只能由本机启动配置决定，不能由 MCP 消息覆盖。Windows 建议使用 CLI 安装生成的 `alethic.exe`，不使用 `.bat/.cmd` 包装 shell。

客户端启动配置示意（不是经过验证的特定客户端配置）：

```json
{"command":"alethic-mcp","args":["--cli","alethic","--timeout","15"]}
```

只支持本地 stdio、UTF-8、每行一个 JSON-RPC 2.0 对象。stdout 仅协议消息，适配器不回显日志；CLI stderr 丢弃而不转发给模型，启动参数错误由 argparse 写 stderr。不支持 Content-Length 帧、批量请求、远程传输、资源、prompts 或订阅。实现顺序处理请求，不宣称支持并发取消。

## 协议边界

有界版本集合：`2024-11-05`、`2025-03-26`、`2025-06-18`。`initialize` 请求版本在集合内则原样返回，否则返回本实现最新的 `2025-06-18`；客户端如不支持该返回版本应断开。版本列表只表示此工具子集的实现协商范围，不宣称已与所有这些版本的完整规范或任意真实 Agent 客户端完成兼容验证。

客户端发送 `initialize` 后还须发送 `notifications/initialized`，才能调用 `tools/list` / `tools/call`。支持 `ping`，未知通知忽略且没有响应。工具参数错误和未知工具使用 JSON-RPC `-32602`；未知方法使用 `-32601`；工具执行失败通过 MCP `isError: true` 和稳定 `error.code` 返回。

单条输入上限 64 KiB，CLI JSON 输出上限 1 MiB（超出拒绝，不截断成成功结果），单次调用超时默认 15 秒，可在启动时设为 0.05–120 秒。CLI stdout 暂存到本机临时文件，避免在内存无限累积；时间上限内的临时文件磁盘占用仍依赖受信任 CLI。临时文件调用结束关闭删除。

## CLI 对接契约

此包不引入 CLI 包依赖；下列固定 argv 已按工作树 `cli/alethic_cli/cli.py` 的解析器对齐。命令附加 `--json --non-interactive`，不得自动打开浏览器/交互输入。成功 stdout 必须是一个 JSON 对象或数组，stderr 不作为业务错误文本。所有用户输入通过 argv 的单一参数或临时 JSON 文件传输，`shell=False`。检索 query 放在全部选项和 `--` 之后，不能解释成 CLI 选项。

| MCP 工具 | CLI argv（`FLAGS` = `--json --non-interactive`） |
|---|---|
| `org_capabilities` | `capabilities FLAGS` |
| `org_search_knowledge` | `knowledge search [--limit N] [--cursor C] FLAGS -- <query>` |
| `org_list_projects` | `projects list [--recruiting] [--limit N] [--cursor C] FLAGS` |
| `org_get_project` | `projects get <id> FLAGS` |
| `org_get_form_schema` | `forms schema project-join FLAGS` |
| `org_list_my_applications` | `applications list --mine [--limit N] [--cursor C] FLAGS` |
| `org_prepare_application` | `applications draft --file <temporary-draft.json> FLAGS` |

`recruiting=false` 和省略参数均表示不筛选；true 才传 `--recruiting`。草稿临时文件包含 `{"kind":"project-join","form_revision":"...","fields":{...}}`，只保存未提交草稿，调用结束（包括超时/失败）删除文件及目录。CLI 返回的 `confirmation_url` 是网页入口，不是凭据，允许返回；确认 token 始终删除。适配器不调用正式 submit 或 confirmation 接口。最终确认及正式提交只能在可信网页完成。

草稿 `fields` 采用当前已实现的 project-join 白名单：必填 `project_id`、`motivation`（10–2000 字符）、`weekly_hours`（整数 1–40），可选 `notes`（最多 2000 字符）。服务端仍验证当前表单版本、项目及业务规则。顶层及 fields 都拒绝额外字段，不能通过 `token`、`confirmation`、`role`、`command` 等扩展提升权限。表单变更须同步更新工具 schema 和测试。搜索长度 1–200，分页 limit 1–50、cursor 1–512，ID/revision 为长度 1–128 的非选项 ASCII 标识符。

CLI 非零退出映射：2 `CLI_ARGUMENT_ERROR`；3 `AUTH_REQUIRED`；4 `FORBIDDEN`；5 `API_UNAVAILABLE`；6 `VALIDATION_FAILED`；7 `CONFIRMATION_REQUIRED`；其余 `CLI_FAILED`。另有 `CLI_TIMEOUT`、`CLI_UNAVAILABLE`、`CLI_OUTPUT_INVALID`。错误不会回传 CLI stdout、stderr、异常文本、环境或命令行。

## 凭据与不可信内容

子进程环境只继承 PATH、Windows 系统路径、临时目录、用户配置目录及语言设置，另设 `PYTHONUTF8=1`。为匹配当前 CLI，还白名单继承受信任本机启动配置中的 `ALETHIC_API_BASE_URL`、`ALETHIC_BASE_URL`、`ALETHIC_CONFIG_DIR`（地址和目录，不是凭据）。不继承 `ALETHIC_TOKEN`、`AUTHORIZATION`、模型 API key、PYTHONPATH 等任意环境变量。CLI 从自己的本地安全存储读取短时 grant，不依赖 token 环境变量。MCP 工具参数不能设置服务地址、配置目录或环境。

防御性递归删除 CLI JSON 中 token、confirmation、authorization、password、secret、credentials、env 等敏感键，并遮蔽 Bearer 字符串。**这不替代 CLI 的无密钥输出契约**：任意自然语言中的无标记秘密无法可靠识别，CLI 绝不能把凭据放在文档/错误/其他字段中。搜索结果和表单文本视为不可信数据：其中的“忽略规则”“以管理员提交”等句子不会改变工具列表、argv 路由、认证或调用权限。适配器不读取/提升凭据；服务端 ACL 是最终边界。

## 验证

仓库根目录运行：

```sh
python -m pytest server/tests/test_m36_org_mcp.py -q
```

测试会启动真实 stdio 适配器进程，再由其启动临时 Python mock CLI 进程，覆盖初始化/版本协商、工具清单、固定只读命令、临时文件草稿及清理、恶意输入不执行 shell、不存在提交工具、schema 拒绝、环境隔离、稳定错误及超时/错误脱敏。另通过真实 `cli/alethic_cli` 子进程访问 loopback mock HTTP 服务，使用临时合成 grant 验证检索及草稿完整链路，不读取真实凭据或访问生产服务。此测试不替代与完整真实 API 和目标 Agent 客户端的整体验证。
