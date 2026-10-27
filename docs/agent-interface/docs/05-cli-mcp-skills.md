# 05 · CLI / MCP / Skill：Agent 友好的适配层

## 总目标

让成员熟悉的终端、Coding Agent 或通用 Agent 使用组织既有 API，而不必强制使用官网模型。**CLI 是第一优先级**：对人可读、对机器 JSON 稳定、退出码明确、环境无强耦合；MCP 是协议适配，不承担业务逻辑。

## 命令草案

```bash
# 身份
alethic auth login
alethic auth status --json
alethic auth logout

# 能力/知识
alethic capabilities --json
alethic skills list --json
alethic knowledge search '项目申请' --json
alethic knowledge get <document-id> --json

# 项目与申请
alethic projects list --recruiting --json
alethic projects get <id> --json
alethic forms schema project-join --json
alethic applications list --mine --json
alethic applications get <id> --json
alethic applications submit --file ./application.json --dry-run
alethic applications submit --file ./application.json --confirm

# Agent 使用
alethic mcp serve
```

所有示例命令均为**拟议命令**，需要后续编写 CLI。

## CLI 输出契约

- `stdout`：`--json` 时严格输出一个可解析 JSON 对象/数组，不混入提示语；`--jsonl` 可选。
- `stderr`：日志、警告、登录指引；不输出密钥。
- 推荐退出码：0 成功；2 参数错误；3 未认证；4 无权限；5 API 不可用；6 输入校验失败；7 冲突或要求确认；其他内部错误。
- 非交互模式（`--non-interactive`）严禁自动打开浏览器或提示 Enter；一律以可识别错误结束。
- 分页与过滤：`--limit`、`--cursor`、`--project`，不可一次提取所有成员数据。
- `--dry-run` 由服务端提供字段校验，不能仅由客户端猜测有效性。
- `--confirm` 只在受信任的人类发起的 CLI 操作中直接表示确认；在 Agent 工具调用下必须有独立的交互确认凭据或先经网页最终确认。

## MCP

优先做本地 **stdio MCP 适配器**：成员安装 CLI、完成登录，然后 MCP 子进程复用同一 CLI/SDK 的授权能力。它只负责工具协议和调用路由，API 权限始终由服务端校验。

工具首批建议：

| MCP Tool（建议名） | 后端映射 | 风险 |
|---|---|---|
| `org_capabilities` | GET `/capabilities` | 低 |
| `org_search_knowledge` | GET `/knowledge/search` | 低（内容本身受 ACL） |
| `org_list_projects` | GET `/projects` | 低 |
| `org_get_project` | GET `/projects/{id}` | 低 |
| `org_get_form_schema` | GET `/forms/{kind}/schema` | 低 |
| `org_list_my_applications` | GET `/applications?mine=true` | 中（个人信息） |
| `org_prepare_application` | 本地草稿/服务端校验 | 中 |
| `org_submit_application` | POST `/applications` | 高：必须确认 |

不提供通用 `execute_sql`、`run_shell_on_server`、`fetch_any_url`、`list_all_members` 等高危万能工具。

### 协议版本兼容策略

截至编制日期，MCP 官方规范主页指向 `2026-07-28` 版本，协议与 Client/SDK 支持程度可能不一致。**不要将某个版本硬编码为所有客户端必须支持。** 先用成熟 SDK 实现本地 stdio，明确做目标客户端的实机兼容测试；如计划远程 Streamable HTTP，单独实施并验证 OAuth 发现、资源受众、Origin 检查等安全要求。远程 MCP 并不是第一版的前置条件。

参见：https://modelcontextprotocol.io/specification

## Skill 三层发现

**L1（最薄）**：CLI/Agent 初始说明：你可以通过 `alethic capabilities --json` 发现能力，通过 `alethic skills list` 寻找具体工作流。

**L2（单个 Skill）**：一份 `SKILL.md` 声明何时使用、工具清单、可执行示例、权限、资料地址、对用户应询问什么。

**L3（实时数据）**：Skill 使用 API 检索真实项目、表单和文档；文档来自正式仓库并带版本与访问范围，不能把实时库存写死在 Skill。

`skills/org-access/SKILL.md` 给出具体示范。Skill 不是后端授权规则，也不是模型系统提示词的替代品。

## 支持模式

- 成员选择**自己**的模型/CLI/MCP 客户端，由个人承担使用模型的费用。
- 不同模型有不同工具/安全能力；不保证每种 Agent 原生支持 MCP，CLI 是基本兼容路径。
- 官网页面里的 DeepSeek Agent（若上线）只是相同 API 的可选封装，使用组织内部成员的独立限额和服务端密钥。
- CLI 必须允许没有 Agent 的普通终端用户完成同样事务。

## 技术选型倾向

为避免低配小主机重复维护前端与 API 代码，推荐从已存在的官网语言生态选择后端/CLI 可共享的契约模型；实现语言与框架需在查看网站仓库后决定，而不是预先拍板。Go CLI 单二进制、Python CLI 和 Node CLI 都有可行性，权衡点是现有成员维护能力、认证库、二进制发布流程。
