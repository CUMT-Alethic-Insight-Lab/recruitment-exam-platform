# 组织接口 MVP：本地运行与人工运维手册

> 本次交付是现有招新平台的增量代码/契约，不表示生产已部署。所有生产发布、知识导入、权限变更、备份和恢复须由有权限的人另行执行；本次文档工作未连接生产、读取密钥或操作真实成员数据。测试结果由主代理按实际执行补充，不预填数字。

- [API 与工作流](agent-interface/docs/04-api-and-workflows.md)
- [OpenAPI 契约](agent-interface/contracts/openapi-skeleton.yaml)
- [验收与事实边界](agent-interface/docs/09-roadmap-and-acceptance.md)
- [决策/未决运维事项](agent-interface/docs/10-decision-register.md)
- [集成交接](agent-interface/docs/11-agent-handoff.md)
- [旧部署说明](../server/deploy/README.md)（历史操作说明，不是本次部署授权）

## 1. 范围与路径

应用内部新路由为 `/api/v1`，本地根地址 `http://127.0.0.1:8901`；公网预期根地址 `https://<host>/recruitment`，反代 strip 后到相同内部路由。`PUBLIC_PREFIX=/recruitment` 用于网页链接，不给 APIRouter 再加此前缀。CLI base 是服务根，**不含 `/api/v1`**。

保留旧 `/api/auth/*`、`/api/campaigns/*`、招新报名/机试/评审和 exam_kit，不将其数据暴露到组织知识接口。组织事务与旧招新 application 分开。网页沿用 Bearer session；CLI 是一次性 code 交换的独立短时 grant，不是 OAuth/OIDC。只有当前 member/admin 可用组织接口；网页管理还要当前 admin，CLI 不授予 review。

本次不交付 Gitea、远程 MCP、官网内置模型或真实 Agent 兼容性认证。其他方案文件里的设备借用、`--confirm` 和通用表单示例不是本次可执行命令。

## 2. 隔离本地运行

下面从仓库根在 Git Bash/Linux shell 运行。只使用新建的开发数据目录，不指向 `server/data`、生产镜像或备份；环境变量必须在导入 `app.config` 前设置。

```sh
python -m venv .venv
# Git Bash / Windows
source .venv/Scripts/activate
# Linux 改用 source .venv/bin/activate
python -m pip install -r server/requirements.txt
export ALE_THIC_DATA_DIR="$PWD/.local-agent-mvp"
export ALE_THIC_DB_PATH="$ALE_THIC_DATA_DIR/alethic_insight.db"
export ALE_THIC_ROLE=authoritative
export PUBLIC_PREFIX=''
export ALE_THIC_CAPTCHA_ENABLED=0
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python -m uvicorn app.main:app --app-dir server --host 127.0.0.1 --port 8901
```

目录由配置初始化。`SECRET_KEY` 示例是当前 shell 的临时开发值，不落盘、不打印；不要采用开发默认值上线。本地初始化可能播种管理员，初始密码必须先改；测试成员请使用本地管理流程建立，不复制真实成员。不要提交 `.local-agent-mvp`、本地凭据或日志；提交前检查 `git status`。

在另一个终端检查 `http://127.0.0.1:8901/api/health` 和 `/openapi.json`。组织网页为 `/org`，先完成已有网页登录与密码变更。

### 注册与初始化位置

`server/app/main.py` 已注册 `org_auth`、`org_meta`、`org_content`、`org_requests` 和 `/org`，新路由均在 `/api/v1`。`db.init_db()` 负责幂等增量建表，不重建旧数据。`/org` 响应禁止 iframe 嵌套，组织错误使用统一 envelope 和 request_id，旧 `/api` 保持原结构。

authoritative/edge 必须保持单一权威：旧 edge 机试暂存转发只适用于旧机试；组织授权、principal、草稿、确认、提交、review 和知识同步均不得在 edge 独立落库。云端 vhost 将 `/recruitment/api/v1/` 全部回源 lab 且不配置热备回落，edge 应用层同时拒写。

## 3. CLI 安装与登录

需要 Python 3.10+；从仓库根，在本地 venv 安装独立客户端：

```sh
python -m pip install ./cli
export ALETHIC_API_BASE_URL=http://127.0.0.1:8901
alethic --help
alethic auth login
alethic auth status --json
alethic projects list --recruiting --limit 20 --json
alethic knowledge search '成员' --json
alethic forms schema project-join --json
alethic applications list --mine --limit 20 --json
```

参见 [CLI 自带说明](../cli/README.md)。CLI 生成随机 request_id，打开可信 `/org?cli_request=...`，用户在网页核对 scopes 后取一次性 code，并在 CLI 隐藏输入提示中输入。不得向 Agent 粘贴网页 token、密码、code、CLI token 或确认 token。默认 scopes 只读；要准备草稿需重新明确授权 `applications:create`，例如：

```sh
alethic auth login --scope profile:read:self --scope applications:create --scope applications:read:self --scope projects:read
```

先读表单，再准备本地脱敏 JSON（project_id 与 revision 以实际接口为准）：

```json
{"kind":"project-join","form_revision":"1","fields":{"project_id":"LOCAL_TEST_PROJECT_ID","motivation":"希望参与本地测试项目并验证加入流程","weekly_hours":4,"notes":"仅本地演练"}}
```

```sh
alethic applications submit --file application.json --dry-run --json
alethic applications draft --file application.json --json
```

`--dry-run` 只服务端 validate；draft 持久化但不提交。CLI 的 `applications submit` 也只准备草稿并返回需要可信网页确认（退出码 7），没有 `--confirm`、最终提交或 review。用户必须在 `/org?draft_id=...` 重新核对并确认，网页才取得短时 token 并提交。REST 最终提交 body 是 `draft_id + confirmation` 字符串，不是 `fields` 或旧版嵌套 `confirmation_token`。

分页只取一页，下一页传 `--cursor`，CLI 页大小目前为 1..50。JSON 输出与日志分离；错误退出码见 CLI 说明。`auth logout` 清理本地凭据，不等于服务端撤销；应到可信网页撤销 grant，或等待过期。凭据默认位于 Windows `%LOCALAPPDATA%/Alethic/credentials.json` 或 POSIX `~/.config/alethic/credentials.json`（可受 XDG 配置影响）；不要读取/复制该文件，不放入代码库、共享目录或备份。

## 4. 本地 stdio MCP 安装

在已安装 CLI、完成授权的同一用户环境：

```sh
python -m pip install ./mcp_adapter
alethic-mcp --help
```

MCP Host 的本地 stdio 配置示例（实际配置文件结构取决于 Host；路径需换为本地 venv 的绝对路径）：

```json
{
  "mcpServers": {
    "alethic": {
      "command": "/absolute/path/to/venv/bin/alethic-mcp",
      "args": ["--cli", "/absolute/path/to/venv/bin/alethic"],
      "env": {"ALETHIC_API_BASE_URL": "http://127.0.0.1:8901"}
    }
  }
}
```

Windows 对应 `Scripts/alethic-mcp.exe` 和 `Scripts/alethic.exe`。这里不在环境参数写 token；适配器通过同一用户 CLI 凭据调用 API。不要将非可信程序或 shell 字符串作为 `--cli`。不要把它启动为远程 HTTP 服务；stdio 的 stdout 专供 JSON-RPC，不手工混入日志。工具只允许发现/读取和准备待网页确认草稿，不包含确认签发、最终提交、review、SQL、shell 或任意 URL 抓取。

本地协议自动测试不等于实际 MCP Host 接入成功，也不代表 MCP 所有版本均兼容。支持版本和参数以 `mcp_adapter/alethic_mcp/server.py`、`--help` 及实际握手为准；真实客户端名称/版本/结果需另行记录。

## 5. 受控知识同步

同步只读取人工准备好的本地公开来源 checkout，不在 HTTP 请求内联网爬取，不读取私有仓和生产文件。脚本 [sync_org_knowledge.py](../server/scripts/sync_org_knowledge.py) 目前只允许 `governance-public`，默认列举成员制度、章程、AI 辅助政策；这些路径必须在选定 revision 真实存在并经人工确认许可。建议使用已审核的完整 commit hash 固定来源。

在服务已对隔离数据库完成初始化后、保持第 2 节相同数据环境，执行：

```sh
python server/scripts/sync_org_knowledge.py --help
python server/scripts/sync_org_knowledge.py --local-dir /path/to/reviewed/governance-public --revision FULL_40_HEX_COMMIT
# 如需覆盖默认名单，人工审查后增加 --allowlist /path/to/approved-manifest.json
```

清单形状：

```json
{"source_repo":"governance-public","documents":[{"path":"docs/zh-CN/membership.md","title":"成员制度","access_level":"public"}]}
```

仅 `public/member`、UTF-8 `.md/.txt`，受数量、体积和路径边界检查；restricted/隐藏文件/路径逃逸拒绝。同步按来源清单 upsert，并删除同来源不再在清单内的记录；**空清单会清空该来源索引**。每次先备份本地目标 DB、审阅清单，失败不应部分写入。revision 缺省时使用内容 SHA-256，链接可能仍指 main；脚本只校验完整 Git revision 的格式，**不会自行验证 checkout 的 remote、commit 或工作树一致性**，操作者必须核对来源。同步仅限 authoritative，edge 会拒绝写入。该同步不等于 GitHub/Gitea 镜像或私有代码索引。完成后用 member/CLI 搜索和文档详情验证来源、版本、可见性；撤除/收窄文档后旧搜索摘要也不得继续可见。

## 6. 测试与验收记录

标准后端命令从 `server/` 运行 `python -m pytest -q`；新增模块测试使用 `test_m31` 以后的独立文件。先运行针对性测试，再由主代理执行全量。文档模块使用 YAML 解析、内部 `$ref`、operationId/路径参数和本地 Markdown 链接检查；正式发布前再与应用生成的 `/openapi.json` 比较。

实际记录（2026-10-27）：

| 证据 | 状态/命令/结果 |
|---|---|
| 文档 YAML 与本地链接 | 通过；OpenAPI 21 个 operation 与运行时路由对照一致，ID 唯一，错误 envelope 一致 |
| 新模块自动测试与全量回归 | `cd server && python -m pytest -q`：**451 passed** |
| 无模型本地 HTTP/CLI/MCP 闭环 | `test_m37_org_live_e2e.py` 通过：真实 uvicorn + 实际 CLI/MCP 源码子进程，合成账号/项目；真实浏览器未验证 |
| CLI/MCP 安装包 | Windows 上两个 wheel 均实际构建成功；Linux、跨平台及全新环境安装待验证 |
| 两种真实 Agent（MCP 与命令行） | 未以协议测试代替，待验证 |
| 目标硬件与容量 | 待测量 |
| 离机备份/隔离恢复 | 待演练 |
| 生产发布/反代/TLS | 配置已入 PR，未执行生产部署、nginx reload 或线上验证 |

## 7. 备份与隔离恢复（人工操作方案）

备份对象不只有代码：SQLite（含旧账号/会话和新 principal/grant、授权码、草稿/确认、事务、幂等、review、审计、项目与文档）、数据目录内的必要上传/清洗件，以及脱离代码库受保护的运行配置。知识来源清单和固定 revision 也要可追溯。原有 exam archive 不是全库备份。

- 有写入时不能只复制 `.db` 而遗漏 WAL。使用 SQLite backup API/`.backup` 得到一致数据库；如需要与上传目录形成跨文件一致快照，先人工停写/停止服务，再一并备份。
- 下面只操作明确指定的**本地演练**库；路径替换为隔离环境，备份文件不得提交仓库。先创建新的备份目录，再运行 Python：

```python
from pathlib import Path
import sqlite3
source = Path(".local-agent-mvp/alethic_insight.db").resolve()
backup = Path("/path/to/new-local-backup/alethic_insight.db").resolve()
assert source.is_file() and not backup.exists()
with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src:
    with sqlite3.connect(backup) as dst:
        src.backup(dst)
        assert dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
```

- 备份含敏感凭据哈希与个人事务，必须加密、限制访问、离机保存并设置保留期；不上传公开仓，不复制 CLI 明文凭据。负责人记录时间、代码/schema 版本、校验摘要、RPO/RTO。
- 恢复演练先停本地测试实例，将备份恢复到**新目录**而非覆盖原库；使用 `ALE_THIC_DATA_DIR/ALE_THIC_DB_PATH` 指向恢复副本，隔离网络后 `PRAGMA integrity_check`，核验表/行数、旧接口和新申请闭环。
- 恢复旧快照可能复活已撤销 grant/会话/一次性 code/确认。恢复后先按已审核的管理步骤无效化这些凭据、要求重新登录/授权；未完成前不开放网络。不能只凭过期时间认为恢复安全。
- 不提供对生产库直接运行的覆盖/DELETE 命令。演练成功才可制定实际恢复窗口、上传目录配套恢复和人员值守方案。

## 8. 发布前与回滚（未执行）

生产部署仍需人工执行。沿用现有 release/current 与 systemd/nginx 流程，但不要直接重跑旧部署说明中的历史身份初始化片段；当前 Bearer session 与环境配置以实际代码和经审查配置为准，不读取或展示私有凭据。

发布前：评审 PR/测试结果 → 备份并记录旧 release → 确认 schema 增量和旧版兼容 → 确认新 API 权威路由及 edge 拒写/回源行为 → 审查 TLS、前缀、页面缓存、授权 TTL/撤销/日志/限流 → 指定维护窗口 → 人工发布 → 使用脱敏账号 smoke test。生产命令由负责人在受控运维渠道执行，本手册不授权自动 deploy。

回滚：先停新写入与授权，记录故障和最后一致点；人工把 `current` 指回已记录的旧 release 并重启，核验旧招新/机试/登录。增量表如旧代码忽略即可保留，**不要为回滚代码直接 DROP 新表**。旧版本不能提供新 API，应同时关闭新网页/CLI 入口并明确告知用户；缓存旧 JS 不能继续诱导提交。

若存在不兼容迁移或库损坏，不能仅切代码。先备份故障态，在维护窗口经批准恢复一致数据库和配套文件，明确恢复点之后的新事务丢失/补录策略；恢复副本先隔离验证并撤销快照凭据，再重新开放。回滚不保证自动保留所有新写入，必须把这一风险和恢复责任人写入发布记录。
