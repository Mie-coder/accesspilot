#!/usr/bin/env bash
set -eu

SCRIPT_DIRECTORY=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIRECTORY/.." && pwd)

cd "$PROJECT_ROOT"

show_help() {
  cat <<'EOF'
用法：
  ./scripts/start-local.sh
  ./scripts/start-local.sh start
  ./scripts/start-local.sh reset
  ./scripts/start-local.sh stop

模式：
  start  保留现有数据，启动数据库、后端和前端（默认）
  reset  永久清空本地业务数据，重新建库后启动全部服务
  stop   关闭脚本管理的前端、后端和数据库，不删除数据

启动完成后，在当前终端输入 stop 回车，或按 Control+C：完整关闭三项服务。
EOF
}

START_MODE=${1:-start}

if [ "$#" -gt 1 ]; then
  echo "只支持一个模式参数。" >&2
  show_help >&2
  exit 2
fi

case "$START_MODE" in
  start|reset|stop) ;;
  -h|--help)
    show_help
    exit 0
    ;;
  *)
    echo "未知模式：$START_MODE" >&2
    show_help >&2
    exit 2
    ;;
esac

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "缺少命令：$1" >&2
    exit 1
  fi
}

port_is_in_use() {
  PORT_NUMBER=$1
  lsof -nP -iTCP:"$PORT_NUMBER" -sTCP:LISTEN >/dev/null 2>&1
}

show_log_tail() {
  LOG_LABEL=$1
  LOG_PATH=$2
  echo "$LOG_LABEL 启动失败。最后 30 行日志：" >&2
  tail -n 30 "$LOG_PATH" >&2 || true
}

wait_for_database() {
  ATTEMPT_NUMBER=0
  while [ "$ATTEMPT_NUMBER" -lt 60 ]; do
    if docker compose exec -T db sh -c \
      'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null 2>&1; then
      return 0
    fi
    ATTEMPT_NUMBER=$((ATTEMPT_NUMBER + 1))
    sleep 1
  done

  echo "数据库在 60 秒内未就绪。" >&2
  docker compose logs --tail 30 db >&2 || true
  return 1
}

wait_for_server() {
  SERVER_LABEL=$1
  SERVER_URL=$2
  SERVER_PROCESS_ID=$3
  SERVER_LOG_PATH=$4
  MAX_WAIT_SECONDS=$5
  WAITED_SECONDS=0

  while [ "$WAITED_SECONDS" -lt "$MAX_WAIT_SECONDS" ]; do
    if curl --fail --silent --show-error "$SERVER_URL" >/dev/null 2>&1; then
      return 0
    fi
    if ! kill -0 "$SERVER_PROCESS_ID" >/dev/null 2>&1; then
      show_log_tail "$SERVER_LABEL" "$SERVER_LOG_PATH"
      return 1
    fi
    WAITED_SECONDS=$((WAITED_SECONDS + 1))
    sleep 1
  done

  echo "$SERVER_LABEL 在 ${MAX_WAIT_SECONDS} 秒内未就绪。" >&2
  show_log_tail "$SERVER_LABEL" "$SERVER_LOG_PATH"
  return 1
}

require_command docker

if [ ! -f .env ]; then
  echo "缺少 .env。请先从 .env.example 创建本地配置。" >&2
  exit 1
fi

RUNTIME_DIRECTORY="$PROJECT_ROOT/.accesspilot-local"
CONTROLLER_PID_PATH="$RUNTIME_DIRECTORY/controller.pid"
API_PID_PATH="$RUNTIME_DIRECTORY/api.pid"
WEB_PID_PATH="$RUNTIME_DIRECTORY/web.pid"

managed_controller_is_running() {
  if [ ! -f "$CONTROLLER_PID_PATH" ]; then
    return 1
  fi

  MANAGED_CONTROLLER_PID=$(sed -n '1p' "$CONTROLLER_PID_PATH")
  case "$MANAGED_CONTROLLER_PID" in
    ""|*[!0-9]*) return 1 ;;
  esac

  if ! kill -0 "$MANAGED_CONTROLLER_PID" >/dev/null 2>&1; then
    return 1
  fi

  MANAGED_CONTROLLER_COMMAND=$(ps -p "$MANAGED_CONTROLLER_PID" -o command= 2>/dev/null || true)
  case "$MANAGED_CONTROLLER_COMMAND" in
    *start-local.sh*) return 0 ;;
    *) return 1 ;;
  esac
}

if [ "$START_MODE" = stop ]; then
  echo "[1/2] 关闭脚本管理的前端和后端..."
  if managed_controller_is_running; then
    kill -TERM "$MANAGED_CONTROLLER_PID"
    STOP_WAIT_SECONDS=0
    while kill -0 "$MANAGED_CONTROLLER_PID" >/dev/null 2>&1 \
      && [ "$STOP_WAIT_SECONDS" -lt 15 ]; do
      STOP_WAIT_SECONDS=$((STOP_WAIT_SECONDS + 1))
      sleep 1
    done
    if kill -0 "$MANAGED_CONTROLLER_PID" >/dev/null 2>&1; then
      echo "前后端未在 15 秒内停止。请回到启动终端按 Control+C。" >&2
      exit 1
    fi
  else
    echo "没有发现脚本管理的前后端进程。"
  fi

  rm -f "$CONTROLLER_PID_PATH" "$API_PID_PATH" "$WEB_PID_PATH"
  echo "[2/2] 关闭 AccessPilot Docker 服务..."
  docker compose stop
  echo "AccessPilot 已关闭；本地数据保留。"
  exit 0
fi

require_command curl
require_command lsof
require_command pnpm

if [ ! -x .venv/bin/python ]; then
  echo "缺少 Python 环境：.venv/bin/python" >&2
  exit 1
fi

if managed_controller_is_running; then
  echo "AccessPilot 已由 start-local.sh 启动。" >&2
  echo "关闭命令：./scripts/start-local.sh stop" >&2
  exit 1
fi

rm -f "$CONTROLLER_PID_PATH" "$API_PID_PATH" "$WEB_PID_PATH"

if port_is_in_use 8000 || port_is_in_use 5173; then
  echo "端口 8000 或 5173 已被占用。" >&2
  echo "先在旧的前后端终端按 Control+C，再重新运行脚本。" >&2
  exit 1
fi

if [ "$START_MODE" = reset ]; then
  echo "警告：reset 模式会永久删除 AccessPilot 本地 PostgreSQL 数据。"
  echo "删除：申请、审批、授权、聊天、Workspace、Session、审计记录。"
  echo "保留：源码、.env、依赖、Docker 镜像。"
  printf "输入 RESET 确认："
  read -r RESET_CONFIRMATION
  if [ "$RESET_CONFIRMATION" != "RESET" ]; then
    echo "已取消，未修改数据。"
    exit 0
  fi

  echo "[1/5] 删除 AccessPilot 容器和 PostgreSQL 数据卷..."
  docker compose down -v
else
  echo "[1/5] start 模式：保留现有数据库数据。"
fi

echo "[2/5] 启动 PostgreSQL 数据库..."
docker compose up -d db
wait_for_database

mkdir -p "$RUNTIME_DIRECTORY/logs"
RUN_LOG_DIRECTORY=$(mktemp -d "$RUNTIME_DIRECTORY/logs/run.XXXXXX")
API_LOG_PATH="$RUN_LOG_DIRECTORY/api.log"
WEB_LOG_PATH="$RUN_LOG_DIRECTORY/web.log"
API_PROCESS_ID=""
WEB_PROCESS_ID=""
CLEANUP_FINISHED=false

echo "$$" >"$CONTROLLER_PID_PATH"

stop_accesspilot() {
  if [ "$CLEANUP_FINISHED" = true ]; then
    return
  fi
  CLEANUP_FINISHED=true

  if [ -n "$WEB_PROCESS_ID" ] && kill -0 "$WEB_PROCESS_ID" >/dev/null 2>&1; then
    kill "$WEB_PROCESS_ID" >/dev/null 2>&1 || true
    wait "$WEB_PROCESS_ID" 2>/dev/null || true
  fi
  if [ -n "$API_PROCESS_ID" ] && kill -0 "$API_PROCESS_ID" >/dev/null 2>&1; then
    kill "$API_PROCESS_ID" >/dev/null 2>&1 || true
    wait "$API_PROCESS_ID" 2>/dev/null || true
  fi

  echo "正在关闭 AccessPilot Docker 服务..."
  docker compose stop >/dev/null 2>&1 || true
  echo "前端、后端和数据库已停止；本地数据保留。"
  echo "本次日志：$RUN_LOG_DIRECTORY"
  rm -f "$CONTROLLER_PID_PATH" "$API_PID_PATH" "$WEB_PID_PATH"
}

trap stop_accesspilot EXIT
trap 'exit 0' INT TERM

echo "[3/5] 执行数据库迁移、恢复虚构目录、启动后端..."
(
  . .venv/bin/activate
  exec ./scripts/start-api.sh
) >"$API_LOG_PATH" 2>&1 &
API_PROCESS_ID=$!
echo "$API_PROCESS_ID" >"$API_PID_PATH"
wait_for_server \
  "后端 API" \
  "http://127.0.0.1:8000/health" \
  "$API_PROCESS_ID" \
  "$API_LOG_PATH" \
  180

echo "[4/5] 启动前端开发服务器..."
pnpm --filter @accesspilot/web exec vite \
  --host 127.0.0.1 \
  --port 5173 >"$WEB_LOG_PATH" 2>&1 &
WEB_PROCESS_ID=$!
echo "$WEB_PROCESS_ID" >"$WEB_PID_PATH"
wait_for_server \
  "前端" \
  "http://127.0.0.1:5173" \
  "$WEB_PROCESS_ID" \
  "$WEB_LOG_PATH" \
  60

echo ""
echo "[5/5] AccessPilot 启动完成"
echo "模式：$START_MODE"
echo "前端工作台：http://127.0.0.1:5173"
echo "  用途：登录并体验权限申请、审批、开通和政策查询。"
echo "后端 API：http://127.0.0.1:8000"
echo "  用途：FastAPI 服务地址，前端请求会代理到这里。"
echo "API 文档：http://127.0.0.1:8000/docs"
echo "  用途：查看和调试后端接口。"
echo "健康检查：http://127.0.0.1:8000/health"
echo "  用途：确认后端进程存活。"
echo "本次日志：$RUN_LOG_DIRECTORY"
echo "完整关闭：当前终端输入 stop 回车，或按 Control+C。"

while kill -0 "$API_PROCESS_ID" >/dev/null 2>&1 \
  && kill -0 "$WEB_PROCESS_ID" >/dev/null 2>&1; do
  CONTROL_COMMAND=""
  if IFS= read -r -t 2 CONTROL_COMMAND; then
    case "$CONTROL_COMMAND" in
      stop)
        echo "收到 stop，正在完整关闭 AccessPilot..."
        exit 0
        ;;
      "") ;;
      *) echo "未知控制命令：$CONTROL_COMMAND。可用命令：stop" ;;
    esac
  else
    sleep 1
  fi
done

if ! kill -0 "$API_PROCESS_ID" >/dev/null 2>&1; then
  show_log_tail "后端 API" "$API_LOG_PATH"
fi
if ! kill -0 "$WEB_PROCESS_ID" >/dev/null 2>&1; then
  show_log_tail "前端" "$WEB_LOG_PATH"
fi
exit 1
