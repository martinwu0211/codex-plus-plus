"""Isolated MCP protocol regression: no real credential or tool execution."""
import ast
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hmac
from pathlib import Path
import sys
import threading
import unittest


class ProtocolCompatibility(unittest.TestCase):
    def test_initialize_metadata_notifications_and_post_only_transport(self):
        source = Path(__file__).with_name("server.py")
        tree = ast.parse(source.read_text(), filename=str(source))
        selected = [node for node in tree.body
                    if (isinstance(node, ast.FunctionDef) and node.name == "rpc")
                    or (isinstance(node, ast.ClassDef) and node.name in ('Handler', 'BoundedServer'))
                    or (isinstance(node, ast.Assign) and any(
                        isinstance(target, ast.Name) and target.id == "TOOLS"
                        for target in node.targets))]
        def denied(*args, **kwargs):
            raise AssertionError("Protocol test must never execute a workspace tool")
        scope = {"json": json, "sys": sys, "hmac": hmac, "SECRET": "fake-protocol-credential",
                 "ALLOWED_ORIGINS": set(), "MAX_REQUEST": 1_000_000,
                 "TOOL_LIMIT": threading.BoundedSemaphore(2),
                 'threading': threading, 'ThreadingHTTPServer': ThreadingHTTPServer,
                 "BaseHTTPRequestHandler": BaseHTTPRequestHandler}
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("t_"):
                scope[node.name] = denied
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), "exec"), scope)
        handler = scope["Handler"]
        handler.log_message = lambda self, *args: None
        server = scope['BoundedServer'](("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(method, payload=None, path="/mcp/fake-protocol-credential", headers=None):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            try:
                body = json.dumps(payload).encode() if payload is not None else None
                connection.request(method, path, body=body,
                                   headers=headers or {"Content-Type": "application/json"})
                response = connection.getresponse()
                return response.status, dict(response.getheaders()), response.read()
            finally:
                connection.close()
        try:
            status, _, body = request("POST", {"jsonrpc": "2.0", "id": 1,
                "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
            self.assertEqual(status, 200)
            initialized = json.loads(body)["result"]
            self.assertEqual(initialized["protocolVersion"], "2025-06-18")
            self.assertEqual(initialized["capabilities"], {"tools": {}})
            status, _, body = request("POST", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            self.assertEqual(status, 200)
            tools = json.loads(body)["result"]["tools"]
            self.assertEqual({tool["name"] for tool in tools},
                {"workspace_info", "list_directory", "read_file", "search_workspace", "git_status", "git_diff"})
            for tool in tools:
                self.assertEqual(tool["annotations"], {"readOnlyHint": True,
                    "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
            schemas = {tool['name']: tool['inputSchema'] for tool in tools}
            self.assertEqual(schemas['search_workspace']['required'], ['query'])
            self.assertEqual(schemas['read_file']['required'], ['path'])
            status, _, body = request("POST", {"jsonrpc": "2.0", "method": "notifications/initialized"})
            self.assertEqual((status, body), (202, b""))
            status, headers, body = request("GET")
            self.assertEqual((status, headers.get("Allow"), body), (405, "POST", b""))
            status, _, _ = request("GET", path="/mcp/not-authorized")
            self.assertEqual(status, 404)
            status, _, body = request("GET", path="/health")
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["ok"])
            for path in ('/prefix/fake-protocol-credential', '/mcp/fake-protocol-credential/'):
                self.assertEqual(request('GET', path=path)[0], 404)
            self.assertEqual(request('GET', headers={'Origin': 'https://untrusted.invalid'})[0], 403)
            self.assertEqual(request('POST', [], headers={'Content-Length': '-1'})[0], 413)
            for payload in (42, {'jsonrpc': '2.0', 'id': 9, 'method': 'ping', 'params': []}):
                status, _, body = request('POST', payload)
                self.assertEqual(status, 200)
                self.assertIn('error', json.loads(body))
            self.assertEqual(request('POST', {'jsonrpc': '2.0', 'method': 'notifications/unknown'})[0], 202)
            original = scope['TOOLS']['workspace_info']
            scope['TOOLS']['workspace_info'] = (lambda _: 'protocol stub', *original[1:])
            for _ in range(3):
                status, _, body = request('POST', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                    'params': {'name': 'workspace_info', 'arguments': {}}})
                self.assertNotIn('isError', json.loads(body)['result'])
            for _ in range(32):
                server.slots.acquire()
            try:
                self.assertEqual(request('GET', path='/health')[0], 503)
            finally:
                for _ in range(32):
                    server.slots.release()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
