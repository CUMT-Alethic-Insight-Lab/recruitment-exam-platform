# 11 · MVP Coding Agent 集成交接

## 阅读与范围

先读 [00 · 现有系统核实](00-current-system-review.md)、[04 · API](04-api-and-workflows.md)、[09 · 验收](09-roadmap-and-acceptance.md)、[10 · 决策](10-decision-register.md) 和 [运行手册](../../AGENT_INTERFACE_MVP.md)。其他设计文件和 CLI 示例仍可能包含未来命令、OAuth、设备借用及复杂状态机，不能直接当成本次已实现接口。

本次是现有招新平台的组织接口增量：保留旧 `/api/auth/*`、招新/机试/评审及 exam_kit；新接口内部 `/api/v1`，公网反代预期 `/recruitment/api/v1`。不重写官网、不读取密钥或生产数据、不部署、不自行把 mock 导入真实目录。

## 集成所有者检查清单

1. 在 `server/app/main.py` 注册新增组织、CLI 授权和事务 router，初始化新增表/索引；具体模块名以合并后的代码为准。不要二次添加 `/recruitment` 或重复 `/api/v1`。静态 `/applications/drafts`、`/applications/validate` 必须不被动态 `{id}` 抢占。
2. 所有业务入口统一调用 `app.org_auth.require_org_identity(request, scope=None, web_only=False)`；内部 `OrgIdentity` 含 `student_id/role/principal_id/auth_type/scopes`。member/admin 才有组织权限，applicant 拒绝。
3. 网页管理操作要求 web-only 与当前 admin；CLI scope 不授予 review，旧管理员 session 的 kind 不能覆盖当前降级角色。
4. 对齐授权码签发、一次性交换、grant 分页/撤销四个固定接口。不要给一次性 code 流程加“OAuth 已完成”的名称。
5. 对齐项目/知识/表单/draft/validate/confirmation/提交/本人查询/admin review。提交 body 仅 `draft_id + confirmation`；draft/validate 对 CLI 和网页均不隐式签发确认 token。
6. 对照运行时 `/openapi.json` 核验 [人工 skeleton](../contracts/openapi-skeleton.yaml) 的精确字段、状态码、分页、错误格式、时间格式、确认 payload 与 TTL。人工契约不应声称不存在的路径或 schema。
7. 对齐 CLI 实际包路径、安装命令、base URL 是服务根还是含 API 前缀、凭据位置、浏览器确认链接和 stdio 启动命令；在运行手册补充实际可执行示例，不把旧命令草案当验证证据。
8. 确认知识同步是本地受控来源、显式可见级别、无请求内外网爬取；摘要/原文/分页同步应用 ACL。同步脚本的真实文件名、参数与来源约束要和运行手册一致。
9. 做增量迁移及旧 schema 回归；核查数据回收逻辑不会把组织事务当旧招新报名清除。明确新数据的备份集合与回滚兼容性。
10. 运行标准全量测试及新增 `test_m31` 之后的独立用例，补真实 HTTP 无模型闭环、CLI/MCP 本地协议测试，填写实际命令/结果，不提前写数字。

## 必须保留的安全边界

- 身份和权限每次服务端验证，不能靠 Skill、prompt、CLI `--confirm` 或“我是管理员”的文字。
- 用户在可信网页审阅草稿后才获得短期、一次性且内容绑定的确认；变更内容需重新确认。CLI/MCP 不提供确认签发与 review 工具。
- 幂等重放和 token 消费必须在事务中协调，重试不重复提交；跨用户幂等键不能复用结果。
- 不透明 token、code、确认 token 不进源码、URL、普通日志、模型上下文。CLI 的 token exchange 响应不是普通可转发业务 JSON。
- restricted 招新资料、评审、成绩、联系方式不进入通用搜索/CLI/MCP；无权访问不能靠详情错误泄露内容。
- 不提供万能 SQL、服务端 shell、任意 URL 抓取、成员全量导出工具。

## 完成交接格式

提交集成结果时分别写明：变更文件、实际 API/CLI 命令、数据迁移、自动测试命令及真实结果、未执行/失败项、人工验证证据、回滚方法、未决决策 ID。代码测试、真实浏览器、Windows/Linux、两种真实 Agent、硬件压测、备份恢复、生产部署分别标注状态。

本地 HTTP 或 MCP 协议测试通过不意味着真实 Agent Host、生产 TLS/反代、N5095/4GB 容量或恢复已验证。OAuth/OIDC、Gitea、远程 MCP、官网内置模型不在本次交付。生产操作必须由具备授权的人在审批后执行；主代理负责最终注册、跨模块集成、测试及提交/PR，子模块不自行发布。
