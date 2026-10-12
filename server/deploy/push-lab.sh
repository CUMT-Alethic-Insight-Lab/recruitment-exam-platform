#!/usr/bin/env bash
# 把招新平台（FastAPI）与机试会话接收机推送到实验室内网小主机。用法：
#   LAB_HOST=<user@host> SSH_KEY=~/.ssh/<私钥> bash deploy/push-lab.sh
# 必填环境变量：
#   LAB_HOST=<user@host>   实验室内网部署目标（真实地址见 org 私有仓 infra-handoff）
#   SSH_KEY=<私钥路径>     部署用 SSH 私钥
# 小主机需已装好 python3-venv / nginx / cloudflared（一次性初始化见 deploy/README.md）。
set -euo pipefail

export MSYS2_ARG_CONV_EXCL="*"

HOST="${LAB_HOST:?请先设置 LAB_HOST=user@host}"
SSH_OPTS=(-o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new)
KEY="${SSH_KEY:?请先设置 SSH_KEY=私钥路径}"
if [ -f "$KEY" ]; then SSH_OPTS+=(-i "$KEY"); fi

cd "$(dirname "$0")/.."

STAMP="$(date +%Y%m%d-%H%M%S)"
STAGE="~/recruitment-upload/$STAMP"
RELEASE="/srv/alethic/recruitment/releases/$STAMP"
SERVICE="alethic-recruitment"

echo "[1/6] tar app static requirements.txt -> $HOST"
tar czf - app static requirements.txt | \
  ssh "${SSH_OPTS[@]}" "$HOST" "rm -rf $STAGE && mkdir -p $STAGE && tar xzf - -C $STAGE"

echo "[2/6] move into $RELEASE and fix owner"
ssh "${SSH_OPTS[@]}" "$HOST" "
  mkdir -p /srv/alethic/recruitment/releases /srv/alethic/recruitment/data
  mv $STAGE $RELEASE
  rmdir ~/recruitment-upload 2>/dev/null || true
"

echo "[3/6] ensure venv + pip install"
ssh "${SSH_OPTS[@]}" "$HOST" "
  if [ ! -x /srv/alethic/recruitment/venv/bin/uvicorn ]; then
    python3 -m venv /srv/alethic/recruitment/venv
  fi
  /srv/alethic/recruitment/venv/bin/pip install -q --upgrade pip \
    -i https://mirrors.aliyun.com/pypi/simple/
  /srv/alethic/recruitment/venv/bin/pip install -q \
    -i https://mirrors.aliyun.com/pypi/simple/ -r '$RELEASE/requirements.txt'
"

echo "[4/6] push session_receiver.py"
tar czf - -C ../exam_kit session_receiver.py | \
  ssh "${SSH_OPTS[@]}" "$HOST" "mkdir -p /srv/alethic/receiver /srv/alethic/received_sessions && tar xzf - -C /srv/alethic/receiver"

echo "[5/6] switch current symlink + restart services"
ssh "${SSH_OPTS[@]}" "$HOST" "
  ln -sfn '$RELEASE' /srv/alethic/recruitment/current
  sudo -n systemctl restart $SERVICE alethic-session-receiver
  for i in \$(seq 1 15); do
    sleep 1
    if systemctl is-active --quiet $SERVICE && curl -fsS http://127.0.0.1:8901/api/health >/dev/null; then
      break
    fi
    if [ \"\$i\" = 15 ]; then
      echo \"ERROR: $SERVICE 健康检查 15s 未通过\" >&2
      sudo -n journalctl -u $SERVICE --no-pager -n 30 >&2
      exit 1
    fi
  done
"

echo "[6/6] prune old releases (keep 5)"
ssh "${SSH_OPTS[@]}" "$HOST" "
  cd /srv/alethic/recruitment/releases && ls -1t | tail -n +6 | xargs -r rm -rf
"

echo "done: $RELEASE is live on lab host"
