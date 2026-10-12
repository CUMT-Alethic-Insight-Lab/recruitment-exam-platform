# 招新平台部署（server/deploy/）

把 `server/` 下的 FastAPI 应用部署到一台 Linux 服务器（Ubuntu 24.04 验证过），
经 nginx 挂在主站 `/recruitment/` 路径下。本文是通用版说明；真实的
主机地址、账号、密钥路径与运维排障记录维护在 org 私有仓 **infra-handoff**。

- 应用入口：`app.main:app`，uvicorn 监听 `127.0.0.1:8901`
- systemd 单元：`alethic-recruitment.service`（User=www-data）
- nginx：`/recruitment/` strip 前缀后反代到 8901（片段见 `nginx-recruitment.location.conf`）
- 数据目录：`/var/www/recruitment/data`（sqlite，跨 release 持久，属主 www-data）
- venv：`/var/www/recruitment/venv`（跨 release 复用）
- 发布目录：`/var/www/recruitment/releases/<YYYYmmdd-HHMMSS>`，`current` 软链指向当前版

## 一次性初始化（服务器上）

以下命令在服务器上以具备免密 sudo 的部署用户执行。

1. 建目录：

```bash
sudo mkdir -p /var/www/recruitment/releases /var/www/recruitment/data
sudo chown www-data:www-data /var/www/recruitment/data
```

2. 生成环境变量文件 `/etc/alethic-recruitment.env`（含强随机 ADMIN_TOKEN，不要存进任何仓库）：

```bash
TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
printf 'ADMIN_TOKEN=%s\nPUBLIC_PREFIX=/recruitment\nALE_THIC_DATA_DIR=/var/www/recruitment/data\n' "$TOKEN" | \
  sudo tee /etc/alethic-recruitment.env >/dev/null
sudo chmod 600 /etc/alethic-recruitment.env
sudo chown root:root /etc/alethic-recruitment.env
```

3. 安装 systemd 单元：

```bash
sudo cp deploy/alethic-recruitment.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now alethic-recruitment
```

（首次部署前服务会因 current 不存在而启动失败，跑一次下面的日常部署即可；
或先手动跑一次 push-server.sh 再 enable。）

4. nginx：备份并编辑主站站点配置，在 `listen 127.0.0.1:80` 的 server 块内、
   `location /` 之前加入 `nginx-recruitment.location.conf` 中的 location 段，然后：

```bash
sudo nginx -t && sudo systemctl reload nginx
```

## 日常部署（本机）

```bash
DEPLOY_HOST=<user@host> SSH_KEY=<私钥路径> bash server/deploy/push-server.sh
```

`DEPLOY_HOST` / `SSH_KEY` 为必填环境变量（真实取值见私有仓 infra-handoff）。

脚本流程：tar `app static requirements.txt` → ssh 流式上传到家目录中转
（部署用户无权写 /var/www）→ `sudo mv` 到 `/var/www/recruitment/releases/<时间戳>`
并 chown www-data → 建/复用 venv 并 `pip install -r requirements.txt` →
切 `current` 软链 → `systemctl restart alethic-recruitment` → 本机健康检查 →
保留最近 5 个 release。

## 实验室内网小主机（lab，authoritative）

lab 侧部署用 `push-lab.sh`（`LAB_HOST` / `SSH_KEY` 同样走环境变量），
systemd 单元在 `deploy/lab/` 下：主服务、数据回收 timer、以及机试会话
局域网接收机 `alethic-session-receiver.service`（考试机 upload_mode=lan
时接收清洗后的轨迹文档）。

## 安装回收定时器

`app.maintenance` 会删除 `expires_at` 已过期的批次数据（uploads 目录、
exam_session、application 等行，见 `app/maintenance.py`），并把批次置为
`closed`，可安全重复执行。用 systemd timer 每天跑一次：

```bash
sudo cp deploy/alethic-recruitment-purge.service /etc/systemd/system/
sudo cp deploy/alethic-recruitment-purge.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now alethic-recruitment-purge.timer
```

验证（应看到 timer 处于 active，并列出下次触发时间）：

```bash
systemctl list-timers alethic-recruitment-purge.timer
journalctl -u alethic-recruitment-purge.service -n 20   # 查看最近一次回收摘要
```

手动立即跑一次：`sudo systemctl start alethic-recruitment-purge.service`。

## 安装暂存转发定时器（云端 edge）

「静态优先云端 + 动态优先 lab」架构下，云端 VPS 以 `ALE_THIC_ROLE=edge`
运行：交卷端点不写主库，multipart 暂存 `data/staging/{session_id}/` 并响应
`pending_sync`；本定时器每 30min 跑 `app.staging_forward`，探 lab 可达后逐个
POST 到 lab 的 `/api/internal/ingest-staged`（`X-Staging-Secret` 共享密钥），
lab 应答成功即删暂存（云端不留数据），lab 不可达本轮整体跳过。

env 需在 `/etc/alethic-recruitment.env` 追加：

```
ALE_THIC_ROLE=edge
LAB_ORIGIN_URL=https://lab-origin.alethicinsight.com/recruitment
STAGING_SECRET=<与 lab 一致的强随机密钥>
# lab 回源若挂 CF Access：
CF_ACCESS_CLIENT_ID=...
CF_ACCESS_CLIENT_SECRET=...
```

lab（authoritative）侧同 env 文件配 `STAGING_SECRET` 即自动挂载收口端点；
lab 侧如需 exam-archive 即时备份再配 `ARCHIVE_ENABLED=1` /
`ARCHIVE_REPO_DIR` / `ARCHIVE_GITHUB_TOKEN`。

```bash
sudo cp deploy/alethic-recruitment-forward.service /etc/systemd/system/
sudo cp deploy/alethic-recruitment-forward.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now alethic-recruitment-forward.timer
```

手动立即跑一次：`sudo systemctl start alethic-recruitment-forward.service`。

## 验证

```bash
curl -s https://<主站域名>/recruitment/api/health
curl -sI https://<主站域名>/recruitment/          # 空库时应 302 到管理端
systemctl status alethic-recruitment
journalctl -u alethic-recruitment -n 50
```

## 注意

- `ADMIN_TOKEN`、密码等敏感信息只放 `/etc/alethic-recruitment.env`，不要写入仓库。
- 换 token / 改环境变量：编辑 env 文件后 `sudo systemctl restart alethic-recruitment`。
- 回滚：把 `current` 软链指回上一个 release 并重启服务。
- 真实主机地址/账号/密钥路径等运维细节见 org 私有仓 **infra-handoff**。
