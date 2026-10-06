#!/usr/bin/env python3
"""只读工作区 MCP 桥，沿用用户维护的既有桥实现并在本项目整改。

来源和许可说明见 THIRD_PARTY.md；既有代码的迁入不等于独立重写。

协议与权限边界：
  1. **认证用 URL 里的密钥，不实现 OAuth 2.1。**
     ChatGPT 的连接器表单支持 No Auth，配上不可猜的路径就够用了。
     省掉动态客户端注册、PKCE、轮换 refresh token 一大堆最容易出错的代码。
  2. **只读是在服务端做掉的，不是靠提示词约束。**
     下面根本没有写文件、删文件、执行命令的工具——模型再想干也没有入口。

工具清单对齐上游那九个，少了 test_status / execution_* 三个
（那三个是给它的状态机用的，我们不需要）。
"""
from __future__ import annotations

import json
import fnmatch
import os
import stat
import subprocess
import sys
import tempfile
import hmac
import threading
import time
import selectors
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(os.environ.get("MCP_WORKSPACE", "/workspace")).resolve()
# codex++：密钥由容器首次启动时生成，放在数据卷里，不进镜像
SECRET = Path(os.environ.get("MCP_SECRET_FILE", "/data/state/mcp.secret")).read_text().strip()
if len(SECRET) < 32:
    raise ValueError('MCP secret must contain at least 32 characters')
ACCESS_LOG = Path(os.environ.get("MCP_ACCESS_LOG", "/data/state/access.log"))
LOG_LOCK = threading.Lock()
ALLOWED_ORIGINS = set(filter(None, os.environ.get('MCP_ALLOWED_ORIGINS', '').split(',')))
MAX_REQUEST = 1_000_000
TOOL_LIMIT = threading.BoundedSemaphore(2)
PORT = int(os.environ.get("MCP_PORT", "48770"))
MAX_BYTES = 120_000          # 单个文件返回上限，别把整个仓库塞进对话

# 这些目录不给看：要么是凭证，要么是噪音
DENY = {".git", "node_modules", ".venv", "venv", "__pycache__", ".cache",
        "dist", "build", ".next", "charts",
        ".ssh", ".aws", ".gnupg", ".codex", ".claude", ".local", "private",
        "chrome-profile", "codex-home", "browser_profile", "secret", "secrets",
        "credentials", "user data"}
EXTRA_DENY = {name.strip().lower() for name in os.environ.get('MCP_DENY_NAMES', '').split(',')
              if name.strip()}
if any(name in {'.', '..'} or any(char in name for char in '/\\\0') for name in EXTRA_DENY):
    raise ValueError('MCP_DENY_NAMES must contain comma-separated file or directory names')
DENY |= EXTRA_DENY
DENY_FILE = {".env", ".secret", "auth.json", "credentials.json", ".npmrc",
             ".pypirc", ".netrc", ".git-credentials", "id_rsa", "id_dsa",
             "id_ecdsa", "id_ed25519", "login data", "cookies", "rclone.conf", "tokens.json",
             "web data", "local state", "id_ed25519_sk", "id_ecdsa_sk"}
DENY_PATTERNS = (".env.*", "*.pem", "*.key", "*.p12", "*.pfx", ".secret*",
                 "credentials.*", "secrets.*", "auth.*.json", "id_rsa.*",
                 "id_dsa.*", "id_ecdsa.*", "id_ed25519.*", "*.env", "*.secret",
                 "*.tfstate*", "*.ppk", "*.jks", "*.keystore", "service-account*.json",
                 "token.json", '*.tfvars*', '*.kdbx', 'kubeconfig', 'passwords.*',
                 '*.token', '*.gpg', '*.sqlite', '*.sqlite3', 'login data-*', 'cookies-*',
                 '*client_secret*', '*serviceaccount*.json', '*.env.*', 'env.*',
                 '*kubeconfig*', '*.asc', '*.ovpn', 'web data-*')
DOT_ALLOW = {'.github', '.gitignore', '.gitattributes', '.editorconfig', '.codexpp'}


def denied(name: str) -> bool:
    name = name.lower()
    return ((name.startswith('.') and name not in DOT_ALLOW) or name in DENY or name in DENY_FILE
            or any(fnmatch.fnmatchcase(name, pattern) for pattern in DENY_PATTERNS))


def safe(rel: str) -> Path:
    """把相对路径解析到工作区内，越界就抛。

    `..` 和绝对路径都要挡——只读不等于随便读，/etc/shadow 也是只读的。
    """
    if not isinstance(rel, str) or '\0' in rel:
        raise ValueError('Invalid relative path')
    relative = Path(rel)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Path must stay inside the workspace')
    p = ROOT
    for part in relative.parts:
        if denied(part):
            raise ValueError('This path is not exposed')
        p = p / part
        if p.is_symlink():
            raise ValueError('Symbolic links are not exposed')
    return p


def read_bytes(p: Path) -> tuple[bytes, int]:
    """Open relative to directory FDs; symlink replacement cannot escape ROOT."""
    parts = p.relative_to(ROOT).parts
    if not parts:
        raise ValueError('Expected a regular file')
    directory = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('Expected a regular file')
            return stream.read(MAX_BYTES), info.st_size
    finally:
        os.close(directory)


@contextmanager
def git_snapshot(d: Path):
    """Copy bounded regular metadata only; original config/hooks are never used.

    Object files are copied too: alternates, linked object stores and concurrent
    repository-config changes cannot redirect a command outside this snapshot.
    Large repositories fail closed rather than using their original Git config.
    """
    work = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    metadata = None
    try:
        for part in d.relative_to(ROOT).parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=work)
            os.close(work)
            work = child
        metadata = os.open('.git', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=work)
        with tempfile.TemporaryDirectory(prefix='codexpp-git-') as folder:
            target = Path(folder)
            budget = [128 * 1024 * 1024, 20000]

            def copy(parent, name, destination):
                budget[1] -= 1
                if budget[1] < 0:
                    raise ValueError('Repository exceeds the safe snapshot limit')
                source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                try:
                    info = os.fstat(source)
                    if stat.S_ISDIR(info.st_mode):
                        destination.mkdir()
                        for child in os.listdir(source):
                            copy(source, child, destination / child)
                    elif stat.S_ISREG(info.st_mode):
                        if info.st_size > budget[0]:
                            raise ValueError('Repository exceeds the safe snapshot limit')
                        with destination.open('xb') as output:
                            while chunk := os.read(source, min(65536, budget[0] + 1)):
                                budget[0] -= len(chunk)
                                if budget[0] < 0:
                                    raise ValueError('Repository exceeds the safe snapshot limit')
                                output.write(chunk)
                    else:
                        raise ValueError('Unsupported repository metadata')
                finally:
                    os.close(source)

            for name in ('HEAD', 'index', 'packed-refs', 'refs'):
                if name in os.listdir(metadata):
                    copy(metadata, name, target / name)
            objects = os.open('objects', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=metadata)
            try:
                (target / 'objects').mkdir()
                for name in os.listdir(objects):
                    # Do not copy objects/info (alternates) or promisor metadata.
                    if len(name) == 2 and all(c in '0123456789abcdef' for c in name):
                        copy(objects, name, target / 'objects' / name)
                    elif name == 'pack':
                        pack = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=objects)
                        try:
                            (target / 'objects' / 'pack').mkdir()
                            for file in os.listdir(pack):
                                if file.endswith(('.pack', '.idx', '.rev')):
                                    copy(pack, file, target / 'objects' / 'pack' / file)
                        finally:
                            os.close(pack)
            finally:
                os.close(objects)
            (target / 'config').write_text('[core]\nrepositoryformatversion = 0\nbare = false\n')
            yield target, work
    finally:
        if metadata is not None:
            os.close(metadata)
        os.close(work)


def git_run(snapshot: tuple[Path, int], args: list[str]):
    directory, work = snapshot
    environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_PAGER='cat', GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0',
                       GIT_DIR=str(directory), GIT_WORK_TREE='/proc/self/fd/' + str(work),
                       GIT_ATTR_NOSYSTEM='1', GIT_NO_LAZY_FETCH='1')
    command = ['git', '--no-pager', '--literal-pathspecs', '-c', 'core.fsmonitor=false',
                           '-c', 'core.hooksPath=' + os.devnull,
                           '-c', 'core.attributesFile=' + os.devnull, '-c', 'log.showSignature=false',
                           '-c', 'diff.ignoreSubmodules=all', '-c', 'status.submoduleSummary=false',
                           '-c', 'core.pager=cat', *args]
    with subprocess.Popen(command, env=environment, pass_fds=(work,), stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL) as process:
        data = bytearray()
        deadline = time.monotonic() + 30
        try:
            with selectors.DefaultSelector() as poll:
                poll.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not poll.select(remaining):
                        raise subprocess.TimeoutExpired(command, 30)
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        return subprocess.CompletedProcess(command, 1, '', '')
            code = process.wait(timeout=max(.01, deadline - time.monotonic()))
            return subprocess.CompletedProcess(command, code, data.decode('utf-8', 'replace'), '')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def git(*args: str, repo: str = "") -> str:
    """在某个项目里跑 git。

    支持工作区根仓库（repo="."）及工作区下的独立项目仓库。
    """
    d = safe(repo) if repo else ROOT
    if (d / '.git').is_symlink() or not (d / ".git").is_dir() or not any((d / ".git").iterdir()):
        # 直接把 git 的 "not a git repository" 抛给模型没有信息量。
        # Empty metadata directories are not valid repositories.
        return f"{repo or '工作区根目录'} 不是 git 仓库。有仓库的是：" + "、".join(repos())
    if not args or args[0] not in ('log', 'status', 'diff'):
        raise ValueError('Only supported read-only Git commands are allowed')
    with git_snapshot(d) as snapshot:
        return git_query(d, snapshot, args)


def git_query(d: Path, snapshot: tuple[Path, int], args: tuple[str, ...]) -> str:
    if args[0] == 'diff':
        staged = '--staged' in args
        scope = list(args[args.index('--') + 1:]) if '--' in args else []
        for name in scope:
            safe(str(d.relative_to(ROOT) / name))
        options = ['diff', '--no-ext-diff', '--no-textconv', '--no-renames', '--ignore-submodules=all'] + (['--staged'] if staged else [])
        names = git_run(snapshot, options + ['--name-only', '-z', '--', *scope])
        if names.returncode:
            return 'Git file listing failed'
        # Git must not read live worktree files after our path/link checks.
        with tempfile.TemporaryDirectory(prefix='codexpp-diff-') as temporary:
            worktree = Path(temporary)
            permitted, budget = [], 128 * 1024 * 1024
            for name in names.stdout.split('\0'):
                if not name:
                    continue
                try:
                    source = safe(str(d.relative_to(ROOT) / name))
                    if source.exists():
                        data, size = read_bytes(source)
                        if size > MAX_BYTES or len(data) > budget:
                            raise ValueError('File exceeds the safe diff limit')
                        budget -= len(data)
                        destination = worktree / name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(data)
                    permitted.append(name)
                except (ValueError, OSError):
                    continue
            if not permitted:
                return '(无可公开改动)'
            descriptor = os.open(worktree, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                r = git_run((snapshot[0], descriptor), options + ['--', *permitted])
            finally:
                os.close(descriptor)
    elif args[0] == 'status':
        r = git_run(snapshot, ['status', '--porcelain=v1', '-z', '--no-renames', '--ignore-submodules=all'])
        if r.returncode:
            return 'Git query failed'
        output = []
        for entry in r.stdout.split('\0'):
            if not entry:
                continue
            try:
                safe(str(d.relative_to(ROOT) / entry[3:]))
            except ValueError:
                continue
            output.append(entry[:3] + repr(entry[3:]))
        return '\n'.join(output)
    else:
        r = git_run(snapshot, list(args))
    return r.stdout[:MAX_BYTES] if not r.returncode else 'Git query failed'


def repos() -> list[str]:
    out = []
    metadata = ROOT / '.git'
    if metadata.is_dir() and not metadata.is_symlink() and any(metadata.iterdir()):
        out.append('.')
    for d in ROOT.iterdir():
        if not d.is_dir() or d.is_symlink() or denied(d.name):
            continue
        g = d / ".git"
        if not g.is_symlink() and g.is_dir() and any(g.iterdir()):
            out.append(d.name)
    return sorted(out)


# ── 工具实现 ──────────────────────────────────────────────────────────
def t_workspace_info(_: dict) -> str:
    tops = sorted(d.name for d in ROOT.iterdir()
                  if d.is_dir() and not d.is_symlink() and not denied(d.name) and not d.name.startswith("."))
    summaries = {}
    for name in repos()[:20]:
        try:
            summaries[name] = git('log', '-1', '--oneline', repo=name).strip()[:80]
        except (ValueError, OSError, subprocess.TimeoutExpired):
            summaries[name] = 'Repository cannot be safely queried within the snapshot limits'
    return json.dumps({
        "root": str(ROOT),
        "projects": tops,
        "repos": summaries,
    }, ensure_ascii=False)


def t_list_directory(a: dict) -> str:
    p = safe(a.get("path", "."))
    directory = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in p.relative_to(ROOT).parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        out = []
        for name in sorted(os.listdir(directory))[:400]:
            if denied(name):
                continue
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                out.append('d ' + name)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                out.append('- ' + name + f'  {info.st_size}B')
        return '\n'.join(out) or '(空)'
    finally:
        os.close(directory)


def t_read_file(a: dict) -> str:
    p = safe(a["path"])
    if not p.is_file():
        return f"不是文件：{a['path']}"
    data, size = read_bytes(p)
    txt = data.decode("utf-8", "replace")
    if size > MAX_BYTES:
        txt += f"\n…（只给了前 {MAX_BYTES} 字节，原文件 {size} 字节）"
    return txt


def t_search_workspace(a: dict) -> str:
    q = a['query']
    if not isinstance(q, str) or not q or len(q) > 1000:
        raise ValueError('Expected a non-empty keyword of at most 1000 characters')
    root = safe(a.get('path', '.'))
    lines, examined, total = [], 0, 0

    def files():
        if root.is_file():
            yield root
            return
        for directory, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = sorted(name for name in dirs if not denied(name)
                             and not (Path(directory) / name).is_symlink())
            for name in sorted(names):
                if not denied(name):
                    yield Path(directory) / name

    for candidate in files():
        examined += 1
        if examined > 5000:
            break
        try:
            candidate = safe(str(candidate.relative_to(ROOT)))
            data, _ = read_bytes(candidate)
        except (OSError, ValueError):
            continue
        if b'\0' in data:
            continue
        for number, line in enumerate(data.decode('utf-8', 'replace').splitlines(), 1):
            if q in line:
                entry = f'{candidate.relative_to(ROOT)}:{number}:{line}'
                size = len(entry.encode('utf-8'))
                if total + size > MAX_BYTES:
                    return '\n'.join(lines) or '匹配内容超过返回上限'
                lines.append(entry)
                total += size
                if len(lines) >= 120:
                    return '\n'.join(lines)
    return '\n'.join(lines) or '没有匹配'


def t_git_status(a: dict) -> str:
    if a.get("repo"):
        return git("status", "--short", "--branch", repo=a["repo"]) or "(干净)"
    out = []
    for r in repos()[:20]:
        try:
            st = git("status", "--short", "--branch", repo=r).strip()
        except (ValueError, OSError, subprocess.TimeoutExpired):
            st = 'Repository cannot be safely queried within the snapshot limits'
        out.append(f"=== {r} ===\n{st or '(干净)'}")
    return "\n".join(out)


def t_git_diff(a: dict) -> str:
    repo = a.get("repo") or ""
    if not repo:
        return "要指定 repo（哪个项目）。可选：" + "、".join(repos())
    args = ["diff"]
    if a.get("staged"):
        args.append("--staged")
    if a.get("path"):
        args += ["--", a["path"]]
    return git(*args, repo=repo) or "(无改动)"


TOOLS = {
    "workspace_info": (t_workspace_info, "工作区总览：根目录、项目及最近提交摘要", {}),
    "list_directory": (t_list_directory, "列出某个目录下的内容",
                       {"path": {"type": "string", "description": "相对工作区根的路径，默认 ."}}),
    "read_file": (t_read_file, "读一个文件的内容（最多 120KB）",
                  {"path": {"type": "string", "description": "相对工作区根的文件路径"}}),
    "search_workspace": (t_search_workspace, "在允许的工作区文件中按字面关键词搜索",
                         {"query": {"type": "string", "description": "要搜的字符串"},
                          "path": {"type": "string", "description": "限定搜索目录，默认全部"}}),
    "git_status": (t_git_status, "git 短状态；不给 repo 就汇总所有项目",
                   {"repo": {"type": "string", "description": "项目名，如 my-project"}}),
    "git_diff": (t_git_diff, "某个项目的 git diff；staged=true 看暂存区",
                 {"repo": {"type": "string", "description": "项目名，必填"},
                  "path": {"type": "string"}, "staged": {"type": "boolean"}}),
}


# ── MCP 协议层（JSON-RPC over HTTP）────────────────────────────────────
def rpc(req: dict) -> dict | None:
    if (not isinstance(req, dict) or req.get('jsonrpc') != '2.0'
            or not isinstance(req.get('method'), str)
            or ('id' in req and (isinstance(req['id'], (bool, dict, list))))):
        return {'jsonrpc': '2.0', 'id': None,
                'error': {'code': -32600, 'message': 'Invalid request'}}
    if 'id' not in req:
        return None  # All notifications have no response and execute no tools.
    mid, method, params = req.get("id"), req.get("method"), req.get("params", {})
    if not isinstance(params, dict):
        return {'jsonrpc': '2.0', 'id': mid,
                'error': {'code': -32602, 'message': 'Invalid params'}}

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method == "initialize":
        return ok({
            "protocolVersion": (params.get("protocolVersion")
                                if params.get("protocolVersion") in ('2024-11-05', '2025-03-26', '2025-06-18')
                                else '2025-06-18'),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "codex-plus-workspace", "version": "0.1.0"},
        })
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None                      # 通知没有回复
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [{
            "name": n,
            "description": desc,
            "annotations": {"readOnlyHint": True, "destructiveHint": False,
                            "idempotentHint": True, "openWorldHint": False},
            "inputSchema": {"type": "object", "properties": props,
                            "required": {'read_file': ['path'], 'search_workspace': ['query'],
                                         'git_diff': ['repo']}.get(n, [])},
        } for n, (_, desc, props) in TOOLS.items()]})
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str) or name not in TOOLS:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32602, "message": f"没有这个工具：{name}"}}
        if not TOOL_LIMIT.acquire(blocking=False):
            return ok({'content': [{'type': 'text', 'text': 'Workspace is busy; retry later'}], 'isError': True})
        try:
            arguments = params.get('arguments', {})
            if not isinstance(arguments, dict):
                raise ValueError('Expected an object for tool arguments')
            text = TOOLS[name][0](arguments)
        except Exception as exc:
            # 工具失败要作为内容返回，不是协议错误——否则 ChatGPT 看不到原因
            message = str(exc) if isinstance(exc, ValueError) else 'Workspace query failed'
            return ok({"content": [{"type": "text", "text": f"执行失败：{message}"}],
                       "isError": True})
        finally:
            TOOL_LIMIT.release()
        return ok({"content": [{"type": "text", "text": text}]})
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"未实现的方法：{method}"}}


class BoundedServer(ThreadingHTTPServer):
    """Limit open handlers, including slow unauthenticated connections."""
    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(*args, **kwargs)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b'HTTP/1.0 503 Busy\r\nContent-Length: 0\r\n\r\n')
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, *args):
        try:
            super().process_request_thread(*args)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    timeout = 30

    def _auth(self) -> bool:
        # 密钥在路径里：/mcp/<secret>。没有 OAuth，靠不可猜。
        return hmac.compare_digest(self.path.encode(), ('/mcp/' + SECRET).encode())

    def _origin(self) -> bool:
        origin = self.headers.get('Origin')
        return origin is None or origin in ALLOWED_ORIGINS

    def _send(self, code: int, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._origin():
            return self._send(403, b'{"error":"origin denied"}')
        if self.path == "/health":
            return self._send(200, b'{"service":"codex-plus-workspace","ok":true}')
        if not self._auth():
            return self._send(404, b'{"error":"not found"}')
        # This endpoint accepts JSON-RPC POSTs; it does not provide an SSE stream.
        self.send_response(405)
        self.send_header("Allow", "POST")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        if not self._origin():
            return self._send(403, b'{"error":"origin denied"}')
        if not self._auth():
            return self._send(404, b'{"error":"not found"}')
        lengths = self.headers.get_all('Content-Length', [])
        if self.headers.get('Transfer-Encoding') or len(lengths) != 1:
            return self._send(400, b'{"error":"invalid framing"}')
        try:
            n = int(lengths[0])
        except ValueError:
            return self._send(400, b'{"error":"invalid length"}')
        if n < 1 or n > MAX_REQUEST:
            return self._send(413, b'{"error":"request size denied"}')
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, b'{"error":"bad json"}')
        if isinstance(payload, list) and (not payload or len(payload) > 100):
            return self._send(400, b'{"error":"invalid batch"}')
        batch = payload if isinstance(payload, list) else [payload]
        out = [r for r in (rpc(x) for x in batch) if r is not None]
        self._rpc_methods = [x.get("method") for x in batch if isinstance(x, dict)
                             and x.get("method") in ("initialize", "tools/list", "tools/call",
                                                       "notifications/initialized", "ping")]
        self._tool_calls = [x.get("params", {}).get("name") for x in batch
                            if isinstance(x, dict) and x.get("method") == "tools/call"
                            and isinstance(x.get("params"), dict)
                            and isinstance(x["params"].get("name"), str)
                            and x["params"].get("name") in TOOLS]
        if not out:
            return self._send(202, b"")
        body = json.dumps(out if isinstance(payload, list) else out[0],
                          ensure_ascii=False).encode()
        self._send(200, body)

    def log_message(self, fmt, *args):
        # Never log paths, user agents or attacker-controlled request lines.
        if getattr(self, 'path', '') != "/health" and hasattr(self, 'path') and self._auth():
            # 给 codex++ doctor 判断「ChatGPT 的请求有没有到、返回码是什么」
            try:
                with LOG_LOCK:
                    if ACCESS_LOG.exists() and ACCESS_LOG.stat().st_size >= 5_000_000:
                        ACCESS_LOG.replace(ACCESS_LOG.with_suffix('.previous.log'))
                    with ACCESS_LOG.open("a") as f:
                        f.write(json.dumps({"ts": int(time.time()), "method": self.command,
                                        "path_ok": self._auth(), "status": int(args[1]) if len(args) > 1 and str(args[1]).isdigit() else None,
                                        "rpc_methods": getattr(self, "_rpc_methods", []),
                                        "tools": getattr(self, "_tool_calls", [])}, ensure_ascii=False) + "\n")
            except Exception:
                pass


if __name__ == "__main__":
    print(f"[mcp] 工作区 {ROOT}", flush=True)
    print(f"[mcp] 监听 127.0.0.1:{PORT}，路径 /mcp/<secret>", flush=True)
    BoundedServer(("127.0.0.1", PORT), Handler).serve_forever()
