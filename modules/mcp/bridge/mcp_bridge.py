#!/usr/bin/env python3
"""Tiny stdio MCP client for any user-scope MCP server in ~/.claude.json.

Why it exists: scheduled tasks, scripts, launchd jobs, and sessions that started before a
server was added often do NOT have that server's tools loaded. Without a bridge they fall
back to the browser, which is slower and less reliable. This script starts the server the
same way Claude Code does (command/args/env from ~/.claude.json), calls one tool, prints
the result and exits. Launchers read their own secrets (Keychain, .env); nothing here
prints them.

usage:
  mcp_bridge.py <server> list                              # tools + input schemas
  mcp_bridge.py <server> call <tool> '<json args>' [timeout_s] [--max-chars N]
  e.g. mcp_bridge.py canvas call list_announcements '{"course_identifier": "1234567"}'
       mcp_bridge.py google-workspace call manage_email '{"operation":"search","email":"you@gmail.com","query":"newer_than:1d"}'
       mcp_bridge.py coursework call gradescope_assignments '{"course": "CS 101"}'

Results print in full by default (an old silent 20,000-character cut once dropped half a
course schedule page). --max-chars N is opt-in; when it cuts, "[TRUNCATED: X of Y chars]"
goes to stderr and as the last line of stdout.
"""
import json, os, subprocess, sys, threading, time

CFG = os.path.expanduser('~/.claude.json')


SERVER = None


def start():
    s = json.load(open(CFG))['mcpServers'][SERVER]
    env = dict(os.environ)
    env.update(s.get('env') or {})
    cmd = [s['command']] + list(s.get('args') or [])
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, text=True, bufsize=1)


class Client:
    def __init__(self):
        self.p = start()
        self.n = 0
        self.resp = {}
        self.lock = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._drain_err, daemon=True).start()

    def _drain_err(self):
        for line in self.p.stderr:
            line = line.rstrip()
            if line and os.environ.get('GW_DEBUG'):
                print('[server]', line[:300], file=sys.stderr)

    def _read(self):
        for line in self.p.stdout:
            try:
                m = json.loads(line)
            except Exception:
                continue
            if 'id' in m and ('result' in m or 'error' in m):
                with self.lock:
                    self.resp[m['id']] = m
                    self.lock.notify_all()

    def send(self, method, params=None, notify=False):
        msg = {'jsonrpc': '2.0', 'method': method}
        if params is not None:
            msg['params'] = params
        if not notify:
            self.n += 1
            msg['id'] = self.n
        self.p.stdin.write(json.dumps(msg) + '\n')
        self.p.stdin.flush()
        return None if notify else self.n

    def call(self, method, params=None, timeout=120):
        i = self.send(method, params)
        end = time.time() + timeout
        with self.lock:
            while i not in self.resp:
                left = end - time.time()
                if left <= 0:
                    raise TimeoutError(f'{method} timed out after {timeout}s')
                self.lock.wait(left)
            return self.resp.pop(i)

    def init(self):
        r = self.call('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                     'clientInfo': {'name': 'mcp-bridge', 'version': '1.0'}}, timeout=180)
        self.send('notifications/initialized', notify=True)
        return r

    def close(self):
        try:
            self.p.terminate()
        except Exception:
            pass


def take_max_chars(argv):
    """Strip --max-chars N / --max-chars=N out of argv. Returns (N or None for no cap, the rest)."""
    n, rest, it = None, [], iter(argv)
    for a in it:
        if a != '--max-chars' and not a.startswith('--max-chars='):
            rest.append(a)
            continue
        v = a.split('=', 1)[1] if '=' in a else next(it, '')
        n = int(v) if v.isdigit() else 0
        if n <= 0:
            sys.exit(f'--max-chars needs a positive whole number, got {v!r}')
    return n, rest


def emit(text, max_chars):
    """Print text whole, or cut at max_chars with a [TRUNCATED: X of Y chars] marker on stdout and stderr."""
    if max_chars is None or len(text) <= max_chars:
        print(text)
        return
    mark = f'[TRUNCATED: {max_chars} of {len(text)} chars]'
    print(text[:max_chars])
    print(mark)
    print(f'mcp_bridge: {mark} (--max-chars {max_chars}; leave it off for the full result)', file=sys.stderr)


def main():
    global SERVER
    max_chars, sys.argv = take_max_chars(sys.argv)
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    SERVER = sys.argv.pop(1)
    c = Client()
    try:
        c.init()
        if sys.argv[1] == 'list':
            r = c.call('tools/list')
            for t in r['result']['tools']:
                d = (t.get('description') or '').replace('\n', ' ')
                print('##', t['name'], '-', d[:200] + ('…' if len(d) > 200 else ''))
                print(json.dumps(t.get('inputSchema', {})))
        elif sys.argv[1] == 'call':
            args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
            timeout = int(sys.argv[4]) if len(sys.argv) > 4 else 120
            r = c.call('tools/call', {'name': sys.argv[2], 'arguments': args}, timeout=timeout)
            if 'error' in r:
                emit('ERROR: ' + json.dumps(r['error']), max_chars)
            else:
                parts = [p.get('text', json.dumps(p)) for p in r['result'].get('content', [])]
                if parts:
                    emit('\n'.join(parts), max_chars)
    finally:
        c.close()


if __name__ == '__main__':
    main()
