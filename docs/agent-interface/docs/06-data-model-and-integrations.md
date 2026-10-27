# 06 · 数据实体、生命周期与 GitHub / Gitea / 文档集成

## MVP 关系模型（逻辑）

```text
Member(id, external_subject, status, display_name, created_at)
RoleAssignment(id, member_id, role, scope_type, scope_id, valid_from, valid_until)
Project(id, slug, title, description, lifecycle_status, visibility,
        owner_member_id, updated_at)
ProjectMembership(id, project_id, member_id, project_role, status)
Resource(id, category, title, visibility, availability_status, owner_id)
Application(id, kind, applicant_id, project_id?, status, payload_json,
            form_revision, submitted_at, updated_at)
ApplicationReview(id, application_id, reviewer_id, decision, reason, decided_at)
FormDefinition(id, kind, revision, schema_json, visibility, policy_ref)
DocumentIndex(id, source_repo, path, revision, visibility,
              source_url, indexed_at, updated_at)
RepoBinding(id, project_id, provider, repo_external_id, canonical, visibility,
            last_synced_at)
AuditEvent(id, principal_id, client_id, action, object_type, object_id,
           event_at, request_id, outcome, fields_redacted)
ClientGrant(id, principal_id, client_id, scopes, expiry, revoked_at)
```

这里只是**逻辑契约**，不是要求一次建立所有实体表；能复用现有官网用户表就不要再建独立身份源。`Member` 映射现有 `account`，内部可继续以学号为主键，但 API 默认只暴露最小身份字段；`external_subject` 仅在未来接入外部身份源时使用。这里的 `Application` 是项目加入/资源等**事务申请**，不是现有招新 `application` 表；实现时建议使用 `ServiceRequest`/`service_request` 消除代码与数据库命名歧义，禁止借此读取招新评审资料。

## 生命周期语义

### 项目阶段（建议，不要求 MVP 一次启用所有）

`idea → proposed → approved → recruiting → active → paused/completed/archived`

- `paused` 和 `active` 可受控相互转换；`archived` 只读。
- 项目成员可见范围与阶段不是同一维度。
- GitHub 是否有 Issues 不自动代表项目处于 `active`；状态由业务服务维护。

### 申请阶段（MVP）

`draft → submitted → in_review → approved/rejected/needs_information/withdrawn`

状态定义及合法转移详见 04。审批是明确的组织权限，不受 Agent 自行生成文本影响。

## 文档的类型与敏感度

- `public`：公开官网、公开制度、公开学习路线；可以被公开访问和引用。
- `member`：全体认证成员可见的会议安排、项目参与说明等。
- `project`：指定项目成员/负责人可见的信息。
- `restricted`：审批人或管理者限定的个人信息、内部评估与敏感记录；**默认不允许向个人外部 Agent 批量返回**，需明确政策与必要性。

所有文档记录带 `source_url`、`revision`、`updated_at`、`access_level`、`owner`、`effective_at?`。检索索引和原文要执行同等授权过滤，不能检索返回截断后的隐私文字片段。

## GitHub + Gitea

- 每个 RepoBinding 只有一个 `canonical=true` 的代码权威来源；另一个可以是镜像或独立的发布仓。
- 公开项目：常见方案是 GitHub 主仓、Gitea 只读拉取镜像；内部项目：Gitea 主仓，公开内容通过明确审核后**选择性发布**，不对全部私有 refs 做自动推送。
- Gitea 镜像主要同步 Git commits、branches、tags，不等于双向同步权限、Issues/PR/审计。推送镜像需要确认覆盖风险。
- 平台首版只读拉取必要仓库元数据：`repo_url`、默认分支、最后更新、Issue/PR 摘要（若授权）、CI 状态（若授权）。不在平台重复管理全部 PR。
- Webhook 优先事件驱动，周期性同步作低频修复；签名校验、幂等消费、失败重试，不允许无界入队。
- 若 GitHub 和 Gitea 无法访问，则相关字段显示 `unavailable/stale`，不应隐瞒信息时间差。

## 数据最小化与索引

- 默认 PostgreSQL/SQLite 二选一，视现有官网 DB 决定；不要为了组织 Agent 建立完全独立的成员数据库。
- 先用关系型数据库全文搜索或静态索引，不需要重型 Embedding/RAG 基础设施。
- 不索引历史私聊、私人评价或完整私有项目代码，除非经专门的权限与数据处理审批。
- 成员资料仅保留业务必要字段；不要把“兴趣/技能/时间安排”转化为隐性绩效评价体系。

## 同步策略

1. 识别源、访问凭证、last_synced_at 和被授权资源。
2. 只同步必要字段并保留 `source_ref`；执行前后校验外部请求速率。
3. 同步失败可重试，显示 stale 状态，并保留最近一次成功快照用于展示（仍受 ACL 控制）。
4. 冲突以唯一权威数据源为准；不要由 LLM 自动裁决双主冲突。
