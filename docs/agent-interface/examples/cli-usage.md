# CLI / Agent 使用示例（提案，不可直接运行）

## 场景一：成员自己查询

```sh
alethic auth login
alethic auth status --json
alethic projects list --recruiting --json
alethic knowledge search "项目申请" --json
```

CLI 不输出隐私令牌；真实文档有 `source_url` 与更新时间。

## 场景二：个人 Agent 辅助填表

用户：“我要申请加入一个视觉方向项目，先帮我看看有哪些在招人。”

Agent：调用 `alethic projects list --recruiting --json`，展示已授权结果并请用户选择。

用户：“我选第一个，你帮我填表。”

Agent：调用 `alethic forms schema project-join --json`，按 schema 从用户消息中提取字段；只询问尚缺信息。展示草稿和声明，等待显式同意，再交由 CLI/可信网页提交。

## 场景三：Bash 脚本中使用

```sh
set -eu
alethic projects list --json > projects.json
python3 -m json.tool projects.json >/dev/null
```

如果认证过期，CLI 返回非零退出码和标准化错误，脚本停止；不得自动尝试使用共享管理员 Token。

## 场景四：接入支持 MCP 的个人 Agent

```sh
alethic auth login
alethic mcp serve
```

实际配置为 MCP `command`/`args` 指向已安装的 CLI；stdio 上只能传协议消息，日志写 stderr。各 Agent 客户端配置格式不同，应在测试后分别发布示例。
