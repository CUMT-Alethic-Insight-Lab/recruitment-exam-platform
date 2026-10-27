# 00 · 现有系统核实与方案评审

> 核实日期：2026-10-27。范围是 `recruitment-exam-platform` 的 `main`（`d3eaf37`）、组织公开仓库清单及公开制度。未读取生产数据库、密钥或私有运维正文，未进行线上写入或入侵式验证。
>
> 本文件只确认代码和仓库中可见的事实；不能证明生产环境已经部署同一版本。本地未提交的成员资料改动不是 `main` 的既有能力，不能作为本方案的已实现前提。

## 结论

方案值得进入设计评审，不建议直接按原顺序开发。正确定位是：**把现有招新平台逐步扩展为成员事务平台，CLI/MCP 只是适配器**。模块化单体、复用身份与数据库、先无模型闭环、最小权限、人工审批、单机优先备份，均符合现有维护能力。

开始真实 Agent 接入前，应先完成身份撤权、令牌隔离、数据分级和兼容契约四件事。第一期建议只做公开制度检索、成员可见项目目录及一个真实低-risk 表单；不要先做通用知识库、Gitea、远程 MCP 或官网模型。

## 已核实的现状

| 事项 | 代码/仓库证据 | 对方案的约束 |
|---|---|---|
| 后端是 FastAPI + Pydantic，SQLite 持久化 | `server/app/main.py`、`server/app/db.py`、`server/requirements.txt` | 沿用 Python/FastAPI/SQLite；无需另起微服务或第二身份库 |
| 已有统一账号入口，但不是 OAuth/OIDC Provider | `server/app/routes/auth.py` 的 `/api/auth/login`、`/api/auth/me` | 网页登录存在不等于可安全委托 CLI；需新增授权流程 |
| 网页与 API 使用不透明 Bearer token，服务端只存 SHA-256 哈希 | `server/app/auth.py`、`server/static/login.html` | 不是 Session Cookie；不能把“防 CSRF”当作唯一网页令牌问题 |
| token 默认有效期 7 天，存于浏览器 localStorage | `server/app/config.py`、`server/static/js/topbar.js` | CLI 不复用网页 token；网页令牌还需 XSS 防护和生命周期治理 |
| 账号角色为 admin/member/applicant；管理接口有独立门禁 | `server/app/constants.py`、`server/app/routes/accounts.py` | 角色可复用，但不等于成员资格、项目 ACL 和客户端 scope |
| 招新 application、评审、面试预约已有独立业务语义 | `server/app/db.py`、`server/app/routes/booking.py` | 新的事务申请不可把招新报名及个人评审数据默认暴露给 Agent |
| 已有招新批次数据回收；账号和批次不是同一生命周期 | `server/app/maintenance.py` | 新事务、审计和成员资料分别制定保留策略，不能一律继承批次期限 |
| 部署是静态官网、云端 edge、lab authoritative 的混合链路 | `server/deploy/README.md`、仓库根 README | 不是“所有服务都在一台 N5095”；写入权威与故障降级要单独设计 |
| 官网源码在独立 `website` 仓，平台源码在本仓 | 组织仓库清单及两仓 README | 不在此 PR 中重写官网或假定 Pages 路由已经覆盖新 API |
| 正式成员及 AI 辅助规则在 `governance-public` | `docs/zh-CN/membership.md`、`ai-assistance-policy.md` | 组织接口引用正式制度，不在示例 Skill 中创造规则 |
| Gitea、硬件实测、真实项目/设备流程 | 本次没有可核实证据 | 继续标记 UNKNOWN；不声称已安装、可承载或已有库存 |

## 上线前必须修复的身份问题

### 1. 角色变更与旧会话之间存在撤权缺口

`require_admin_session()` 在 `server/app/auth.py` 中检查 session kind 和账号 `pw_changed`，但不重新检查当前 `account.role`。`set_role()` 只更新角色，没有撤销该账号旧 session。因此管理员被降级后，其尚未过期的 admin session 仍可能通过管理门禁。

同样，session 中的 `kind` 是登录时快照，不是当前成员资格；`require_applicant()` 与普通 `/api/auth/me` 路径不能代替统一的成员状态检查。这里属于代码审查结论，修复时必须用真实 HTTP 回归用例验证。

**最低要求：**

- 每次受保护请求根据当前账号状态、角色/成员资格、客户端授权和资源 ACL 计算权限，不能信任登录时的角色快照。
- 降级、退出、禁用、重置密码立即撤销或无效化相关会话；权限收窄不需要等待 token 自然过期。
- 用管理员降级、成员退出、项目撤权、重置密码、已撤销客户端五类用例覆盖所有新旧入口。

### 2. 网页会话不能直接变成 Agent 凭据

现有 token 没有 audience、client_id、device 或 scope。把它交给 CLI/MCP 会使适配器继承网页全部能力，并可能继承管理权限。网页 token 与 Agent token 必须按客户端类型隔离，Agent 默认只读、短时、可撤销，管理/评审 scope 不进入普通 BYOA 客户端。

localStorage 不是 OAuth 问题，但会让 XSS 直接取得 bearer token。新的成员内容、Markdown、个人主页链接和文档索引都应视为 XSS 输入面，采用安全渲染、CSP、输出编码及依赖审计；不要仅靠 HttpOnly Cookie 或“页面没有富文本”作假设。

## 契约与数据边界修订

1. `/api/v1` 是新的成员事务命名空间；保留 `/api/auth/*`、`/api/campaigns/*` 等旧接口给现有网页和 exam_kit。先做适配和契约测试，不能一次性改名破坏考生端。
2. 新 `Application` 表示项目加入/资源事务，不等于招新报名。数据库表、Python 模型、API schema 命名要消除歧义；招新问卷、成绩、面试评语和联系方式默认 `restricted`，不得通过通用 CLI/MCP 返回。
3. `profile:read:self` 只返回最小身份和展示字段，不默认返回学号、手机号、出生年月、政治面貌等档案。主体内部可沿用学号键，外部响应应使用稳定、不可枚举的 principal id 或明确的最小字段。
4. 表单定义必须携带 `source_ref`、revision 和更新时间。申请提交必须绑定表单版本和规范化后的内容摘要；确认后内容再变化需重新确认。
5. FastAPI 默认 `{detail: ...}` 错误与拟议 `ApiError` 并不一致。需统一异常处理、request_id、验证错误、429/Retry-After 和限流响应，再发布 SDK。
6. 幂等键必须限定在 principal + operation 范围内，并绑定请求摘要；同键不同请求返回冲突。处理中和已完成的结果均须可安全重放。
7. 服务端不能从 `--confirm` 判断按键的是人还是 Agent。高风险写入首版在可信网页最终确认；一次性确认令牌必须绑定用户、客户端、操作和内容摘要，短期且单次使用。

## 数据与工具的现实约束

- **CLI 不可信地声明“我不是 Agent”。** 任何本地 CLI 输出都可能被脚本转交外部模型。通用 CLI/MCP 的字段和文档默认按“可能离开组织设备”来设计；restricted 数据走受控网页，不靠提示词保密。
- 检索索引与原文必须执行相同 ACL；撤权要同时影响原文、缓存、搜索摘要和导出。
- 不因为使用 SQLite 就接受阻塞式外部网络调用。GitHub/Gitea 同步、模型调用和文档索引须超时、有界并隔离于核心事务请求。
- lab authoritative 的单机故障不会因增加 CLI 而消失。新事务是否沿用该写入拓扑、云端如何暂存、RPO/RTO 及恢复责任人应在 Phase 0 决定并演练。
- Gitea 是可选集成，不应成为组织查询/申请 MVP 的依赖。镜像只同步指定 refs，公开仓与私有仓不做未经审查的全量双向同步。

## 建议的第一期

1. 修复撤权缺口并补完整回归测试；区分网页 session 与 CLI client grant。
2. 选一个真实、字段少、审批人明确的低-risk 事务，先 Web 闭环，再 REST，再 CLI。
3. 文档检索从 `governance-public` 的公开制度和明确批准的成员文档开始，不索引招新、评审或完整私有代码。
4. 只读 Agent 工具稳定后再开放申请写入；写操作最终确认落在可信网页。
5. Gitea、远程 MCP、官网内置模型均后置，按真实使用频率和运维能力重新评估。

## 本次未验证

- 未验证生产版本、实际角色变更请求、域名/TLS、主机资源、备份恢复或 Gitea 运行状态。
- 未验证任何 Agent/MCP 客户端、外部模型价格、真实审批流程或成员试用。
- 本文件是设计与代码审查，不是安全审计完成证明，也不授权直接部署身份/权限改动。
