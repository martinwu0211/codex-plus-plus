#!/usr/bin/env python3
"""Check MCP connectivity without printing any credential characters."""
from __future__ import annotations
import json, os, subprocess, sys, time, urllib.request
from pathlib import Path

S = Path("/data/state")
PORT = os.environ.get("MCP_PORT", "48771")
OK, BAD = "✅", "❌"


def secret() -> str:
    return (S / "mcp.secret").read_text().strip()


def post(url: str, payload: dict, timeout=15) -> tuple[int, dict | str]:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json, text/event-stream",
                                          "User-Agent": "codexpp-doctor/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode()
            return r.status, (json.loads(body) if body else "")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


def get(url: str, timeout=15) -> int:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "codexpp-doctor/1"}),
                                    timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def step(n, name, fn, fix, dns_wait=0):
    """dns_wait：临时隧道新域名刚生成时 DNS 还没生效（10-05 实测启动 5 秒后查会解析失败），按秒重试。"""
    t0 = time.time()
    while True:
        try:
            ok, detail = fn()
        except Exception as exc:
            ok, detail = False, f"{type(exc).__name__}: {str(exc).replace(secret(), '<secret>')}"
            if dns_wait and "Name or service not known" in str(exc) and time.time() - t0 < dns_wait:
                print(f"   … {n}. 域名还没生效，{int(time.time() - t0)} 秒，继续等", flush=True)
                time.sleep(10)
                continue
        break
    print(f"{OK if ok else BAD} {n}. {name}：{detail}")
    if not ok:
        print(f"   修复建议：{fix}")
        sys.exit(n)


def mcp_roundtrip(base: str):
    sec = secret()
    code, init = post(f"{base}/mcp/{sec}", {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                          "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                                     "clientInfo": {"name": "doctor", "version": "1"}}})
    if code != 200 or "result" not in init:
        return False, f"initialize 返回 {code}"
    code, tl = post(f"{base}/mcp/{sec}", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [t["name"] for t in tl.get("result", {}).get("tools", [])] if isinstance(tl, dict) else []
    return bool(names), f"协议 {init['result'].get('protocolVersion')}，工具 {len(names)} 个"


def main():
    print(f"codex++ doctor（认证已配置，cloudflared "
          f"{subprocess.run(['cloudflared', '--version'], capture_output=True, text=True).stdout.split()[2:3]}）")
    local = f"http://127.0.0.1:{PORT}"
    step(1, "容器内 MCP 桥健康", lambda: (get(f"{local}/health") == 200, "200"),
         "看 /data/state/bridge.log；supervisor 会自动重启桥")
    step(2, "本地 MCP 协议（initialize + tools/list）", lambda: mcp_roundtrip(local),
         "桥能起来但协议不通：查 bridge.log 里的异常")
    url_f = S / "public_url"
    step(3, "公网地址已就绪", lambda: (url_f.is_file() and url_f.read_text().strip().startswith("https://"),
                                    url_f.read_text().strip() if url_f.is_file() else "还没有"),
         "quick 模式等 1 分钟再试；仍没有就看 /data/state/tunnel.log（多半是出网被挡）")
    pub = url_f.read_text().strip()
    step(4, "公网健康（隧道）", lambda: (get(f"{pub}/health") == 200, pub),
         f"隧道没通：看 tunnel.log；named 模式检查 config.yml 的 ingress 是否指向 127.0.0.1:{PORT}", dns_wait=90)
    step(5, "公网 MCP 协议（隧道 + 密钥路径）", lambda: mcp_roundtrip(pub),
         "公网能到但协议不通：多半是 Cloudflare 规则拦了 POST，或路径被改写", dns_wait=90)
    # 请求日志只证明流量到达，不能把非 doctor 流量都当成 ChatGPT。
    log = S / "access.log"
    rows = []
    if log.is_file():
        for line in log.read_text().splitlines()[-500:]:
            try: rows.append(json.loads(line))
            except ValueError: pass
    recent = [r for r in rows if r.get("ts", 0) > time.time() - 86400]
    if not recent:
        print(f"⚠️ 6. 最近 24 小时没有已认证请求。连接器地址为 "
              f"{pub}/mcp/<secret>（完整地址用 codex++ url --full 查看）")
    else:
        bad = [r for r in recent if not r["path_ok"]]
        codes = {}
        for r in recent: codes[r["status"]] = codes.get(r["status"], 0) + 1
        print(f"{OK if not bad else BAD} 6. 最近 24 小时已认证请求 {len(recent)} 次，返回码 {codes}（包含自检，不能归属到ChatGPT）"
              + ("" if not bad else f"；其中 {len(bad)} 次密钥路径不对 → 连接器里的地址过期了，重新填一次"))
    reg = S / "connector_url"
    if reg.is_file() and reg.read_text().strip() != f"{pub}/mcp/{secret()}":
        print("⚠️ 7. 连接器登记的地址和当前地址不一致（quick 隧道重启会换地址）→ 运行 codex++ connect 或手动更新连接器")
    proof_file = S / "connector.json"
    try:
        proof = json.loads(proof_file.read_text())
    except (OSError, ValueError):
        proof = {}
    current = reg.is_file() and reg.read_text().strip() == f"{pub}/mcp/{secret()}"
    if proof.get("toolsVerified") and current:
        print(f"{OK} 8. ChatGPT 真实 workspace_info 调用已验收：{proof.get('verifiedAt')}")
    else:
        print("⚠️ 8. 当前地址尚无已验收的 ChatGPT 工具调用，协议成功不代表绑定完成。")
    print("检查完毕。")


if __name__ == "__main__":
    main()
