# 04 · MVP REST API 契约与业务流程

> 本文件描述本次 MVP 集成目标，不是生产上线声明。接口清单以本文件和 [OpenAPI](../contracts/openapi-skeleton.yaml) 为边界；运行与人工发布步骤见 [MVP 运行手册](../../AGENT_INTERFACE_MVP.md)。自动化、真实客户端与生产验收必须分别记录。

## 共同约定与兼容性

- 应用内部统一 `/api/v1`。公网预期 `/recruitment/api/v1`，由反向代理剥离 `/recruitment`；不能在 router 再加一遍。
- 保留 `/api/auth/*`、`/api/campaigns/*`、旧招新报名、机试、评审和 exam_kit API。新的组织事务申请不是旧招新 `application`，不能混用列表、权限或保留期。
- 网页复用原有不透明 Bearer session，不改成 Cookie、不宣称 OAuth/OIDC。CLI 使用独立、短时、可撤销 grant，不能复制网页 token。
- `require_org_identity(request, scope=None, web_only=False)` 每次检查当前 `account.role`；只有 `member/admin` 是组织成员，`applicant` 无组织权限。内部身份字段为 `student_id, role, principal_id, auth_type, scopes`；外部默认不返回学号或完整成员档案。
- 网页管理操作必须同时 `web_only=True` 和当前 `role == 'admin'`，CLI grant 即使由管理员创建也不继承评审权限。
- 项目、知识检索、申请和 grant 列表统一 `{items, next_cursor}`；使用有界 `limit` 与不透明 `cursor`，结束时 `next_cursor: null`。能力集合和表单字段数组不是列表分页端点。
- HTTPS 用于远程访问；本地仅回环 HTTP。JSON 输入输出，时间使用带时区的 ISO-8601。错误、request_id、分页上限和 TTL 的精确值由集成后的运行时 OpenAPI 与回归测试核对，不沿用草案假定。

## CLI 一次性授权码（不是 OAuth 实现）

| 方法 | `/api/v1` 下路径 | 权限与语义 |
|---|---|---|
| POST | `/auth/cli/authorization-requests` | 网页成员；body `request_id, scopes`，返回 `code, expires_at` |
| POST | `/auth/cli/token` | 无需已有 Bearer；body `request_id, code`，一次性交换，返回 `token, expires_at, scopes` |
| GET | `/auth/cli/grants` | 网页成员查看本人 grant，分页 |
| DELETE | `/auth/cli/grants/{id}` | 网页成员撤销本人 grant |

1. CLI 生成随机 `request_id`，告诉用户所需最小 scopes 并引导可信网页授权。
2. 用户在已有登录态的网页核对请求和 scopes，网页申请短时一次性 code。
3. CLI 用匹配的 `request_id + code` 交换独立 token。失效、重复兑换、请求不匹配不能签发新 token。
4. code/token 不进 URL、日志、模型上下文或普通命令输出；`/token` 的响应是凭据交付，不代表应把完整响应粘贴给 Agent。
5. 网页可列出/撤销授权；当前成员身份失效后旧 grant 不应继续获得权限。

本次不提供 OAuth discovery、PKCE/OIDC Provider、第三方登录或远程 MCP 授权服务器。以后若实施 OAuth，应另行迁移和安全评审，不能把本协议命名为已完成的 OAuth。

## 业务接口

| 方法 | 路径（省略 `/api/v1`） | 权限与边界 |
|---|---|---|
| GET | `/me` | `profile:read:self`；最小当前身份 |
| GET | `/capabilities` | 当前身份实际能力；不是赋权接口 |
| GET | `/projects` | `projects:read`；仅可见项目，分页，可筛选招募状态 |
| GET | `/projects/{id}` | `projects:read` + 可见性；项目详情 |
| POST / PATCH | `/projects` / `/projects/{id}` | 仅网页当前 admin；创建/更新目录 |
| GET | `/knowledge/search` | `knowledge:read`；查询 `q`，仅授权文档摘要，分页 |
| GET | `/knowledge/documents/{id}` | `knowledge:read` + 文档可见性；原文与摘要同权限 |
| GET | `/forms/project-join/schema` | `applications:create`；项目加入的表单版本、字段、来源和更新时间 |
| POST | `/applications/drafts` | `applications:create`；持久化本人待确认草稿 |
| POST | `/applications/validate` | `applications:create`；仅校验与规范化预览，不签发确认 token |
| POST | `/applications/{id}/confirmations` | `applications:create` + 本人可信网页；核对草稿后签发短时、一次性确认 token |
| POST | `/applications` | `applications:create`；body `draft_id + confirmation`，必需 `Idempotency-Key` |
| GET | `/applications` | `applications:read:self`；本人申请，分页，不靠 `mine=true` 实现隔离 |
| GET | `/applications/{id}` | 本人归属；网页 admin 可读取已进入审批流程的共享业务记录，不能读取他人未提交草稿 |
| POST | `/applications/{id}/review` | 仅网页当前 admin；审核决定与审计 |
| POST | `/applications/{id}/withdraw` | `applications:create` + 本人可信网页；撤回允许状态 |

项目管理、项目详情与撤回已纳入本次本地实现。本次不包含 `/resources`、设备借用、通用 `/forms/{kind}/schema`、指派审批和仓库管理 API；其他设计文件中的相关项仍是未来方案。项目及知识目录可为空，不伪造库存、负责人和已安装服务。

## 项目加入闭环

1. 查询项目和 `/forms/project-join/schema`，以返回的 revision 和字段定义为准。
2. 调用 validate 获取规范化预览；它不持久化、不提交、不返回确认 token。
3. 调用 drafts 保存草稿。CLI/MCP 可完成这两步，但响应不能含确认 token。
4. 用户打开可信网页，重新读取本人草稿、目标项目、表单版本和完整预览，再明确确认。网页调用 `POST /applications/{id}/confirmations`。
5. 确认凭据绑定主体、草稿与规范化内容摘要，短期且单次使用；内容、版本或身份变更后不能沿用旧确认。CLI 的 `--confirm` 或 Agent 的“用户已同意”文字不能替代这一步。
6. `POST /applications` 只提交 `draft_id` 与 `confirmation`，不得再次直接提交 `fields`。服务端从持久草稿取内容，并在同一事务内校验确认、提交和记录幂等结果。
7. 幂等键按主体与操作隔离、绑定请求摘要；相同请求重试返回同一结果，相同键不同请求返回冲突。应先处理合法重放，再拒绝已消费的 token，不能令网络重试产生重复申请。
8. 提交成功只是 `submitted`，不代表已批准。网页管理员审核后记录决定、操作者与时间；CLI/MCP 不执行 review。

最小闭环为 `draft -> submitted -> approved | rejected`。当前事务模块还接受 review decision `in_review/needs_information`，允许补件草稿再确认提交，以及本人网页 withdraw；具体可转换状态由服务端验证，终态不能任意退回。CLI/MCP 只准备草稿，不执行这些管理动作。

当前请求形状：draft/validate 接受 `kind: "project-join"`、`form_revision` 与 `fields`；fields 是 `project_id`、`motivation`（10..2000 字符）、`weekly_hours`（整数 1..40）、可选 `notes`（最多 2000 字符）。draft 可再传 `draft_id` 更新本人可编辑状态，更新使旧确认失效。validate 返回 `valid, preview, content_hash`；confirmation 接口返回 `confirmation` 字符串、`expires_at, preview`；最终提交仅 `{"draft_id":"...","confirmation":"..."}`。不再接受旧示例嵌套 `confirmation_token`。review body 为 `decision, reason`，理由必须非空。

## 契约检查

- OpenAPI skeleton 是人工维护的 MVP 契约；精确请求字段、状态码和错误形状需与集成后 `/openapi.json` 对照，不能拿本文件代替运行测试。
- 私有资源的列表、摘要、原文、详情必须同时授权；使用难猜 ID 不是鉴权。
- 错误不能暴露 SQL、密码、Bearer、code 或确认 token；不得声称旧 `{detail: ...}` 全部已迁移为统一错误结构。
- 验证缺少认证、applicant、降级/撤销、scope 不足、他人草稿、CLI 取确认、重复确认、幂等冲突和 CLI review 均被拒绝。
- 公开/成员知识必须带来源与版本信息；任何模型生成的说明都不是制度权威。restricted 招新档案、成绩和评语不进入通用知识检索。
