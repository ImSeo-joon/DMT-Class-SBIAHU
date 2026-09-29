#!/usr/bin/env bash
# 配置 DMT CLASS 01 班级网站的邮箱验证发信。
#
#   sudo bash deploy/setup-mail.sh                  交互式填写并写入 /etc/dmt-class-site.env，重启服务并试发一封
#   sudo bash deploy/setup-mail.sh --test a@b.com   只发测试邮件，不改配置
#   sudo bash deploy/setup-mail.sh --show           只打印当前发信配置（密码打码）
#
# 已存在的其它配置项（SITE_ORIGIN、DMT_DB_PATH 等）会被保留，只覆盖 SMTP_* 这几行。

set -euo pipefail

ENV_FILE="${ENV_FILE:-/etc/dmt-class-site.env}"
SERVICE="${SERVICE:-dmt-class-site}"
APP_DIR="${APP_DIR:-/opt/dmt-class-site}"
RUN_USER="${RUN_USER:-dmt-site}"

info() { printf '\033[1;32m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "请用 root 运行：sudo bash deploy/setup-mail.sh"

SMTP_KEYS=(SMTP_HOST SMTP_PORT SMTP_SECURITY SMTP_USER SMTP_PASSWORD SMTP_SENDER SMTP_SENDER_NAME)

read_env_file() {
  [[ -f "$ENV_FILE" ]] || return 0
  # 只读取形如 KEY=VALUE 的行，忽略注释与空行
  while IFS= read -r line; do
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
    printf '%s\n' "${BASH_REMATCH[1]}=${BASH_REMATCH[2]}"
  done < "$ENV_FILE"
}

capture_current() {
  while IFS='=' read -r key value; do
    case "$key" in
      SMTP_HOST)          CUR_HOST="$value" ;;
      SMTP_PORT)          CUR_PORT="$value" ;;
      SMTP_SECURITY)      CUR_SECURITY="$value" ;;
      SMTP_USER)          CUR_USER="$value" ;;
      SMTP_SENDER)        CUR_SENDER="$value" ;;
      SMTP_SENDER_NAME)   CUR_SENDER_NAME="$value" ;;
    esac
  done < <(read_env_file)
}

dump_config() {
  local password
  password="$(read_env_file | sed -n 's/^SMTP_PASSWORD=//p')"
  info "当前配置：$ENV_FILE"
  echo "  SMTP_HOST         = ${CUR_HOST:-<未设置>}"
  echo "  SMTP_PORT         = ${CUR_PORT:-<未设置>}"
  echo "  SMTP_SECURITY     = ${CUR_SECURITY:-<未设置>}"
  echo "  SMTP_USER         = ${CUR_USER:-<未设置>}"
  echo "  SMTP_SENDER       = ${CUR_SENDER:-<未设置>}"
  echo "  SMTP_SENDER_NAME  = ${CUR_SENDER_NAME:-<未设置>}"
  if [[ -n "$password" ]]; then
    echo "  SMTP_PASSWORD     = 已设置（${#password} 个字符）"
  else
    echo "  SMTP_PASSWORD     = <未设置>"
  fi
}

need_app_python() {
  local python="$APP_DIR/.venv/bin/python"
  [[ -x "$python" ]] || die "找不到 $python，请确认 APP_DIR（当前：$APP_DIR）"
  printf '%s\n' "$python"
}

run_with_env() {
  local python="$1"; shift
  # 把 env 文件里的变量加载到当前 shell，再以服务用户身份运行
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
  if id "$RUN_USER" >/dev/null 2>&1; then
    sudo -u "$RUN_USER" --preserve-env=SMTP_HOST,SMTP_PORT,SMTP_SECURITY,SMTP_USER,SMTP_PASSWORD,SMTP_SENDER,SMTP_SENDER_NAME,DMT_DB_PATH,SITE_ORIGIN \
      "$python" "$APP_DIR/server.py" "$@"
  else
    "$python" "$APP_DIR/server.py" "$@"
  fi
}

send_test() {
  local target="$1"
  local python
  python="$(need_app_python)"
  [[ -f "$ENV_FILE" ]] || die "$ENV_FILE 不存在，先跑一次配置"
  info "向 $target 发送测试邮件…"
  run_with_env "$python" test-mail "$target"
}

prompt_and_write() {
  capture_current
  dump_config
  echo
  info "填写发信参数，直接回车表示保留当前值"
  read -rp  "SMTP 服务器      [${CUR_HOST:-smtp.qq.com}]: " host;   host="${host:-${CUR_HOST:-smtp.qq.com}}"
  read -rp  "端口             [${CUR_PORT:-465}]: "       port;   port="${port:-${CUR_PORT:-465}}"
  local default_security="${CUR_SECURITY:-}"; [[ -n "$default_security" ]] || default_security=$([[ "$port" == "465" ]] && echo ssl || echo starttls)
  read -rp  "加密方式 ssl/starttls/none [${default_security}]: " security; security="${security:-$default_security}"
  read -rp  "发信邮箱账号     [${CUR_USER:-}]: " user;     user="${user:-${CUR_USER:-}}"
  user="${user:-}"
  local password=""
  read -rsp "授权码/专用密码  [回车保留原值]: " password; echo
  [[ -n "$password" ]] || password="$(read_env_file | sed -n 's/^SMTP_PASSWORD=//p')"
  local default_sender="${CUR_SENDER:-$user}"
  read -rp  "发信地址 From    [${default_sender}]: " sender; sender="${sender:-$default_sender}"
  local default_name="${CUR_SENDER_NAME:-DMT CLASS 01}"
  read -rp  "发信人显示名     [${default_name}]: " sender_name; sender_name="${sender_name:-$default_name}"

  [[ -n "$host" ]]     || die "SMTP 服务器不能为空"
  [[ -n "$sender" ]]   || die "发信地址不能为空"
  [[ "$security" =~ ^(ssl|starttls|none)$ ]] || die "加密方式只能是 ssl、starttls 或 none"

  local tmp; tmp="$(mktemp)"
  if [[ -f "$ENV_FILE" ]]; then
    grep -vE "^[[:space:]]*($(IFS='|'; echo "${SMTP_KEYS[*]}"))=" "$ENV_FILE" > "$tmp" || true
  fi
  {
    echo "# ---- 发信：邮箱验证（由 deploy/setup-mail.sh 写入）----"
    echo "SMTP_HOST=$host"
    echo "SMTP_PORT=$port"
    echo "SMTP_SECURITY=$security"
    echo "SMTP_USER=$user"
    echo "SMTP_PASSWORD=$password"
    echo "SMTP_SENDER=$sender"
    echo "SMTP_SENDER_NAME=$sender_name"
  } >> "$tmp"
  install -m 600 -o root -g root "$tmp" "$ENV_FILE"
  rm -f "$tmp"
  info "已写入 $ENV_FILE（权限 600）"

  if systemctl list-unit-files "$SERVICE.service" >/dev/null 2>&1; then
    info "重启服务 $SERVICE…"
    systemctl restart "$SERVICE"
    sleep 1
    systemctl is-active --quiet "$SERVICE" && info "$SERVICE 运行中" || warn "$SERVICE 未处于运行状态，用 journalctl -u $SERVICE -n 50 查看"
  else
    warn "没找到 systemd 服务 $SERVICE，跳过重启；手动重启后再试发邮件"
  fi
}

main() {
  case "${1:-}" in
    --show)
      capture_current; dump_config ;;
    --test)
      [[ -n "${2:-}" ]] || die "用法：--test 收件邮箱"
      send_test "$2" ;;
    "")
      prompt_and_write
      echo
      read -rp "现在发一封测试邮件吗？填收件邮箱（回车跳过）: " target
      [[ -n "$target" ]] && send_test "$target"
      echo
      warn "别忘了在网站「编辑后台 → 邀请码与验证」里把「邮箱验证要求」设成 notify 或 gate，并确认「邮箱域名校验」符合预期。" ;;
    *)
      die "未知参数：$1" ;;
  esac
}

main "$@"
