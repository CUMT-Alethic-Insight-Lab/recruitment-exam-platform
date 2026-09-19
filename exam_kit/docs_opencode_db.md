# opencode.db（v1.18.29+ 会话 SQLite 存储）勘察结论

勘察日期：2026-09-05。数据来源：本机真实库 `C:\Users\74090\.local\share\opencode\opencode.db`
（271MB，含 -wal/-shm，用户私人数据）——**只读勘察**：先把 db+wal+shm 三个文件拷到系统临时目录，
打开的是副本，原文件未做任何修改。本机 opencode 为 npm 安装的 1.18.18（该版本已用同一 SQLite 布局，
库内会话 version 字段跨 1.18.3 ~ 1.18.18）。

对应实现：`exam_server.py` 的 `opencode_db_sessions` / `opencode_db_session_doc` /
`opencode_export_session`。本文是后续维护的 schema 事实来源。

## 1. 存储布局

`%USERPROFILE%\.local\share\opencode\` 下：

- `opencode.db` + `opencode.db-wal` + `opencode.db-shm`（WAL 模式；**新版会话全在这**）。
- 旧版 `storage/session/*.json` 分立布局已不存在（目录可能残留但不再写入）。
- 其余：`auth.json`（Zen/kimi 等凭证）、`log/`、`repos/`、`snapshot/`、`tool-output/`。

## 2. 表清单（sqlite_master，v1.18.18 库实测）

业务表：`account`、`account_state`、`control_account`、`credential`、`data_migration`、
`event`、`event_sequence`、`message`、`part`、`permission`、`project`、`project_directory`、
`session`、`session_context_epoch`、`session_input`、`session_message`、`session_share`、
`todo`、`workspace`，另有 drizzle 迁移表 `__drizzle_migrations` / `migration`。

会话链路核心是三张表：**session → message → part**。

## 3. 关键表结构（只列考试相关字段）

### session（会话壳，138 行）

```sql
CREATE TABLE session (
  id text PRIMARY KEY,            -- "ses_..." 前缀
  project_id text NOT NULL,       -- → project.id（本机见 "global" 与 64 位 hex 两种）
  parent_id text,                 -- 子会话（subagent）指向父会话
  slug text NOT NULL,
  directory text NOT NULL,        -- 工作目录，正斜杠形式，如 "P:/AletnicInsightRecruitment"
  title text NOT NULL, version text NOT NULL,
  time_created integer NOT NULL,  -- epoch 毫秒 int
  time_updated integer NOT NULL,  -- epoch 毫秒 int（自动发现按它过滤/排序）
  time_compacting integer, time_archived integer,
  workspace_id text, path text, agent text, model text, metadata text,
  cost real DEFAULT 0, tokens_input/output/reasoning/cache_read/cache_write integer DEFAULT 0,
  summary_additions/deletions/files integer, summary_diffs text,
  revert text, permission text, share_url text,
  FOREIGN KEY (project_id) REFERENCES project(id) ON DELETE CASCADE
)
```

有用的索引：`session_parent_idx`、`session_project_idx`、`session_workspace_idx`。

### message（一条 LLM 消息，4569 行）

```sql
CREATE TABLE message (
  id text PRIMARY KEY,            -- "msg_..." 前缀
  session_id text NOT NULL,       -- → session.id
  time_created integer NOT NULL,  -- epoch 毫秒 int
  time_updated integer NOT NULL,
  data text NOT NULL,             -- JSON：{"role":"user|assistant","time":{"created":ms,"completed":ms},
                                  --   "modelID","providerID","agent","mode","path":{"cwd",...},"cost","tokens",...}
  FOREIGN KEY (session_id) REFERENCES session(id) ON DELETE CASCADE
)
```

索引 `message_session_time_created_id_idx` 支撑按会话+时间取消息。

### part（消息分片，20476 行）

```sql
CREATE TABLE part (
  id text PRIMARY KEY,            -- "prt_..." 前缀
  message_id text NOT NULL,       -- → message.id
  session_id text NOT NULL,       -- 冗余，直接指向 session
  time_created integer NOT NULL,  -- epoch 毫秒 int（data 里没有 time 的分片排序就靠它）
  time_updated integer NOT NULL,
  data text NOT NULL,             -- JSON：{"type":"text|reasoning|tool|step-start|step-finish|patch|...", ...}
  FOREIGN KEY (message_id) REFERENCES message(id) ON DELETE CASCADE
)
```

索引：`part_message_id_id_idx`、`part_session_idx`。

### 其他

- `todo(session_id, position, content, status, priority, ...)`：会话级 todo 列表。
- `session_message`：本库 0 行（1.18.18 未启用，疑似新版本的预留/替代表；取数仍走 message/part）。
- `session_share`、`session_input`、`session_context_epoch`、`event*`：与考试取数无关。

## 4. 时间格式

**一律 epoch 毫秒整数**（列与 data JSON 内均是，如 1788362472414），
与导出 JSON `info.time.created/updated`、`message.info.time.created`、
`part.time.start/end` 同单位，无需换算。

## 5. 与 opencode export 单文件 JSON 的同构关系

`opencode export <sessionID>` 产物（仓库样例 `export_session_example/platform_flow-md.json`）：

```json
{"info": {"id":"ses_...","directory":"P:\\proj","time":{"created":ms,"updated":ms}, ...},
 "messages": [{"info": {"id":"msg_...","sessionID":"ses_...","role":"user","time":{"created":ms}, ...},
               "parts": [{"id":"prt_...","messageID":"msg_...","type":"text","text":"..."}, ...]}]}
```

db 拼装（`opencode_db_session_doc`）语义对齐：

- `info` ← session 行列（id/projectID/directory/slug/title/version/time/cost/tokens）。
- `messages[i].info` ← message.data JSON，**补** `id`（列主键）与 `sessionID` 列值
  （data JSON 里没有这两个键）；`time.created` 缺失时回退 `time_created` 列。
- `messages[i].parts[j]` ← part.data JSON，**补** `id`/`messageID`/`sessionID`。
- 排序：message 按 `time_created, id`；part 按 `time_created, id`（text 分片 data 里无 time，
  列上的 time_created 是唯一稳定排序键；E 线 cleaner 对无 time 分片回退消息时间）。
- 注意 directory 斜杠方向：db 存正斜杠（`P:/proj`），导出 JSON 里是反斜杠（`P:\proj`）——
  cleaner 不读该字段，工作目录匹配统一走 `Path(...).resolve()`，两种写法都能命中。

拼出的 doc 直接落盘为 `pending_upload/<学号>_<会话id>.json`，走 E 线
`cleaner.clean_session`（自动识别 opencode JSON）既有管线，无新增清洗逻辑。

## 6. 读取方式（exam_server 实现）

1. 首选 `sqlite3.connect("file:<db>?mode=ro", uri=True)` 纯只读打开。
2. 打不开（opencode 正开着、WAL/shm 锁）→ 把 db+wal+shm 拷到 `%TEMP%\opencode_db_*`
   再只读打开副本（拷副本是考试机稳妥路径）。原库永远只读、绝不写。
3. 会话列表只查 session 表（轻）；内容按需对选定会话查 message/part。

## 7. opencode export CLI（通道①）

- `opencode export <sessionID>` 存在，产物为上述单文件 JSON；另有 `--sanitize` 脱敏。
- **实测 1.18.18 的 export 没有 `--output` 参数**（`opencode export --help` 仅
  sessionID positional + --sanitize/--print-logs/--log-level/--pure）——JSON 打到
  stdout。因此实现直接把 stdout 重定向到临时文件，不依赖 `--output` 是否存在于
  1.18.29；如未来版本加了 `--output` 再切换也不影响当前实现。
- 调用时只传可执行文件路径（列表 argv，不经 shell/PowerShell），并把 opencode.exe
  所在目录（安装器默认 `%LOCALAPPDATA%\Programs\AlethicTools\opencode`）临时附加到
  子进程 PATH。
- 产物校验：必须是 `looks_like_opencode` 的 JSON 且 `info.id == sessionID`，否则视为
  失败、降级 db 副本拼装。
