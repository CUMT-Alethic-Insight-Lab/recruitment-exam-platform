# 09 · 分期交付计划与可验证验收

## 原则

先让**无需 LLM 的业务闭环**跑通，再接个人 Agent 与可选官网 DeepSeek；先验证安全边界，再开放写入。每个阶段独立 PR，可回滚；没有审核权限时停在 PR，不直接推主分支。

## Phase 0｜既有系统事实审计（必须先做）

交付：`inventory.md`（独立内部文件，可能含敏感信息，不进入公开仓）、依赖版本、网络/端口、网站认证模式、现有成员/项目数据库、CPU/RAM/磁盘峰值、域名/TLS、GitHub/Gitea 配置。

验收：每一个“已存在”均有代码路径/配置/运行截图或命令证据。未知以 `UNKNOWN` 标示；不得构造假入口或假 API。

代码侧初审见 [00 · 现有系统核实与方案评审](00-current-system-review.md)，不替代生产配置、硬件与恢复能力核查。

## Phase 0.5｜身份与兼容安全门禁（P0，先于 Agent 接入）

- 本 PR 已覆盖管理员角色变化撤销旧会话及管理门禁实时角色核验；后续仍须让成员退出、项目 ACL 和客户端 grant 具有同等撤权语义，所有受保护请求检查当前权限。
- 隔离网页 session 与 CLI/MCP client grant，后者默认短时、只读、可撤销，不继承管理权限。
- 明确招新报名与新事务申请的边界；restricted 档案、评审及联系方式不进入通用 CLI/MCP。
- 保留旧网页/exam_kit 接口，用 `/api/v1` 做兼容扩展，并统一错误、request_id、限流及幂等契约。

验收：管理员降级、成员退出、项目撤权、密码重置、客户端撤销的真实 HTTP 回归测试通过；旧网页和 exam_kit 兼容测试通过。未完成本阶段，不开放真实数据的 Agent 工具。

## Phase 1｜业务层和 Web 基础（P0）

实现：会员身份映射、`/me`、知识文档列表/检索、项目基础目录、表单定义、草稿/提交/查询自己申请，受控审核后台；OpenAPI 契约与单元/权限测试。

验收：在无需模型情况下，用户可通过 Web 完成一次申请，审批人通过授权界面处理，普通成员无法查看他人申请。所有数据来自持久 DB 而不是模型输出。

## Phase 2｜API 认证与 CLI（P0）

实现：复用/扩展登录至 CLI 的安全授权；`auth login/status/logout`、JSON 响应、错误代码、`--dry-run`、申请预览/确认、跨平台初级支持（至少 Linux + Windows）。

验收：新设备登录成功、撤销成功、令牌不暴露；JSON 可机器解析；断开模型服务后 CLI 仍能办理同一事务。

## Phase 3｜个人 Agent 接入（P1）

实现：本地 stdio MCP + `skills/org-access/SKILL.md` + 接入指南；读取和申请工具，工具列表可以限制权限。

验收：至少两种不同运行形态：一种支持 MCP 的 Agent，一种只能运行命令行的 Agent。两者使用相同成员身份与 API；Agent 被恶意文档提示后无法越权，申请必须确认。

## Phase 4｜GitHub/Gitea 集成（P1，可独立并行）

实现：仓库权威源登记、只读项目仓元数据、单向镜像与同步状态、Webhook 验证、错误重试；仅选择性同步公开允许的内容。

验收：GitHub 掉线时业务申请仍可工作；同步过期清楚展示；私有项目不被镜像泄漏。Gitea 服务启动、备份和恢复已验证。

## Phase 5｜可选官网 DeepSeek（P2）

实现：成员内置 Org Agent 的轻量问答/表单交互，L1/L2/L3 三层上下文加载；组织 API Key 在后端保管；用相同工具；可按成员设置 Token/费用上限。

验收：模型不可用时网页表单、REST 和 CLI 正常；未授权用户不能调用；用日志评估平均调用次数及开销，不假设 2 元的固定服务次数。

## 质量门禁（每个阶段）

**必须具备：**

- 功能单元测试、核心 HTTP 契约测试、鉴权/ACL 回归测试。
- 端到端成功/失败/重试/取消用例；风险动作具确认环节。
- OpenAPI 文档与实现对齐，示例命令实际执行成功。
- 真实硬件上有空闲/轻负载/短时峰值记录，测量 RSS、CPU、响应时间及磁盘增长；不能承诺未经实测的吞吐量。
- 备份脚本和一次可复现的恢复操作证明。
- 合并前 README、部署步骤、环境变量示例、升级/回滚、疑难排查文档同步更新。

## 验收矩阵

| 用例 | Web | REST | CLI | MCP | 模型非必需 |
|---|---|---|---|---|---|
| 查看自己身份 | ✓ | ✓ | ✓ | 可选 | ✓ |
| 搜索允许文档 | ✓ | ✓ | ✓ | ✓ | ✓ |
| 查看项目与招募 | ✓ | ✓ | ✓ | ✓ | ✓ |
| 填草稿并预览 | ✓ | ✓ | ✓ | ✓ | ✓ |
| 提交申请（确认） | ✓ | ✓ | ✓ | ✓ | ✓ |
| 查询本人申请 | ✓ | ✓ | ✓ | ✓ | ✓ |
| 指派审核 | ✓ | ✓ | 未来可选 | 不建议首版 | ✓ |

## 非功能性能目标

不要拍脑袋写“1000 QPS”之类数字。建议以**组织真实成员数、并发典型场景**确定验收负载：例如 2–5 个并行检索/申请请求、Gitea 同步期间可用性、连续服务数小时的内存曲线、低内存情况下处理 429/503 的行为。具体阈值在 Phase 0 测量后确定。

## PR 拆分建议

1. `docs: decision register, API scopes, model, threat model`
2. `feat: core model + migration + permission middleware + read APIs`
3. `feat: application draft/submit/approval + audit + tests`
4. `feat: CLI auth + JSON queries + form submit`
5. `feat: local stdio MCP + Skill integration examples`
6. `feat: Gitea/GitHub repo bindings and sync (read-only first)`
7. `feat: optional website org agent (gated)`
8. `ops: backup/restore/metrics/security hardening`

每个 PR 附：范围、数据变更、风险、测试结果、回滚方法、文档变更和未解决事项。重大身份/权限变更必须先评审设计。
