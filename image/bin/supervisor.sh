#!/usr/bin/env bash
# codex++ 容器内常驻：MCP 桥 + 隧道，挂了就重启。Chrome 不常驻（第 2 阶段按需启动）。
set -uo pipefail
umask 077
LOCK=${CODEXPP_CHROME_LOCK:-/run/lock/browser-fleet/chrome.lock}
[[ -f $LOCK && ! -L $LOCK && -r $LOCK ]] || { echo 'Missing regular readable shared Chrome lock; refusing startup'; exit 2; }
S=/data/state; mkdir -p "$S" /data/codex-home; chmod 700 "$S" /data/codex-home
[[ -s $S/mcp.secret ]] || { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$S/mcp.secret"; chmod 600 "$S/mcp.secret"; }
shutdown() {
  trap '' TERM INT
  python3 /opt/codexpp/bin/browser.py stop >>"$S/supervisor.log" 2>&1 || true
  mapfile -t workers < <(jobs -pr)
  if ((${#workers[@]})); then kill -TERM "${workers[@]}" 2>/dev/null || true; fi
  wait || true
  exit 0
}
trap shutdown TERM INT

run_bridge() { python3 /opt/codexpp/bridge/server.py >>"$S/bridge.log" 2>&1; }
run_tunnel() {
  if [[ ${TUNNEL_MODE:-quick} == named ]]; then
    # 用户自带命名隧道：ingress 必须指向容器内 MCP_PORT（默认 48771）。
    cloudflared tunnel --no-autoupdate --config /run/secrets/tunnel/config.yml run >>"$S/tunnel.log" 2>&1
  else
    : > "$S/tunnel.log"
    cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:${MCP_PORT}" >>"$S/tunnel.log" 2>&1
  fi
}
watch_url() {   # 从临时隧道日志里抓出公网地址，写给 doctor 和 up 用
  for _ in $(seq 60); do
    u=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$S/tunnel.log" 2>/dev/null | tail -1)
    [[ -n $u ]] && { echo "$u" > "$S/public_url"; return; }
    sleep 1
  done
}
[[ ${TUNNEL_MODE:-quick} == named && -n ${PUBLIC_URL:-} ]] && echo "$PUBLIC_URL" > "$S/public_url"

( while :; do run_bridge; echo "$(date -u +%FT%TZ) bridge exited, restart" >>"$S/bridge.log"; sleep 3; done ) &
( while :; do [[ ${TUNNEL_MODE:-quick} == quick ]] && rm -f "$S/public_url" && (watch_url &) ; run_tunnel; echo "$(date -u +%FT%TZ) tunnel exited, restart" >>"$S/tunnel.log"; sleep 5; done ) &
wait
