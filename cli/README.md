# Alethic CLI (MVP)

独立 Python 3.10+ 客户端，运行时只使用标准库；不会复用网页 Bearer session。未实现 MCP、刷新令牌、管理员审批或 CLI 最终提交。

```sh
python -m pip install ./cli
alethic auth login
alethic auth status --json
alethic auth scopes --json
alethic capabilities --json
alethic projects list --recruiting --limit 20 --json
alethic projects get PROJECT_ID --json
alethic knowledge search '项目加入' --json
alethic knowledge get DOCUMENT_ID --json
alethic forms schema project-join --json
alethic applications list --mine --limit 20 --json
alethic applications get APPLICATION_ID --json
alethic applications draft --file application.json --json
alethic applications submit --file application.json --dry-run --json
alethic applications submit --file application.json --json
alethic auth logout --json
```

未安装时可在 `cli/` 下运行 `python -m alethic_cli ...`。分页使用 `--limit 1..50 --cursor ...`，申请列表支持 `--project ID`。只读取一页，保持服务端 `{items,next_cursor}` 契约。

## 登录与本地凭据

登录生成随机 `request_id`，打开 `{base}/org?cli_request=REQUEST_ID&scopes=...`。请仅在可信网页中使用现有网页登录；CLI **不索取网页 token、密码或 confirmation_token**。网页完成授权后，把一次性 code 输入 CLI 隐藏提示；CLI 通过 `POST /api/v1/auth/cli/token`（`request_id,code`）交换短时凭据。默认申请只读 scopes；要准备申请可用 `alethic auth login --scope profile:read:self --scope applications:create`，重复 `--scope` 明确列出所需权限。服务端决定最终权限。

`--non-interactive` 或非 TTY 登录返回 `AUTH_REQUIRED`（3）和授权 URL，不打开浏览器、不等待输入。其他命令从不自动登录。过期/缺失/来源不匹配的凭据稳定返回 `AUTH_REQUIRED`。

默认 base 为 `https://alethicinsight.com/recruitment`，设置 `ALETHIC_API_BASE_URL` 可覆盖（兼容 `ALETHIC_BASE_URL`）；base **不含** `/api/v1`。HTTPS 必需，只有 loopback mock 可用 HTTP；禁用 HTTP 跳转，防止 Bearer 被转发到其他地址。

短时 token 只存在本地凭据文件，不输出到 stdout/stderr：

- Windows：`%LOCALAPPDATA%/Alethic/credentials.json`，使用 `icacls` 去继承并只授权当前 Windows 用户；ACL 设置失败则不保存 token。
- POSIX：`${XDG_CONFIG_HOME:-~/.config}/alethic/credentials.json`，文件 `0600`，专用目录 `0700`。
- `ALETHIC_CONFIG_DIR` 可覆盖专用目录，测试必须使用临时目录；勿指定项目目录或共享目录。凭据是短时明文文件，不是系统钥匙串；不要备份、同步或提交。

`auth status` 实际请求 `/me`；`auth scopes` 显示本地授权 scope 快照（不是服务端当前权限证明）。`logout` 至少删除本地凭据；由于 grant 撤销端点限定网页会话，CLI 不冒用该接口。命令明确输出 `server_revoked:false` 并提示在可信网页撤销，或等待过期。

## 申请边界

输入文件原样交给服务端 schema 校验，例如（实际 revision/project 以接口为准）：

```json
{"kind":"project-join","form_revision":"1","fields":{"project_id":"PROJECT_ID","motivation":"说明希望参与的具体工作与可验证贡献","weekly_hours":4,"notes":""}}
```

`submit --dry-run` 只调用 `/applications/validate`，不保存草稿、不提交。`draft` 调 `/applications/drafts`，成功返回草稿和可信网页 URL。`submit` 创建草稿后以 `CONFIRMATION_REQUIRED`（7）返回 `error.draft`、`error.confirmation_url`、`error.submitted:false`；必须由用户在该网页核对并完成最终提交。

可信 URL 固定由已配置的 base 构造为 `/org?draft_id=...`，不采用服务端任意跳转 URL。草稿响应支持 `id` 或 `draft_id`。CLI 从不调用 confirmations 或最终 `POST /applications`，没有 `--confirm` 参数，不读取/输出一次性 confirmation token；包含 confirmation/token 字段的申请文件会被拒绝。

网页集成契约：`/org` 需要识别 `cli_request`（授权请求标识）、`scopes`（逗号分隔的权限申请，仅作为用户核对提示，不能静默授权）和 `draft_id`（读取归属当前成员的草稿供核对；不能自动确认/提交）。这些网页行为不由 CLI 包实现，完整浏览器闭环需要与网页模块一起验收。

## 输出与退出码

全局选项 `--json` / `--non-interactive` 可放在命令前后。`--json` 时 stdout 严格一个 JSON 值（帮助 `--help` 除外）；浏览器指引、警告只在 stderr。错误使用 `{"error":{"code":"...","message":"..."}}`。不会原样回显服务端错误正文，以免泄漏凭据或个人字段。

退出码：0 成功；2 参数错误；3 未认证/过期；4 无权限或不可见资源；5 网络/API 不可用（含限流）；6 输入/服务端校验失败；7 冲突或需要可信网页确认。`--confirm` 是参数错误（2），不是同意证明。

本地验证（不访问线上）：

```sh
python -m pytest server/tests/test_m35_org_cli.py -q
```
