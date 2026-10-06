# Codex + MCP + tunnel + dedicated on-demand Chrome.
FROM node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c

COPY dependencies.json /opt/codexpp/dependencies.json
COPY bin/install_dependency.py /opt/codexpp/install_dependency.py
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 git ca-certificates curl tini procps \
 && test "$(dpkg --print-architecture)" = amd64 \
 && python3 /opt/codexpp/install_dependency.py cloudflared /tmp/cf.deb \
 && apt-get install -y /tmp/cf.deb && rm -f /tmp/cf.deb \
 && rm -rf /var/lib/apt/lists/*

RUN CODEX_VERSION=$(python3 -c 'import json; print(json.load(open("/opt/codexpp/dependencies.json"))["codex_version"])') \
 && npm install -g "@openai/codex@$CODEX_VERSION" && npm cache clean --force

RUN apt-get update \
 && apt-get install -y --no-install-recommends xvfb x11vnc novnc websockify fonts-liberation \
 && CHROME_URL=$(python3 -c 'import json; print(json.load(open("/opt/codexpp/dependencies.json"))["chrome"]["url"])') \
 && CHROME_SHA256=$(python3 -c 'import json; print(json.load(open("/opt/codexpp/dependencies.json"))["chrome"]["sha256"])') \
 && curl -fsSL --connect-timeout 30 --max-time 1800 --retry 2 -o /tmp/chrome.deb "$CHROME_URL" \
 && printf '%s  %s\n' "$CHROME_SHA256" /tmp/chrome.deb | sha256sum -c - \
 && apt-get install -y /tmp/chrome.deb \
 && rm -f /tmp/chrome.deb && rm -rf /var/lib/apt/lists/*

COPY bridge/ /opt/codexpp/bridge/
COPY bin/ /opt/codexpp/bin/
RUN chmod +x /opt/codexpp/bin/* \
 && mkdir -p /data /workspace && chown -R 1000:1000 /data

ENV MCP_WORKSPACE=/workspace MCP_SECRET_FILE=/data/state/mcp.secret MCP_ACCESS_LOG=/data/state/access.log \
    MCP_PORT=48771 CODEX_HOME=/data/codex-home TUNNEL_MODE=quick \
    DISPLAY=:100 CODEXPP_EXTERNAL_DISPLAY=0 CODEXPP_CDP_PORT=9224 \
    CODEXPP_VNC_PORT=5902 CODEXPP_NOVNC_PORT=6082 \
    CODEXPP_CHROME_LOCK=/run/lock/browser-fleet/chrome.lock

USER 1000
WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/opt/codexpp/bin/supervisor.sh"]
HEALTHCHECK --interval=60s --timeout=5s CMD curl -fs "http://127.0.0.1:${MCP_PORT}/health" || exit 1
