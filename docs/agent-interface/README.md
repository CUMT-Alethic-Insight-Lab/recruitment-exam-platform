# Alethic Insight · Agent-Native 组织信息接口体系

> 设计与实施文档 v1.0｜2026-10-27｜**设计稿，尚未部署；先读 [00 · 现有系统核实与方案评审](docs/00-current-system-review.md)**
>
> 适用：Alethic Insight Lab 内部成员、平台维护者与 Coding Agent。
> 主旨：**统一协作契约，不统一个人工作方式。**

## 一句话定位

在现有组织官网身份体系与自托管平台之上，提供一套**成员认证、权限控制、API-first、CLI/Bash/MCP 友好**的组织信息与事务接口。成员可以选择网页、命令行、自己熟悉的 Coding/通用 Agent，或可选的官网内置轻量 DeepSeek Agent，完成查询、填表、申请与状态查询。

**不是**对外开放的公共聊天 AI；**不是**自动经营社团的管理智能体；**不是**代码生成、科研规划或自主调动成员的 Agent 平台。

## 已由讨论确立的设计原则（事实/决策）

1. 服务对象仅为**通过认证的内部成员**；官网公开信息及公开学习仓库可以保持开放，但不为公众提供付费 Agent。
2. **API 是业务能力的唯一逻辑入口**；网页、CLI、MCP、个人 Agent 和可选 DeepSeek 助手均调用同一后端业务服务。
3. **Bring Your Own Agent（BYOA）**：尊重成员既有工作习惯及模型选择，组织不承担个人 Agent 的模型推理成本。
4. 独立维护**入口、使用方式/工具适配、业务内容**。采用模块化单体即可实现低耦合，不追求微服务数量。
5. Agent 以自然语言降低组织办事摩擦：查制度、查项目与资源、填表/申请、查申请状态；不需要写代码或主动安排人员。
6. 现有代码与部署文档显示静态官网、云端 edge、lab authoritative 的混合链路；N5095 / 4 GB RAM / 512 GB SSD 是 lab 侧设计约束而非已验证容量。轻量业务优先复用现有 FastAPI + SQLite，Gitea 是否部署与容量均待实测；推理由远程 API 承担。
7. 已使用 GitHub，自托管 Gitea 为可选集成；每个代码仓库需要**明确权威写入源**，镜像仅为同步而非双主。
8. 原先的三层 Agent 上下文仍然适用：L1 身份/基本规则/Skill 索引；L2 按需发现 Skill、工具与文档；L3 读取具体信息及 metadata。它是上下文发现机制，**不是**权限边界。
9. 可选官网内置模型首选低成本 DeepSeek Flash 类服务；历史讨论中的 **¥2/人** 仅是经验预算量级，**周期、余额规则、当前 API 单价尚未确认**，不得作为已上线制度。

## 还未决定（不能让 Agent 自行假定）

- 正式成员资格/项目 ACL 的权威规则、首批项目/资源/申请的真实流程；现有 `account.role` 不能代替这些决策。
- Gitea 的部署状态与具体域名、GitHub/Gitea 的主仓策略和私有仓访问方式。
- 首批纳入的表单种类、审批责任人、必填字段、权限范围。
- API 的入口域名、是否公网可连接或经私有网络访问，以及是否先做远程 MCP。
- 内部数据分类与个人 Agent 外部模型使用政策；备份存放位置及可靠性目标。

以上待决策点详见 `docs/10-decision-register.md`。

## 文档目录与阅读顺序

| 文件 | 职责 | 读者 |
|---|---|---|
| `docs/00-current-system-review.md` | 现状证据、评审结论、上线前安全门禁 | 全体维护者，先读 |
| `docs/01-product-and-user-stories.md` | 价值、范围、真实场景、角色 | 全体成员、需求负责人 |
| `docs/02-architecture.md` | 三层结构、系统边界、事实来源、失败降级 | 架构/后端 |
| `docs/03-auth-and-permissions.md` | CLI 认证、成员授权、作用域、确认机制 | 后端/安全 |
| `docs/04-api-and-workflows.md` | API 资源、流程、错误及幂等契约 | 后端/前端/CLI |
| `docs/05-cli-mcp-skills.md` | Bash/JSON/本地 MCP、Skill 三层发现 | Agent/工具开发 |
| `docs/06-data-model-and-integrations.md` | 数据模型、GitHub/Gitea、文档与元数据 | 数据/后端 |
| `docs/07-deployment-ops.md` | N5095 部署资源、备份、监控、恢复 | 运维 |
| `docs/08-security-privacy.md` | 威胁模型、外部 Agent 数据界限、审计 | 安全/审核 |
| `docs/09-roadmap-and-acceptance.md` | MVP 分期、依赖、验证/上线清单 | Coding Agent/项目负责人 |
| `docs/10-decision-register.md` | 待确认事项、推荐默认值、决策记录 | 项目负责人 |
| `docs/11-agent-handoff.md` | 可复制的 Coding Agent 交付指令、PR 切分 | Coding Agent |
| `contracts/openapi-skeleton.yaml` | 非生产级 OpenAPI 契约起点 | 后端/客户端 |
| `skills/org-access/SKILL.md` | 可移植的组织访问 Skill 示例 | Agent 用户 |
| `examples/cli-usage.md` | 成员 CLI 使用体验草案 | 成员/测试 |

## 最小用户旅程

1. 成员 `alethic auth login`，经官网身份验证并对 CLI 授权。
2. 在终端运行 `alethic projects list --json`，或让个人 Agent 调用 `alethic mcp serve` 暴露的工具。
3. 成员说“我想申请一块开发板”，Agent 读取表单定义，生成**待确认草稿**。
4. 成员确认后客户端提交申请；平台校验身份、权限、资源和必填字段，记录审计信息。
5. 审批由有权的实际负责人完成；成员通过 UI/CLI/Agent 查询状态。
6. 即使模型 API 断连，网页表单与 CLI/REST 仍可使用。

## 不属于本项目

- 公共匿名 Agent、社交聊天、通用代码补全、Agent 自主跨项目调岗、自动批准资源申请、替代 GitHub/Gitea 的代码评审或 CI、无人确认的写操作。
- 把全部成员资料、私有仓库或会议纪要原文批量送进第三方模型；把授权 Token 放进模型上下文。

## 仓库边界

- `open-onboarding`：公开的学习、协作和 Agent 接入说明，不存内部凭据/真实成员记录。
- `awesome-ai`：学习材料索引，不管理社团申请。
- `governance-public`：公开的正式组织章程与规则；本平台从中引用，不自动创造规则。
- `recruitment-exam-platform`：现有 FastAPI/SQLite 业务平台与本方案文档；后续 API、CLI/MCP 适配器优先在此渐进扩展，是否拆分新仓由实际模块边界决定。私有配置、密钥和真实成员数据不进入仓库。

> **交付定义：** 文档是一个 Coding Agent 能按 PR 分段实现的工程规范草案；其中所有示例路径、CLI 命令、API 均为拟议接口，不代表现有网站已具备。
