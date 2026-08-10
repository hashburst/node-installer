#!/usr/bin/env python3
"""
HB-Files Panel v2.0 -- Strato B2 (cifratura sovrana lato client)
Browser chiama /proxy/* su :8092. Il panel inoltra al backend :8091.
La cifratura/decifratura avviene NEL BROWSER: il panel non vede mai chiaro.
Single SSH tunnel: ssh -L 8092:127.0.0.1:8092 synapta@85.233.199.35
"""
import os, json, urllib.request, urllib.error, logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

FILES_API = os.environ.get('HB_FILES_API', 'http://127.0.0.1:8091')
PORT      = int(os.environ.get('HB_FILES_PANEL_PORT', '8092'))
HTML_FILE = os.environ.get('HB_PANEL_HTML', '/opt/hashburst-files/panel.html')
LOG = logging.getLogger('hb-panel')

def proxy_to_backend(method, path, headers, body=b''):
    url = FILES_API + path
    req = urllib.request.Request(url, data=body or None, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return (r.status, r.headers.get('Content-Type','application/json'),
                    r.headers.get('Content-Disposition',''), r.read())
    except urllib.error.HTTPError as e:
        return e.code, 'application/json', '', e.read()
    except Exception as ex:
        return 502, 'application/json', '', json.dumps({'error': str(ex)}).encode()

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _html(self):
        try:
            body = Path(HTML_FILE).read_bytes()
        except Exception:
            body = b'<h1>Panel HTML non trovato</h1>'
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self, method, bpath):
        # inoltra header di auth e cifratura
        fwd = {}
        for h in ('X-Api-Key','X-Signature','X-HB-Encrypted','Content-Type','X-HB-Token'):
            v = self.headers.get(h)
            if v: fwd[h] = v
        n = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(n) if n else b''
        st, rct, rcd, rb = proxy_to_backend(method, '/'+bpath, fwd, body)
        self.send_response(st)
        self.send_header('Content-Type', rct)
        if rcd: self.send_header('Content-Disposition', rcd)
        self.send_header('Content-Length', str(len(rb)))
        self.end_headers()
        self.wfile.write(rb)

    def do_GET(self):
        p = self.path
        if p == '/' or p == '/index.html': self._html(); return
        if p.startswith('/proxy/'): self._proxy('GET', p[7:]); return
        self.send_response(404); self.end_headers()

    def do_POST(self):
        p = self.path
        if p.startswith('/proxy/'): self._proxy('POST', p[7:]); return
        self.send_response(404); self.end_headers()

    def do_DELETE(self):
        p = self.path
        if p.startswith('/proxy/'): self._proxy('DELETE', p[7:]); return
        self.send_response(404); self.end_headers()

def main():
    logging.basicConfig(level=logging.INFO)
    LOG.info("Panel B2 on :%d  backend %s  html %s", PORT, FILES_API, HTML_FILE)
    HTTPServer(('127.0.0.1', PORT), H).serve_forever()

if __name__ == '__main__':
    main()
