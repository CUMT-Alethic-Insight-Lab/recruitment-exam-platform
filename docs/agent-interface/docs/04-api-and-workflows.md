# 04 · REST API 契约与业务流程

> 以下 endpoints 是设计提案，不代表已上线。实现前必须对照现有官网 API，并让 OpenAPI 定义与实际返回一致。

## 约定

- HTTPS，统一版本前缀 `/api/v1`；`Content-Type: application/json`；时间 ISO-8601 UTC 带时区，显示由客户端本地化。
- 身份由 Bearer Token 或经过 CSRF 防护的同域网站 Session 确认；敏感接口不允许匿名。
- 列表带 `limit` 和 `cursor`；默认有上限（如 50），禁止无限读取。
- 关键对象使用不易枚举的 `id`；**不可仅凭难猜 ID 代替鉴权**。
- 每次响应带 `request_id`；可能为缓存数据的资源带 `updated_at`、`source_ref`、`last_synced_at`。
- 只读响应可使用 ETag；错误遵循统一结构。

## 第一批资源和接口

| Method | Path | 功能 | 核心权限 |
|---|---|---|---|
| GET | `/me` | 当前主体身份（最小公开字段） | `profile:read:self` |
| GET | `/capabilities` | 查询当前用户可用的能力与 API/Skill 索引 | 已认证 |
| GET | `/knowledge/search?q=...` | 搜索允许读取的文档片段与来源 | `knowledge:read` |
| GET | `/knowledge/documents/{id}` | 获取一份授权文档或摘要，带版本 | `knowledge:read` + ACL |
| GET | `/projects?recruiting=true` | 项目列表及状态 | `projects:read` + ACL |
| GET | `/projects/{id}` | 项目详情/相关 URL/负责人联系方式策略 | `projects:read` + ACL |
| GET | `/forms` | 当前可申请的表单类型 | 已认证 |
| GET | `/forms/{kind}/schema` | 表单字段、验证、审批人规则说明 | 对应业务权限 |
| POST | `/applications/drafts` | 创建/更新待确认草稿 | `applications:create` |
| POST | `/applications/validate` | 无副作用校验草稿，供 `--dry-run` 使用 | `applications:create` |
| POST | `/applications` | 显式提交申请 | `applications:create` |
| GET | `/applications?mine=true` | 当前用户的申请列表 | `applications:read:self` |
| GET | `/applications/{id}` | 查询具体申请 | 归属/审批人 ACL |
| GET | `/resources` | 仅授权资源的目录及状态 | `resources:read` |
| POST | `/applications/{id}/review` | 审批人的决定 | `applications:review:assigned` + 明确确认 |

草稿接口也可做为表单客户端本地草稿，但要让用户跨设备继续时才需要服务端持久化；若只需字段补全，初版可以只在客户端临时保存。避免一开始过度设计草稿数据库。

## 示例：申请设备

### 查询 Schema

```http
GET /api/v1/forms/equipment-borrow/schema
Authorization: Bearer <REDACTED>
```

```json
{
  "kind": "equipment-borrow",
  "revision": "1",
  "fields": [
    {"name": "resource_id", "type": "string", "required": true},
    {"name": "project_id", "type": "string", "required": false},
    {"name": "purpose", "type": "string", "required": true, "min_length": 10},
    {"name": "from_at", "type": "datetime", "required": true},
    {"name": "until_at", "type": "datetime", "required": true}
  ],
  "source_ref": "policy://equipment-borrow",
  "updated_at": "2026-10-27T00:00:00Z"
}
```

> 日期、表单和文档链接仅为示意；平台不得假装当前真有库存或设备借用制度。

### 提交（需用户确认）

```http
POST /api/v1/applications
Authorization: Bearer <REDACTED>
Idempotency-Key: <client-generated-uuid>
Content-Type: application/json
```

```json
{
  "kind": "equipment-borrow",
  "form_revision": "1",
  "fields": {
    "resource_id": "demo-dev-board-01",
    "purpose": "验证小型视觉模型在开发板的推理延迟与内存占用",
    "from_at": "2026-11-02T01:00:00Z",
    "until_at": "2026-11-09T01:00:00Z"
  },
  "confirmation": {"method": "interactive", "confirmation_token": "<one-time-short-lived-proof>"}
}
```

```json
{
  "id": "app_demo_a1",
  "status": "submitted",
  "submitted_at": "2026-10-27T02:00:00Z",
  "next_action": "await_reviewer",
  "request_id": "req_demo_01"
}
```

**注意：** `confirmation_token` 仅表示推荐的一次性确认协议设计。必须由服务器签发、与身份及申请内容绑定、有效期短且单次使用，不能允许 Agent 自行生成一个字符串表示授权；MVP 也可以让用户在可信网页中完成最终提交。

## 状态机：申请

```text
draft -> submitted -> in_review -> approved | rejected
                   \-> needs_information -> submitted
           submitted/in_review -> withdrawn (若制度允许)
```

任何状态转换都由服务端验证：例如只有原申请人可撤回可撤回状态，只有指派审批人可批准，不允许 `approved -> draft`。日志记录旧状态/新状态/主体/时间/原因。

## 错误格式

```json
{
  "error": {
    "code": "INSUFFICIENT_SCOPE",
    "message": "This action is not authorized for this client.",
    "details": {"required_scope": "applications:create"},
    "retryable": false
  },
  "request_id": "req_..."
}
```

标准错误代码草案：`AUTH_REQUIRED` (401)、`MEMBERSHIP_INACTIVE` (403)、`INSUFFICIENT_SCOPE` (403)、`RESOURCE_FORBIDDEN` (403)、`RESOURCE_NOT_FOUND` (404)、`VALIDATION_FAILED` (422)、`CONFIRMATION_REQUIRED` (409/428，需统一)、`STATE_CONFLICT` (409)、`RATE_LIMITED` (429)、`UPSTREAM_UNAVAILABLE` (503)。对于私有资源，未经授权时可统一返回 404 以避免泄露其存在。

## 重要性高于端点数量的契约

1. OpenAPI/服务端/CLI 三者一致，禁止“文档自称支持但实现缺失”。
2. 列表与详情均做行级授权，项目成员只看允许的字段。
3. 写操作使用原子事务及幂等键，返回明确对象 ID；网络失败后可通过幂等键查重。
4. 读取文档必须带权威来源、版本和更新时间，引用优先返回 URL+锚点。
5. 没有真正的数据源时不要预设已在线的“可借开发板”；使用空列表和“未配置”。
6. 对需要人工审核的申请，响应状态是 `submitted`，不能写成 `approved`。
7. 搜索支持可信全文索引（数据库 FTS/轻量检索优先），没有必要一开始上 RAG 或向量 DB。
