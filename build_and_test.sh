#!/usr/bin/env bash
# 以 ubuntu 执行：构建 codex++ 镜像，用一个空的测试工作区起容器，跑 doctor，测完停掉。
# 测试用独立容器、卷和 quick 临时隧道；旧 codexplus 服务已退役。
set -euo pipefail
cd "$(dirname "$0")"
# 测试资源与正式登录数据严格分开；清理只触及本次新建资源。
TEST_ID="$(date -u +%Y%m%d%H%M%S)-$$"
export CODEXPP_CONTAINER="codexpp-test-$TEST_ID"
export CODEXPP_VOLUME="codexpp-test-data-$TEST_ID"
export CODEXPP_PORT="${CODEXPP_TEST_PORT:-48772}"
export CODEXPP_CDP_PORT="${CODEXPP_TEST_CDP_PORT:-19224}"
export CODEXPP_VNC_PORT="${CODEXPP_TEST_VNC_PORT:-15902}"
export CODEXPP_NOVNC_PORT="${CODEXPP_TEST_NOVNC_PORT:-16082}"
export CODEXPP_IMAGE="codexpp-test:$TEST_ID"
export TUNNEL_MODE=quick
D() { if docker info >/dev/null 2>&1; then docker "$@"; else sudo docker "$@"; fi; }
T=''
cleanup() {
  result=$?
  trap - EXIT
  ./codex++ down >/dev/null 2>&1 || true
  D volume rm "$CODEXPP_VOLUME" >/dev/null 2>&1 || true
  D image rm "$CODEXPP_IMAGE" >/dev/null 2>&1 || true
  [[ -z $T ]] || rm -rf -- "$T"
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
echo "[1/4] 构建独立测试镜像"
D build --network="${CODEXPP_BUILD_NETWORK:-default}" -t "$CODEXPP_IMAGE" image/
echo "[2/4] 起测试容器"
T=$(mktemp -d)
echo "hello codex++" > "$T/README.md"
CODEXPP_WORKSPACE=$T CODEXPP_PROJECT_ROOT=$T ./codex++ up
echo "[3/4] 协议、边界与测试"
./codex++ sandbox-check
D exec "$CODEXPP_CONTAINER" python3 -m unittest discover -s /opt/codexpp/bridge -v
D exec "$CODEXPP_CONTAINER" python3 -m unittest discover -s /opt/codexpp/bin -v
./codex++ doctor
./codex++ status
echo "[4/4] 检查通过，退出时清理本次测试资源。正式镜像标签未改变。"
