#!/usr/bin/env bash
# AI4S 人才地图 · 服务器一键安装（Ubuntu 22.04 / 24.04）
#
# 用法（在服务器上，软件已上传到 /opt/ai4s_sourcing）：
#   sudo bash /opt/ai4s_sourcing/deploy/setup.sh talent.example.com   # 有域名：自动配置 HTTPS
#   sudo bash /opt/ai4s_sourcing/deploy/setup.sh                      # 暂时没有域名：只开 http://服务器IP（仅限试用）
# 国内大陆服务器装 Python 包慢的话，先 export PIP_INDEX=https://mirrors.aliyun.com/pypi/simple/
# 可以重复运行（例如换域名、更新依赖）。
set -euo pipefail

APP_DIR=/opt/ai4s_sourcing
DOMAIN="${1:-}"
PIP_INDEX="${PIP_INDEX:-}"

[ "$(id -u)" = 0 ] || { echo "请用 sudo 运行"; exit 1; }
[ -f "$APP_DIR/app.py" ] || { echo "没找到 $APP_DIR/app.py，请先把软件上传到 $APP_DIR"; exit 1; }
cd "$APP_DIR"

echo "==> 安装系统依赖（Python、Caddy）"
apt-get update -y
apt-get install -y python3-venv python3-pip caddy

echo "==> 创建运行账号 ai4s"
id ai4s &>/dev/null || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin ai4s

echo "==> 安装 Python 依赖"
[ -d .venv ] || python3 -m venv .venv
PIPARGS=()
[ -n "$PIP_INDEX" ] && PIPARGS=(-i "$PIP_INDEX")
.venv/bin/pip install "${PIPARGS[@]}" -q -U pip
.venv/bin/pip install "${PIPARGS[@]}" -q -r requirements.txt

echo "==> 写入配置与权限"
mkdir -p .streamlit backups
cp deploy/streamlit-config.toml .streamlit/config.toml
chown -R ai4s:ai4s "$APP_DIR"
chmod 750 "$APP_DIR"
for f in .openalex_key .s2_key team.db; do [ -f "$f" ] && chmod 600 "$f"; done
[ -f .openalex_key ] || echo "⚠️  还没有 .openalex_key：每周自动更新数据需要它（见 DEPLOY.md 第 3 步）"

echo "==> 注册后台服务"
cp deploy/ai4s.service deploy/ai4s-fetch.service deploy/ai4s-fetch.timer \
   deploy/ai4s-fetch-general.service deploy/ai4s-fetch-general.timer \
   deploy/ai4s-backup.service deploy/ai4s-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ai4s.service ai4s-fetch.timer ai4s-fetch-general.timer ai4s-backup.timer
systemctl restart ai4s.service

echo "==> 配置 Caddy"
SITE="${DOMAIN:-:80}"
sed "s|__SITE__|$SITE|" deploy/Caddyfile > /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl enable caddy
systemctl reload caddy || systemctl restart caddy

IP=$(curl -s -4 --max-time 5 ifconfig.me || echo "服务器公网IP")
echo
echo "✅ 安装完成"
if [ -n "$DOMAIN" ]; then
  echo "   访问：https://$DOMAIN （第一次访问时 Caddy 会自动申请证书，可能要等几十秒）"
else
  echo "   访问：http://$IP （未加密，仅限试用；配好域名后重新运行：sudo bash deploy/setup.sh 你的域名）"
fi
if [ ! -f team.db ] || ! sudo -u ai4s .venv/bin/python manage_users.py list | grep -q "管理员"; then
  echo "   还没有管理员账号，请运行："
  echo "   cd $APP_DIR && sudo -u ai4s .venv/bin/python manage_users.py add cpy --role admin"
fi
