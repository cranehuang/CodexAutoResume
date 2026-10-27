#!/usr/bin/env python3
"""Read-only Codex App Server probe over the current user's Unix socket.

Operations: WebSocket handshake, initialize, thread/loaded/list, thread/list,
account/rateLimits/read. No turn/resume, turn/start, daemon lifecycle, or file writes.
No external Python dependencies.
"""
import argparse
import base64
import datetime
import hashlib
import json
import os
import secrets
import socket
import struct
import sys
import time
from pathlib import Path

GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

class Client:
    def __init__(self, path, timeout=7):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(path)
        self.buf = b''
        key = base64.b64encode(secrets.token_bytes(16)).decode('ascii')
        req = (f'GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n'
               f'Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n'
               f'Sec-WebSocket-Version: 13\r\n\r\n').encode('ascii')
        self.sock.sendall(req)
        while b'\r\n\r\n' not in self.buf:
            chunk = self.sock.recv(8192)
            if not chunk:
                raise RuntimeError('Socket closed during WebSocket handshake')
            self.buf += chunk
            if len(self.buf) > 65536:
                raise RuntimeError('Oversized WebSocket handshake')
        head, self.buf = self.buf.split(b'\r\n\r\n', 1)
        lines = head.decode('iso-8859-1').split('\r\n')
        if not lines[0].startswith('HTTP/1.1 101 '):
            raise RuntimeError(f'WebSocket upgrade rejected: {lines[0]}')
        headers = dict(line.split(':', 1) for line in lines[1:] if ':' in line)
        headers = {k.lower():v.strip() for k,v in headers.items()}
        expected = base64.b64encode(hashlib.sha1((key + GUID).encode('ascii')).digest()).decode('ascii')
        if headers.get('sec-websocket-accept') != expected:
            raise RuntimeError('WebSocket accept key mismatch')

    def close(self):
        self.sock.close()

    def take(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(8192)
            if not chunk:
                raise RuntimeError('Server closed WebSocket unexpectedly')
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send_frame(self, opcode, payload=b''):
        if isinstance(payload, str):
            payload = payload.encode('utf-8')
        n = len(payload)
        if n < 126:
            header = bytes([0x80 | opcode, 0x80 | n])
        elif n < 65536:
            header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack('!H', n)
        else:
            header = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack('!Q', n)
        mask = secrets.token_bytes(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def receive_message(self):
        pieces = []
        while True:
            a, b = self.take(2)
            fin, opcode = bool(a & 0x80), a & 0x0F
            masked = bool(b & 0x80)
            n = b & 0x7F
            if n == 126:
                n = struct.unpack('!H', self.take(2))[0]
            elif n == 127:
                n = struct.unpack('!Q', self.take(8))[0]
            if n > 16 * 1024 * 1024:
                raise RuntimeError('Frame is unexpectedly large')
            mask = self.take(4) if masked else None
            payload = self.take(n)
            if mask:
                payload = bytes(v ^ mask[i % 4] for i, v in enumerate(payload))
            if opcode == 0x8:
                raise RuntimeError('Server sent WebSocket close')
            if opcode == 0x9:
                self.send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode not in (0x0, 0x1):
                continue
            pieces.append(payload)
            if fin:
                return b''.join(pieces).decode('utf-8')

    def rpc(self, request_id, method, params=None):
        obj = {'id':request_id, 'method':method}
        if params is not None:
            obj['params'] = params
        self.send_frame(1, json.dumps(obj, ensure_ascii=False, separators=(',', ':')))
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            msg = json.loads(self.receive_message())
            if msg.get('id') == request_id:
                if 'error' in msg:
                    raise RuntimeError(f'{method}: {msg["error"]}')
                return msg.get('result', {})
        raise TimeoutError(f'{method} timed out')

    def initialized(self):
        self.send_frame(1, '{"method":"initialized","params":{}}')


def clock(ts):
    if ts is None:
        return '?'
    try:
        return datetime.datetime.fromtimestamp(int(ts)).astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')
    except (ValueError, TypeError, OSError):
        return str(ts)


def main():
    p = argparse.ArgumentParser(description='Read-only check of the already-running Codex App Server')
    p.add_argument('--socket', default=str(Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex'))) / 'app-server-control' / 'app-server-control.sock'))
    p.add_argument('--limit', type=int, default=10)
    args = p.parse_args()
    print('Socket:', args.socket)
    c = Client(args.socket)
    try:
        result = c.rpc(1, 'initialize', {'clientInfo':{'name':'codex_guardian_probe','title':'Codex Guardian Read-only Probe','version':'0.1.0'}})
        c.initialized()
        print('Handshake and initialize: OK')
        if isinstance(result, dict):
            print('Server:', {k:result[k] for k in ('serverVersion',) if k in result})

        try:
            res = c.rpc(2, 'thread/loaded/list')
            print('\nLoaded thread IDs:')
            data = res.get('data') if isinstance(res, dict) else None
            for tid in (data or []):
                print('  ', tid)
            if not data:
                print('  (none returned)')
        except Exception as e:
            print('Loaded thread query unsupported/error:', e)

        try:
            res = c.rpc(3, 'thread/list', {'limit':max(1, min(args.limit, 50)), 'sortKey':'recency_at'})
            print('\nRecent threads (no message content):')
            for thread in res.get('data', []):
                print('  ID:', thread.get('id'), '| cwd:',thread.get('cwd'), '| status:', thread.get('status'))
            print('  Further pages:', bool(res.get('nextCursor')))
        except Exception as e:
            print('Thread list query unsupported/error:', e)

        try:
            res = c.rpc(4, 'account/rateLimits/read')
            limits = res.get('rateLimits') or {}
            print('\nRate limits:')
            print('  ordinaryUsageAllowed:', res.get('ordinaryUsageAllowed'))
            print('  rateLimitReachedType:', limits.get('rateLimitReachedType'))
            for name in ('primary', 'secondary'):
                w = limits.get(name)
                if isinstance(w, dict):
                    print(f'  {name}: used={w.get("usedPercent")}%, window={w.get("windowDurationMins")}m, resets={clock(w.get("resetsAt"))}')
                else:
                    print(f'  {name}: {w}')
        except Exception as e:
            print('Rate limits query unsupported/error:', e)
    finally:
        c.close()

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print('PROBE FAILED:', type(e).__name__, str(e), file=sys.stderr)
        sys.exit(1)
