#!/usr/bin/env bash
# 把招新平台（FastAPI）推送到云端服务器。用法：
#   DEPLOY_HOST=ubuntu@<服务器地址> SSH_KEY=~/.ssh/<私钥> bash deploy/push-server.sh
# 必填环境变量：
#   DEPLOY_HOST=<user@host>   部署目标（真实地址见 org 私有仓 infra-handoff）
#   SSH_KEY=<私钥路径>        部署用 SSH 私钥
# 服务器需先按 deploy/README.md 完成一次性初始化（目录、systemd unit、env、nginx）。
set -euo pipefail

# 防止 Git Bash/MSYS 把 /var/... 之类的参数当本地路径转换
export MSYS2_ARG_CONV_EXCL="*"

HOST="${DEPLOY_HOST:?请先设置 DEPLOY_HOST=user@host}"
SSH_OPTS=(-o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new)
KEY="${SSH_KEY:?请先设置 SSH_KEY=私钥路径}"
if [ -f "$KEY" ]; then SSH_OPTS+=(-i "$KEY"); fi

cd "$(dirname "$0")/.."

STAMP="$(date +%Y%m%d-%H%M%S)"
# 中转目录用远端 ~ 展开（不要展开本地 HOME 传给远端）
STAGE="~/recruitment-upload/$STAMP"
RELEASE="/var/www/recruitment/releases/$STAMP"
SERVICE="alethic-recruitment"

echo "[1/5] tar app static requirements.txt -> $HOST"
# 部署用户无权写 /var/www，先解到家目录中转目录，再 sudo mv 到位
tar czf - app static requirements.txt | \
  ssh "${SSH_OPTS[@]}" "$HOST" "rm -rf $STAGE && mkdir -p $STAGE && tar xzf - -C $STAGE"

echo "[2/5] move into $RELEASE and fix owner"
ssh "${SSH_OPTS[@]}" "$HOST" "
  sudo -n mkdir -p /var/www/recruitment/releases /var/www/recruitment/data
  sudo -n mv $STAGE $RELEASE
  rmdir ~/recruitment-upload 2>/dev/null || true
  sudo -n chown -R www-data:www-data $RELEASE
"

echo "[3/5] ensure venv + pip install"
ssh "${SSH_OPTS[@]}" "$HOST" "
  if [ ! -x /var/www/recruitment/venv/bin/uvicorn ]; then
    sudo -n python3 -m venv /var/www/recruitment/venv
  fi
  sudo -n /var/www/recruitment/venv/bin/pip install -q --upgrade pip
  sudo -n /var/www/recruitment/venv/bin/pip install -q -r '$RELEASE/requirements.txt'
"

echo "[4/5] switch current symlink + restart $SERVICE"
ssh "${SSH_OPTS[@]}" "$HOST" "
  sudo -n ln -sfn '$RELEASE' /var/www/recruitment/current
  sudo -n systemctl restart $SERVICE
  # uvicorn 启动约 1~2s，轮询健康检查避免竞速误报
  # 注意 \$i / \$(seq) 要转义——双引号串在本地展开，远端才该见到 $
  for i in \$(seq 1 15); do
    sleep 1
    if systemctl is-active --quiet $SERVICE && curl -fsS http://127.0.0.1:8901/api/health >/dev/null; then
      break
    fi
    if [ "\$i" = 15 ]; then
      echo "ERROR: $SERVICE 健康检查 15s 未通过" >&2
      exit 1
    fi
  done
"

echo "[5/5] prune old releases (keep 5)"
ssh "${SSH_OPTS[@]}" "$HOST" "cd /var/www/recruitment/releases && ls -1t | tail -n +6 | xargs -r sudo -n rm -rf"

echo "done: $RELEASE is live ($SERVICE active, health check ok)"
