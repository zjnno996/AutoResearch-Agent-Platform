#!/bin/bash
# Claw Research Web — start/stop the new frontend-web plus required backend services
# Usage: ./start-web.sh [start|stop|restart|status]

set -u

BASE="$(cd "$(dirname "$0")" && pwd)"
WEB="$BASE/frontend-web"
LOG="$BASE/logs"
PIDF="$BASE/.pids"

# Resolve python path from env/config/system
_cfg_py=""
for _cfg in "$BASE/config.arc.yaml" "$BASE/examples/config_template.yaml" "$BASE"/backend/runs/project_configs/*.yaml; do
    [ -f "$_cfg" ] || continue
    _cfg_py=$(grep 'python_path:' "$_cfg" 2>/dev/null | head -1 | sed 's/.*python_path:[[:space:]]*"\{0,1\}\([^"]*\)"\{0,1\}/\1/' | tr -d '[:space:]')
    [ -n "$_cfg_py" ] && [ -x "$_cfg_py" ] && break
    _cfg_py=""
done
PY="${PYTHON_PATH:-${_cfg_py:-python3}}"
PY_CMD_PATH="$(command -v "$PY" 2>/dev/null || true)"
if [ -x "$PY_CMD_PATH" ]; then
    export PATH="$(dirname "$PY_CMD_PATH"):$PATH"
fi

RESOURCE_MONITOR_PORT="${RESOURCE_MONITOR_PORT:-8905}"
AGENT_BRIDGE_PORT="${AGENT_BRIDGE_PORT:-8906}"
REVIEW_PORT="${REVIEW_PORT:-8907}"
FRONTEND_WEB_PORT="${FRONTEND_WEB_PORT:-5910}"
RESEARCHCLAW_SKIP_TORCH_INSTALL="${RESEARCHCLAW_SKIP_TORCH_INSTALL:-1}"
RESEARCHCLAW_STAGE8_LOCAL_FALLBACK="${RESEARCHCLAW_STAGE8_LOCAL_FALLBACK:-0}"
export RESOURCE_MONITOR_PORT AGENT_BRIDGE_PORT REVIEW_PORT FRONTEND_WEB_PORT RESEARCHCLAW_SKIP_TORCH_INSTALL RESEARCHCLAW_STAGE8_LOCAL_FALLBACK

mkdir -p "$LOG" "$PIDF"
ulimit -n 65535 2>/dev/null || true

G='\033[0;32m'; R='\033[0;31m'; Y='\033[0;33m'; N='\033[0m'

is_port_listening() {
    local port="$1"
    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
        return $?
    fi
    if [ -x "$PY" ]; then
        "$PY" - "$port" <<'PYEOF' >/dev/null 2>&1
import socket
import sys
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(0.3)
try:
    sock.connect(("127.0.0.1", int(sys.argv[1])))
except OSError:
    raise SystemExit(1)
finally:
    sock.close()
raise SystemExit(0)
PYEOF
        return $?
    fi
    return 1
}

wait_for_port() {
    local port="$1"
    local attempts="${2:-30}"
    local i
    for ((i=0; i<attempts; i++)); do
        is_port_listening "$port" && return 0
        sleep 0.5
    done
    return 1
}

confirm_service_start() {
    local name="$1" port="$2" pid="$3" logfile="$4" pidfile="$5"
    if wait_for_port "$port"; then
        echo -e "  ${G}✅ ${name} (PID=${pid}, port ${port})${N}"
        return 0
    fi
    echo -e "  ${R}✗ ${name} 启动失败，port ${port} 未监听${N}"
    [ -f "$logfile" ] && tail -20 "$logfile"
    kill "$pid" 2>/dev/null || true
    rm -f "$pidfile"
    return 1
}

kill_port_process() {
    local port="$1"
    local pids=""
    if command -v lsof >/dev/null 2>&1; then
        pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
    elif command -v ss >/dev/null 2>&1; then
        pids="$(ss -ltnp 2>/dev/null | awk -v port=":$port" '$4 ~ port { if (match($0, /pid=([0-9]+)/, m)) print m[1] }' | sort -u)"
    elif command -v fuser >/dev/null 2>&1; then
        pids="$(fuser "${port}/tcp" 2>/dev/null || true)"
    fi
    [ -z "$pids" ] && return 0
    for pid in $pids; do
        kill "$pid" 2>/dev/null || true
        sleep 0.2
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
    done
}

kill_pattern_processes() {
    local pattern="$1"
    local pids=""
    pids="$(ps -ef | grep -F "$pattern" | grep -v grep | awk '{print $2}' | sort -u)"
    [ -z "$pids" ] && return 0
    for pid in $pids; do
        kill "$pid" 2>/dev/null || true
        sleep 0.2
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
    done
}

ensure_node_path() {
    local node_dir=""
    if [ -x /opt/conda/envs/clawailab/bin/node ]; then
        node_dir="/opt/conda/envs/clawailab/bin"
    elif [ -x /opt/conda/envs/clawailab/bin/npm ]; then
        node_dir="/opt/conda/envs/clawailab/bin"
    fi
    if [ -n "$node_dir" ]; then
        export PATH="$node_dir:$PATH"
    fi
    [ -e "$WEB/node_modules" ] || ln -s ../frontend/node_modules "$WEB/node_modules" 2>/dev/null || true
}

start_resource_monitor() {
    if is_port_listening "$RESOURCE_MONITOR_PORT"; then
        echo -e "  ${Y}⏭ resource_monitor 已在运行 (port ${RESOURCE_MONITOR_PORT})${N}"
        return
    fi
    rm -f "$PIDF/resource_monitor.pid"
    if command -v setsid >/dev/null 2>&1; then
        setsid "$PY" -u "$BASE/backend/services/resource_monitor.py" --host 0.0.0.0 --port "$RESOURCE_MONITOR_PORT" \
            > "$LOG/resource_monitor.log" 2>&1 &
    else
        nohup "$PY" -u "$BASE/backend/services/resource_monitor.py" --host 0.0.0.0 --port "$RESOURCE_MONITOR_PORT" \
            > "$LOG/resource_monitor.log" 2>&1 &
    fi
    local pid=$!
    echo "$pid" > "$PIDF/resource_monitor.pid"
    confirm_service_start "resource_monitor" "$RESOURCE_MONITOR_PORT" "$pid" \
        "$LOG/resource_monitor.log" "$PIDF/resource_monitor.pid"
}

start_agent_bridge() {
    if is_port_listening "$AGENT_BRIDGE_PORT"; then
        echo -e "  ${Y}⏭ agent_bridge 已在运行 (port ${AGENT_BRIDGE_PORT})${N}"
        return
    fi
    rm -f "$PIDF/agent_bridge.pid"
    if command -v setsid >/dev/null 2>&1; then
        setsid "$PY" -u "$BASE/backend/services/agent_bridge.py" \
            --port "$AGENT_BRIDGE_PORT" --python "$PY" \
            --agent-dir "$BASE/backend/agent" \
            --runs-dir "$BASE/backend/runs" \
            --pool-idea 3 --pool-exp 2 --pool-code 3 --pool-exec 4 --pool-write 2 \
            --total-gpus 8 --gpus-per-project 1 \
            --discussion-mode --discussion-rounds 2 \
            --discussion-models "Qwen3.5-122B-A10B-FP8" \
            > "$LOG/agent_bridge.log" 2>&1 &
    else
        nohup "$PY" -u "$BASE/backend/services/agent_bridge.py" \
            --port "$AGENT_BRIDGE_PORT" --python "$PY" \
            --agent-dir "$BASE/backend/agent" \
            --runs-dir "$BASE/backend/runs" \
            --pool-idea 3 --pool-exp 2 --pool-code 3 --pool-exec 4 --pool-write 2 \
            --total-gpus 8 --gpus-per-project 1 \
            --discussion-mode --discussion-rounds 2 \
            --discussion-models "Qwen3.5-122B-A10B-FP8" \
            > "$LOG/agent_bridge.log" 2>&1 &
    fi
    local pid=$!
    echo "$pid" > "$PIDF/agent_bridge.pid"
    confirm_service_start "agent_bridge" "$AGENT_BRIDGE_PORT" "$pid" \
        "$LOG/agent_bridge.log" "$PIDF/agent_bridge.pid"
}

start_review_service() {
    if is_port_listening "$REVIEW_PORT"; then
        echo -e "  ${Y}⏭ review_service 已在运行 (port ${REVIEW_PORT})${N}"
        return
    fi
    rm -f "$PIDF/review_service.pid"
    if command -v setsid >/dev/null 2>&1; then
        setsid "$PY" -u "$BASE/backend/services/review_service.py" --host 0.0.0.0 --port "$REVIEW_PORT" \
            > "$LOG/review_service.log" 2>&1 &
    else
        nohup "$PY" -u "$BASE/backend/services/review_service.py" --host 0.0.0.0 --port "$REVIEW_PORT" \
            > "$LOG/review_service.log" 2>&1 &
    fi
    local pid=$!
    echo "$pid" > "$PIDF/review_service.pid"
    confirm_service_start "review_service" "$REVIEW_PORT" "$pid" \
        "$LOG/review_service.log" "$PIDF/review_service.pid"
}

start_frontend_web() {
    ensure_node_path
    if is_port_listening "$FRONTEND_WEB_PORT"; then
        echo -e "  ${Y}⏭ frontend-web 已在运行 (port ${FRONTEND_WEB_PORT})${N}"
        return
    fi
    if [ ! -x "$WEB/node_modules/.bin/vite" ]; then
        echo -e "  ${R}✗ frontend-web 缺少 vite 依赖，请先确认 frontend/node_modules 可用${N}"
        return 1
    fi
    rm -f "$PIDF/frontend_web.pid"
    cd "$WEB" || return 1
    if command -v setsid >/dev/null 2>&1; then
        setsid env PATH="$PATH" RESOURCE_MONITOR_PORT="$RESOURCE_MONITOR_PORT" AGENT_BRIDGE_PORT="$AGENT_BRIDGE_PORT" \
            CHOKIDAR_USEPOLLING="${CHOKIDAR_USEPOLLING:-true}" CHOKIDAR_INTERVAL="${CHOKIDAR_INTERVAL:-1000}" \
            "$WEB/node_modules/.bin/vite" --host 0.0.0.0 --port "$FRONTEND_WEB_PORT" \
            > "$LOG/frontend-web.log" 2>&1 &
    else
        nohup env PATH="$PATH" RESOURCE_MONITOR_PORT="$RESOURCE_MONITOR_PORT" AGENT_BRIDGE_PORT="$AGENT_BRIDGE_PORT" \
            CHOKIDAR_USEPOLLING="${CHOKIDAR_USEPOLLING:-true}" CHOKIDAR_INTERVAL="${CHOKIDAR_INTERVAL:-1000}" \
            "$WEB/node_modules/.bin/vite" --host 0.0.0.0 --port "$FRONTEND_WEB_PORT" \
            > "$LOG/frontend-web.log" 2>&1 &
    fi
    local pid=$!
    echo "$pid" > "$PIDF/frontend_web.pid"
    confirm_service_start "frontend-web" "$FRONTEND_WEB_PORT" "$pid" \
        "$LOG/frontend-web.log" "$PIDF/frontend_web.pid"
    local result=$?
    cd "$BASE" || return 1
    return "$result"
}

do_start() {
    echo "🦞 启动 Claw Research Web..."
    echo ""
    local failed=0
    start_resource_monitor || failed=1
    start_agent_bridge || failed=1
    start_review_service || failed=1
    start_frontend_web || failed=1
    echo ""
    echo "📍 服务地址:"
    echo -e "   ${G}新版前端:    http://localhost:${FRONTEND_WEB_PORT}/${N}"
    echo "   资源监控 WS:  ws://localhost:${RESOURCE_MONITOR_PORT}"
    echo "   Agent Bridge: ws://localhost:${AGENT_BRIDGE_PORT}"
    echo "   Auto Review:  http://localhost:${REVIEW_PORT}/api/review"
    echo ""
    if [ "$failed" -ne 0 ]; then
        echo -e "${R}一个或多个服务启动失败，请检查上方日志。${N}"
        return 1
    fi
}

do_stop() {
    echo "🛑 停止 Claw Research Web 相关服务..."
    for svc in frontend_web agent_bridge resource_monitor review_service; do
        f="$PIDF/$svc.pid"
        if [ -f "$f" ]; then
            pid=$(cat "$f")
            kill "$pid" 2>/dev/null && sleep 0.5
            kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
            rm -f "$f"
            echo -e "  ${G}⏹ $svc (PID=$pid)${N}"
        fi
    done
    for port in "$FRONTEND_WEB_PORT" "$RESOURCE_MONITOR_PORT" "$AGENT_BRIDGE_PORT" "$REVIEW_PORT"; do
        kill_port_process "$port"
    done
    kill_pattern_processes "$BASE/backend/services/agent_bridge.py"
    kill_pattern_processes "$BASE/backend/services/resource_monitor.py"
    kill_pattern_processes "$BASE/backend/services/review_service.py"
    kill_pattern_processes "$WEB/node_modules/.bin/vite --host 0.0.0.0 --port $FRONTEND_WEB_PORT"
    echo ""
}

do_status() {
    echo "📊 Claw Research Web 状态:"
    if is_port_listening "$RESOURCE_MONITOR_PORT"; then
        echo -e "  ${G}● resource_monitor${N} (port $RESOURCE_MONITOR_PORT)"
    else
        echo -e "  ${R}○ resource_monitor${N} (port $RESOURCE_MONITOR_PORT)"
    fi
    if is_port_listening "$AGENT_BRIDGE_PORT"; then
        echo -e "  ${G}● agent_bridge${N} (port $AGENT_BRIDGE_PORT)"
    else
        echo -e "  ${R}○ agent_bridge${N} (port $AGENT_BRIDGE_PORT)"
    fi
    if is_port_listening "$FRONTEND_WEB_PORT"; then
        echo -e "  ${G}● frontend-web${N} (port $FRONTEND_WEB_PORT)"
    else
        echo -e "  ${R}○ frontend-web${N} (port $FRONTEND_WEB_PORT)"
    fi
    if is_port_listening "$REVIEW_PORT"; then
        echo -e "  ${G}● review_service${N} (port $REVIEW_PORT)"
    else
        echo -e "  ${R}○ review_service${N} (port $REVIEW_PORT)"
    fi
    echo ""
}

case "${1:-start}" in
    start) do_start ;;
    stop) do_stop ;;
    restart) do_stop; sleep 1; do_start ;;
    status) do_status ;;
    *) echo "Usage: $0 {start|stop|restart|status}" ;;
esac
