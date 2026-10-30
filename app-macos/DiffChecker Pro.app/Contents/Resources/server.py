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
import re
import json
import hashlib
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

def split_diff_files(diff_text):
    files = []
    current_file = None
    for line in diff_text.split('\n'):
        if line.startswith('diff --git '):
            if current_file:
                current_file['content'] = '\n'.join(current_file['lines'])
                files.append(current_file)
            m = re.match(r'diff --git a/(.*?) b/(.*)', line)
            path = m.group(2) if m else 'unknown'
            path_sha1 = hashlib.sha1(path.encode('utf-8')).hexdigest()
            current_file = {
                'path': path,
                'sha1': path_sha1,
                'lines': [line]
            }
        elif current_file:
            current_file['lines'].append(line)
    if current_file:
        current_file['content'] = '\n'.join(current_file['lines'])
        files.append(current_file)
    return files

def find_or_init_repo(host, repo_full_path):
    repo_name = repo_full_path.split('/')[-1]
    candidates = [
        f'/Users/pr-dwu-rabby/Documents/ChatGPT/weex/{repo_name}',
        f'/Users/pr-dwu-rabby/IdeaProjects/Multica/{repo_name}',
        os.path.expanduser(f'~/Documents/ChatGPT/weex/{repo_name}'),
        os.path.expanduser(f'~/IdeaProjects/Multica/{repo_name}'),
        os.path.expanduser(f'~/{repo_name}'),
        os.path.expanduser(f'~/.cache/diffchecker-pro/repos/{repo_name}')
    ]
    for c in candidates:
        if os.path.isdir(os.path.join(c, '.git')):
            return os.path.join(c, '.git')
        if os.path.isfile(os.path.join(c, 'HEAD')):
            return c

    cache_dir = os.path.expanduser(f'~/.cache/diffchecker-pro/repos/{repo_name}')
    os.makedirs(cache_dir, exist_ok=True)
    subprocess.run(['git', 'init', '--bare', cache_dir], capture_output=True)
    remote_url = f'git@{host}:{repo_full_path}.git'
    subprocess.run(['git', f'--git-dir={cache_dir}', 'remote', 'add', 'origin', remote_url], capture_output=True)
    return cache_dir

def fetch_git_diff_by_url(target_url):
    anchor = target_url.split('#')[1] if '#' in target_url else ''
    clean_url = target_url.split('#')[0]

    # 1. Commit URL: e.g. https://git.weex.tech/fluidity/analysis_script/-/commit/013264fcd7c260e0b3d6d2957a1d650b7b239562
    m_commit = re.search(r'https?://([^/]+)/(.+?)/(?:-/)?(?:commit|commits)/([0-9a-fA-F]{7,40})', clean_url)
    if m_commit:
        host = m_commit.group(1)
        repo = m_commit.group(2).removesuffix('.git')
        commit = m_commit.group(3)
        git_dir = find_or_init_repo(host, repo)
        remote = f'git@{host}:{repo}.git'
        res = subprocess.run(['git', f'--git-dir={git_dir}', 'show', commit], capture_output=True, text=True)
        if res.returncode != 0:
            subprocess.run(['git', f'--git-dir={git_dir}', 'fetch', remote, commit], capture_output=True)
            res = subprocess.run(['git', f'--git-dir={git_dir}', 'show', 'FETCH_HEAD'], capture_output=True, text=True)
        if res.returncode == 0:
            files = split_diff_files(res.stdout)
            matched = None
            if anchor:
                for f in files:
                    if f['sha1'] == anchor:
                        matched = f
                        break
            if not matched and files:
                matched = files[0]
            return {
                'status': 'success',
                'type': 'gitlab_commit',
                'repo': repo,
                'target': commit,
                'file_hash': anchor,
                'matched_file': matched['path'] if matched else None,
                'content': matched['content'] if matched else res.stdout,
                'files': [{'path': f['path'], 'sha1': f['sha1'], 'content': f['content'], 'lines_count': len(f['lines'])} for f in files]
            }

    # 2. Merge Request URL: e.g. https://git.weex.tech/fluidity/analysis_script/-/merge_requests/1719/diffs#3f610f8a847bf70a384de4c1fbe73d3b83bb3243
    m_mr = re.search(r'https?://([^/]+)/(.+?)/(?:-/)?(?:merge_requests|merge_request)/(\d+)', clean_url)
    if m_mr:
        host = m_mr.group(1)
        repo = m_mr.group(2).removesuffix('.git')
        mr_id = m_mr.group(3)
        git_dir = find_or_init_repo(host, repo)
        remote = f'git@{host}:{repo}.git'
        ref = f'refs/merge-requests/{mr_id}/head'
        subprocess.run(['git', f'--git-dir={git_dir}', 'fetch', remote, ref], capture_output=True)
        res = subprocess.run(['git', f'--git-dir={git_dir}', 'show', 'FETCH_HEAD'], capture_output=True, text=True)
        if res.returncode == 0:
            files = split_diff_files(res.stdout)
            matched = None
            if anchor:
                for f in files:
                    if f['sha1'] == anchor:
                        matched = f
                        break
            if not matched and files:
                matched = files[0]
            return {
                'status': 'success',
                'type': 'gitlab_mr',
                'repo': repo,
                'target': f'MR !{mr_id}',
                'file_hash': anchor,
                'matched_file': matched['path'] if matched else None,
                'content': matched['content'] if matched else res.stdout,
                'files': [{'path': f['path'], 'sha1': f['sha1'], 'content': f['content'], 'lines_count': len(f['lines'])} for f in files]
            }
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

        # 路由 3: 抓取并解析 Unified Diff 网页/链接内容 (支持 git.weex.tech GitLab Commit/MR 及 GitHub)
        if self.path == '/api/fetch_url':
            target_url = req_json.get('url', '').strip()
            if not target_url:
                self._respond_json(400, {'error': '缺少 URL 参数'})
                return

            # 1. 优先使用本地 Git / SSH 获取 git.weex.tech GitLab Commit、Merge Request 或本地仓库差异
            try:
                git_res = fetch_git_diff_by_url(target_url)
                if git_res and git_res.get('status') == 'success':
                    git_res['url'] = target_url
                    self._respond_json(200, git_res)
                    return
            except Exception as e:
                print(f"Git CLI fetch warning: {e}")

            # 2. 通用 HTTP 方式回退
            try:
                fetch_url = target_url
                # 针对 GitHub PR 链接自动转换为 .diff
                if 'github.com' in fetch_url and '/pull/' in fetch_url and not fetch_url.endswith('.diff') and not fetch_url.endswith('.patch'):
                    fetch_url = fetch_url.split('?')[0].rstrip('/') + '.diff'

                headers = {
                    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'text/plain, text/x-diff, text/html, */*'
                }
                req = urllib.request.Request(fetch_url, headers=headers)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw_bytes = resp.read()
                    text_content = raw_bytes.decode('utf-8', errors='replace')
                    self._respond_json(200, {
                        'status': 'success',
                        'url': target_url,
                        'content': text_content,
                        'files': [{'path': 'diff_content', 'sha1': '', 'content': text_content}]
                    })
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
