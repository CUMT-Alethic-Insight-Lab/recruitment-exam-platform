# 10 · MVP 决策登记与未决事项

> “本次固定”指实现契约，不表示生产已批准或运行已验证。不得用 mock、测试目录或开发默认值代替真实组织制度。

## 本次固定的实现决策

| ID | 决策 | 边界与理由 |
|---|---|---|
| D01 | 网页沿用现有不透明 Bearer session；CLI 使用独立短时 grant | 一次性 `request_id + code` 交换，不是 OAuth/OIDC；网页 token 不交 CLI/MCP |
| D02 | 当前 `account.role` 的 member/admin 具有组织成员权限 | applicant 拒绝；逐请求读取当前角色，不信任 session 旧快照；项目/文档仍需资源可见性检查 |
| D03 | 应用内部 `/api/v1`；公网预期 `/recruitment/api/v1` | 反代剥离前缀；旧 `/api/*` 与 exam_kit 合约保留，不能全局改名 |
| D04 | 首个事务只做项目加入 `project-join` | 服务器持久草稿和 schema revision；不宣称设备库存、借用流程或通用表单均已实现 |
| D05 | 可信网页签发内容绑定的一次性确认，网页当前 admin 审核 | draft/validate 不返回确认 token；CLI 不因 admin 账号得到 review 权限；正式审批责任仍由组织确认 |
| D07 | 只索引批准的公开/成员低敏资料 | restricted 招新档案、成绩、评语、联系方式和私有代码默认排除；摘要与原文同 ACL |
| D09 | 沿用 FastAPI/Pydantic/SQLite/vanilla JS | 模块化增量，不新增身份库或部署微服务；CLI/MCP 是适配层 |
| D12 | 首版本地 stdio MCP，不开远程 MCP | 没有 OAuth discovery/远程传输/真实 Host 兼容性的交付声明；服务端才是授权边界 |
| D13 | 申请提交 `draft_id + confirmation`，要求幂等头 | 不允许旧草案 `fields` 直提；主体+操作+请求摘要绑定幂等，确认与提交原子处理 |
| D14 | 列表统一 `{items,next_cursor}` | 项目、知识搜索、申请和 grant 使用有界分页；本次不包含全量成员导出 |

身份集成约定：`from app.org_auth import OrgIdentity, require_org_identity`；调用 `require_org_identity(request, scope=None, web_only=False)`；返回 `student_id, role, principal_id, auth_type ('web'/'cli'), scopes (frozenset)`。网页管理操作还需 `web_only=True` 且 `role == 'admin'`。这不是向外部响应暴露学号的授权。

## 仍需负责人确认/验证

| ID | 未决事项 | 不得擅自假定 | 阻塞范围 |
|---|---|---|---|
| D02-ops | 正式成员变更、退出及权限维护责任人 | 当前 role 的实现规则不替代组织成员制度 | 真实成员运营 |
| D03-ops | 新 `/api/v1` 在混合云/lab 中的写入路由、edge 故障策略 | 旧机试暂存转发不能自动用于新组织事务；edge 不得独立签发权威授权/确认/申请 | 生产发布 |
| D05-ops | 项目加入的正式条件与审批责任人 | 开发示例不是正式制度；admin 是技术门禁，不证明每位 admin 有组织业务授权 | 真实项目开放 |
| D06 | Gitea 是否存在、逐仓权威源及同步许可 | 未安装/未核实；本次知识同步不是 GitHub/Gitea 全仓镜像 | 仓库集成 |
| D07-ops | 可给外部模型的成员资料目录与来源许可 | 内部资料不自动允许发往个人模型；文档中的提示不是权限规则 | 真实知识导入 |
| D08 | Windows/Linux 和两种真实 Agent 的具体版本与实机结果 | Python/协议测试不等于跨平台安装或真实 Host 已验证 | 客户端验收 |
| D10 | 离机备份目标、密钥管理、恢复责任、RPO/RTO | 代码目录、exam archive、同盘备份不等于组织事务可恢复 | 生产上线 |
| D11 | 是否建设官网模型及预算 | 不影响本次无模型闭环；不承诺固定人均服务次数 | 可选模型阶段 |
| D12-ops | 授权/确认 TTL、审计保留、日志访问与限流部署 | 文档不预填未经实现/实测的安全参数 | 最终集成与运维审核 |

## 决策更新规则

后续变更必须记录负责人、日期、依据、受影响 API、兼容/数据迁移与回滚策略。固定本地契约不授权生产配置变更；真实数据、远程接口、身份委托、数据公开范围变更必须先人工审查。详见 [验收](09-roadmap-and-acceptance.md) 与 [交接](11-agent-handoff.md)。
