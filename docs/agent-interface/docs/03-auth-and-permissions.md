# 03 · 认证、授权、令牌与 CLI 登录

## 基本原则

- 组织官网既有认证应尽可能复用，**先审计其实现，再决定如何扩展到 CLI**；不能仅凭“有网页登录”假设其已经是 OAuth 授权服务器。
- 身份认证 Authentication、API 授权 Authorization、成员资格 Membership、具体资源 ACL **四件事分开判断**。
- 个人 Agent 代表成员；服务端校验用户权限与**该客户端被授予的 scope 交集**，再校验资源级权限。
- Token 不进入 LLM 上下文，不作为 CLI 标准输出，不写入 Git/明文配置/日志。

## 推荐认证路径（首选）

**本地浏览器 CLI：OAuth 2.0 Authorization Code + PKCE (S256)。** 由认证服务签发短时访问令牌，CLI 使用操作系统安全凭据存储管理可更新的会话信息，`auth logout` 撤销授权。

实施选择：

- 若现有官网已有成熟 OIDC/OAuth Provider，直接与之集成并做 API audience、member status、scope 的检查。
- 若官网只有 Session Cookie，**不要直接把网站 Session Cookie 复制给 CLI**；当前代码实际使用的是不透明 Bearer session，同样不得复用给 CLI。使用经过审计的授权服务器组件/服务实现 CLI 委托认证，或在明确威胁模型下先使用后台签发的短期、低权限、可撤销个人访问令牌作为过渡。过渡令牌只能经交互式网页签发、默认只读并有到期、客户端/设备标识与 audience；不得把永久静态 Token 当成长期解决方案。
- 截至现状核实，现有 `/api/auth/*` 不是 OAuth/OIDC Provider，token 无 scope/client_id/audience；网页 session 与 CLI/MCP grant 必须隔离。详见 [00 · 现有系统核实与方案评审](00-current-system-review.md)。
- 无浏览器环境可评估 Device Authorization Grant，但仅在认证服务器支持、风险可控时采用；默认不做必选项。

参照：RFC 9700、RFC 8252、RFC 7636、RFC 8628。应遵守 PKCE、state 验证、受限 redirect URI、短生命周期和刷新令牌轮换/撤销策略；不启用 implicit/password grant。

## 授权状态计算

```text
有效请求 = 认证成功
        AND 账号状态有效（在职/在社/未禁用）
        AND 客户端被授予 action scope
        AND 成员本人具备 action permission
        AND 资源 policy（项目/申请/文档）允许
        AND 额外确认/审批条件满足
```

### 初版 Scope 草案

| Scope | 能力 | 初始默认 |
|---|---|---|
| `profile:read:self` | 查看本人基本身份 | CLI 允许 |
| `knowledge:read` | 查看成员可见知识 | CLI 可申请 |
| `projects:read` | 查看自己有权访问的项目与招募 | CLI 可申请 |
| `applications:read:self` | 查看自己申请 | CLI 可申请 |
| `applications:create` | 创建自己申请/草稿 | 写授权+预览 |
| `resources:read` | 查看授权可见的资源目录 | CLI 可申请 |
| `applications:review:assigned` | 审核明确分配自己的申请 | 审批人单独授权，非默认 |
| `projects:manage:assigned` | 修改明确管理的项目元数据 | 负责人单独授权，非 MVP 必需 |

**Scope 授权≠最终数据权限。** `projects:read` 不能让用户看到任何项目的私有材料；`applications:read:self` 不能通过递增 id 枚举他人申请。

## 高风险操作

- 创建申请：必须让成员看到目标、字段、附件、声明、将通知的对象，再进行确认。
- 审核/人员归属/成员停用/密钥操作：不交给普通 Agent 自动执行。需要更严格的重新验证、角色校验、日志和必要时双人确认。
- 所有变更 API 提供 `Idempotency-Key`，避免工具重试造成重复提交。
- 服务端拒绝模型自行声称“已获得用户同意”；确认必须由真实客户端交互产生的显式动作，必要时使用短期一次性确认令牌。服务端不能区分 `--confirm` 是人输入还是 Agent 调用，因此 Agent 场景首版在可信网页最终确认；确认令牌须绑定用户、客户端、操作与内容摘要。

## CLI 要求

```bash
alethic auth login
alethic auth status --json
alethic auth scopes --json
alethic auth logout
```

- 不打印 bearer token；CLI 日志默认脱敏。
- 优先 OS Keychain/libsecret/Secret Service/Windows Credential Manager 等；Linux headless 回退方案须限制本地文件权限 `0600`、加密或使用短期 token，并明确警告。
- 具备多设备撤销和管理员撤销能力。
- 对非交互式脚本提供 `--non-interactive`，过期凭据返回稳定的 `AUTH_REQUIRED`，而不是卡在网页登录。

## 身份变更

成员退出组织/撤销权限/角色变化后，历史令牌不得继续获得已撤销权限。可采取较短 access token 生命周期 + 每次请求/缓存短 TTL 的成员状态校验或服务端 token introspection，提供紧急撤销机制。具体实现基于当前身份服务能力决定。
