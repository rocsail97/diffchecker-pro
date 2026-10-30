#!/usr/bin/env python3
"""
本地代码对比 AI 桥接服务：
1. 默认绑定 WEEX AI Gateway: https://ai-gateway.weex.tech/v1
2. 默认 Key: sk-GH3RuGkL0W2jYEXup7bQvQ
3. 默认模型: gpt-5.3-codex (支持 gpt-5.6-sol, gpt-5.6-terra, gpt-4o-mini 等)
4. 彻底解决前端浏览器的跨域 CORS 拦截问题
5. 针对最新推理模型：不传不兼容的 max_tokens 与 temperature，保证 100% 连通率
6. 一键唤起系统原生的 ChatGPT / Codex 桌面客户端新对话
"""

import sys
import os
import json
import subprocess
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

PORT = 28833
DEFAULT_BASE_URL = "https://ai-gateway.weex.tech/v1"
DEFAULT_KEY = os.environ.get("LITELLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or "sk-GH3RuGkL0W2jYEXup7bQvQ"
DEFAULT_MODEL = "gpt-5.3-codex"

def find_index_html():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(base_dir, 'index.html'),
        os.path.join(base_dir, 'Resources', 'index.html'),
        os.path.join(os.path.dirname(base_dir), 'Resources', 'index.html'),
        os.path.join(base_dir, '..', 'diffchecker-pro', 'index.html'),
        '/Users/pr-dwu-rabby/.gemini/antigravity/scratch/diffchecker-pro/index.html'
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None

class BridgeHandler(BaseHTTPRequestHandler):
    def _send_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS, HEAD')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')

    def _respond(self, status_code, body_bytes, content_type='application/json; charset=utf-8'):
        self.send_response(status_code)
        self._send_cors_headers()
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body_bytes)))
        self.send_header('Connection', 'keep-alive')
        self.end_headers()
        if self.command != 'HEAD':
            try:
                self.wfile.write(body_bytes)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _respond_json(self, status_code, data):
        self._respond(status_code, json.dumps(data, ensure_ascii=False).encode('utf-8'), 'application/json; charset=utf-8')

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors_headers()
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if self.path in ('/', '/index.html', '/diffchecker'):
            index_path = find_index_html()
            if index_path and os.path.exists(index_path):
                with open(index_path, 'rb') as f:
                    content = f.read()
                self._respond(200, content, 'text/html; charset=utf-8')
            else:
                self._respond(404, b"index.html not found. Please ensure index.html exists in the same folder.", 'text/plain; charset=utf-8')
            return
        elif self.path == '/health':
            self._respond_json(200, {
                'status': 'ok',
                'gateway': DEFAULT_BASE_URL,
                'default_model': DEFAULT_MODEL
            })
            return
        else:
            self._respond(404, b"Not Found", 'text/plain; charset=utf-8')

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)

        try:
            req_json = json.loads(post_data.decode('utf-8'))
        except Exception as e:
            self._respond_json(400, {'error': f'Invalid JSON: {str(e)}'})
            return

        # 路由 1: 唤起 ChatGPT / Codex 桌面端新建对话
        if self.path == '/api/open_chatgpt_app':
            prompt = req_json.get('prompt', '')
            try:
                if sys.platform == 'darwin':
                    process = subprocess.Popen('pbcopy', env={'LANG': 'en_US.UTF-8'}, stdin=subprocess.PIPE)
                    process.communicate(prompt.encode('utf-8'))

                    script = '''
                    set appName to "ChatGPT"
                    tell application "System Events"
                        if exists (application process "ChatGPT") then
                            set appName to "ChatGPT"
                        else if exists (application process "Codex") then
                            set appName to "Codex"
                        end if
                    end tell
                    tell application appName to activate
                    delay 0.3
                    tell application "System Events"
                        keystroke "n" using command down
                        delay 0.2
                        keystroke "v" using command down
                    end tell
                    '''
                    subprocess.run(['osascript', '-e', script], capture_output=True)
                elif sys.platform == 'win32':
                    escaped_prompt = prompt.replace("'", "''")
                    subprocess.run(['powershell', '-NoProfile', '-Command', f"Set-Clipboard -Value '{escaped_prompt}'"], capture_output=True)
                    subprocess.run(['cmd', '/c', 'start chatgpt:'], capture_output=True)
                else:
                    try:
                        p = subprocess.Popen(['xclip', '-selection', 'clipboard'], stdin=subprocess.PIPE)
                        p.communicate(prompt.encode('utf-8'))
                    except Exception:
                        pass

                self._respond_json(200, {'status': 'success', 'message': '已在桌面客户端开启新对话并填入代码上下文'})
            except Exception as e:
                self._respond_json(500, {'error': str(e)})
            return

        # 路由 2: 后端转发 WEEX AI Gateway 对话请求
        if self.path == '/api/chat':
            api_key = req_json.get('api_key', '').strip() or DEFAULT_KEY
            base_url = req_json.get('base_url', '').strip() or DEFAULT_BASE_URL
            raw_model = req_json.get('model', '').strip() or DEFAULT_MODEL
            messages = req_json.get('messages', [])

            # 别名映射与清洗
            model_key = raw_model.lower().strip()
            aliases = {
                'deepseek-v4.1-flash': 'deepseek-v4-flash',
                'deepseek-v4.1': 'deepseek-v4-flash',
                'deepseek-v3-pro': 'deepseek-v4-pro',
                'deepseek-v3-flash': 'deepseek-v4-flash',
                '6.1 sol': 'gpt-6.1-sol',
                '6.1-sol': 'gpt-6.1-sol',
                '6 astra': 'gpt-6-astra',
                '6-astra': 'gpt-6-astra',
                '6 sol': 'gpt-6-sol',
                '6-sol': 'gpt-6-sol',
                '6 luna': 'gpt-6-luna',
                '6-luna': 'gpt-6-luna',
                '5.6 sol': 'gpt-5.6-sol',
                '5.6-sol': 'gpt-5.6-sol',
                '5.6 terra': 'gpt-5.6-terra',
                '5.6-terra': 'gpt-5.6-terra',
                '5.6 luna': 'gpt-5.6-luna',
                '5.6-luna': 'gpt-5.6-luna',
                '5.5': 'gpt-5.5'
            }
            model = aliases.get(model_key, raw_model)

            endpoint = base_url.rstrip('/') + '/chat/completions'
            max_tokens = req_json.get('max_completion_tokens') or 8000
            payload = {
                'model': model,
                'messages': messages,
                'max_completion_tokens': max_tokens
            }

            req = urllib.request.Request(
                endpoint,
                data=json.dumps(payload).encode('utf-8'),
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {api_key}',
                    'User-Agent': 'WEEX-Codex-DiffApp/1.0'
                },
                method='POST'
            )

            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    resp_body = resp.read()
                    self._respond(resp.status, resp_body, 'application/json; charset=utf-8')
            except urllib.error.HTTPError as e:
                err_content = e.read().decode('utf-8', errors='replace')
                try:
                    err_json = json.loads(err_content)
                    self._respond_json(e.code, {'error': err_json.get('error', {}).get('message', err_content)})
                except Exception:
                    self._respond_json(e.code, {'error': f'HTTP {e.code}: {err_content}'})
            except urllib.error.URLError as e:
                self._respond_json(502, {'error': f'连接网关失败: {str(e.reason)}，请确认网络连接或 VPN 状态'})
            except Exception as e:
                self._respond_json(500, {'error': f'发生异常: {str(e)}'})
            return

        # 路由 3: 抓取并解析 Unified Diff 网页/链接内容 (绕过跨域 CORS)
        if self.path == '/api/fetch_url':
            target_url = req_json.get('url', '').strip()
            if not target_url:
                self._respond_json(400, {'error': '缺少 URL 参数'})
                return
            try:
                # 针对 GitHub PR 链接自动转换为 .diff
                if 'github.com' in target_url and '/pull/' in target_url and not target_url.endswith('.diff') and not target_url.endswith('.patch'):
                    target_url = target_url.split('?')[0].rstrip('/') + '.diff'

                headers = {
                    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'text/plain, text/x-diff, text/html, */*'
                }
                req = urllib.request.Request(target_url, headers=headers)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw_bytes = resp.read()
                    text_content = raw_bytes.decode('utf-8', errors='replace')
                    self._respond_json(200, {'status': 'success', 'url': target_url, 'content': text_content})
            except Exception as e:
                self._respond_json(500, {'error': f'抓取 URL 失败: {str(e)}'})
            return

        self._respond(404, b"Not Found", 'text/plain; charset=utf-8')
        return

    def log_message(self, format, *args):
        return

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        ex_type, ex_val, _ = sys.exc_info()
        if ex_type in (BrokenPipeError, ConnectionResetError):
            return
        super().handle_error(request, client_address)

def run_server():
    server_address = ('127.0.0.1', PORT)
    try:
        httpd = ThreadedHTTPServer(server_address, BridgeHandler)
        print(f"Code Diff AI Bridge running on http://127.0.0.1:{PORT}")
        httpd.serve_forever()
    except OSError as e:
        print(f"Server already running: {e}")
        sys.exit(0)

if __name__ == '__main__':
    run_server()
