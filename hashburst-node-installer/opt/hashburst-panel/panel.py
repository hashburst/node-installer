#!/usr/bin/env python3
"""
HashBurst Admin Panel — localhost:8088
Access via SSH tunnel: ssh -L 8088:127.0.0.1:8088 root@SERVER_IP
Then open: http://127.0.0.1:8088/?secret=YOUR_PANEL_SECRET
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import subprocess, json, os, datetime, pathlib, urllib.request, secrets

HOST          = "127.0.0.1"
PORT          = 8088
PANEL_SECRET  = os.environ.get("HB_PANEL_SECRET", "CHANGE_IN_ENV")
STATE_DIR     = pathlib.Path("/var/lib/hashburst")
LOG_DIR       = pathlib.Path("/var/log/hashburst")

def sh(cmd, timeout=8):
    try:
        return subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT, timeout=timeout).decode("utf-8","replace").strip()
    except Exception as e:
        return str(e)

def get_tep_status():
    try:
        with urllib.request.urlopen("http://127.0.0.1:47778/", timeout=2) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

def get_node_status():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8009/api/status", timeout=3) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

def tail_log(path, lines=50):
    try:
        return subprocess.check_output(["tail","-n",str(lines),path], stderr=subprocess.DEVNULL).decode("utf-8","replace")
    except:
        return "(log not available)"

CSS = """
body{font-family:Arial,sans-serif;background:#0D1117;color:#ECF0F1;margin:0;padding:0}
header{background:#1A2634;border-bottom:2px solid #2E86C1;padding:16px 32px;display:flex;align-items:center;justify-content:space-between}
header h1{margin:0;color:#2E86C1;font-size:20px;font-weight:bold}
header span{color:#808B96;font-size:12px}
main{max-width:960px;margin:0 auto;padding:24px 32px}
.card{background:#1A2634;border:1px solid #2C3E50;border-radius:8px;padding:20px;margin-bottom:20px}
.card h2{margin:0 0 16px 0;color:#2E86C1;font-size:16px;font-weight:bold;border-bottom:1px solid #2C3E50;padding-bottom:8px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:8px 12px;text-align:left;border-bottom:1px solid #2C3E5066}
th{color:#2E86C1;font-weight:bold}
.ok{color:#2ECC71;font-weight:bold}
.fail{color:#E74C3C;font-weight:bold}
a{color:#2E86C1;text-decoration:none}
a:hover{text-decoration:underline}
code{background:#0D1117;padding:2px 6px;border-radius:4px;font-family:monospace;font-size:12px}
pre{background:#0D1117;border:1px solid #2C3E50;border-radius:6px;padding:16px;overflow:auto;font-size:12px;color:#2ECC71;max-height:300px}
ul{padding-left:20px}li{margin:6px 0}
footer{text-align:center;color:#5D6D7E;font-size:11px;padding:20px;border-top:1px solid #2C3E50;margin-top:40px}
"""

class PanelHandler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def auth_ok(self):
        qs = parse_qs(urlparse(self.path).query)
        return PANEL_SECRET != "CHANGE_IN_ENV" and qs.get("secret",[""])[0] == PANEL_SECRET

    def json_response(self, data, code=200):
        body = json.dumps(data, indent=2, default=str).encode()
        self.send_response(code); self.send_header("Content-Type","application/json")
        self.send_header("X-Frame-Options","DENY"); self.end_headers(); self.wfile.write(body)

    def html_response(self, html, code=200):
        body = html.encode()
        self.send_response(code); self.send_header("Content-Type","text/html; charset=utf-8")
        self.send_header("X-Frame-Options","DENY"); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if not self.auth_ok():
            self.json_response({"error": "Unauthorized — add ?secret=YOUR_PANEL_SECRET"}, 401); return

        routes = {
            "/health":       self.serve_health,
            "/tep":          lambda: self.json_response(get_tep_status()),
            "/node":         lambda: self.json_response(get_node_status()),
            "/token/create": self.serve_token_create,
            "/logs/clusters":lambda: self.json_response({"log": tail_log("/var/log/hashburst/clusters.log")}),
            "/logs/tep":     lambda: self.json_response({"log": tail_log("/var/log/hashburst/tep.log")}),
            "/logs/node":    lambda: self.json_response({"log": tail_log("/var/log/hashburst/node5.log")}),
            "/logs/nginx":   lambda: self.json_response({"log": tail_log("/var/log/nginx/error.log")}),
        }
        handler = routes.get(path)
        if handler: handler()
        elif path in ("/", ""): self.serve_dashboard()
        else: self.json_response({"error": "Not found"}, 404)

    def serve_health(self):
        self.json_response({
            "time_utc":      datetime.datetime.utcnow().isoformat()+"Z",
            "nginx":         sh("systemctl is-active nginx"),
            "php_fpm":       sh("systemctl is-active php8.3-fpm"),
            "hashburst_tep": sh("systemctl is-active hashburst-tep"),
            "hashburst_node":sh("systemctl is-active hashburst-node"),
            "disk":          sh("df -h / | tail -n 1"),
            "mem":           sh("free -h | grep Mem"),
            "load":          sh("uptime"),
            "tep":           get_tep_status(),
            "node":          get_node_status(),
        })

    def serve_token_create(self):
        token = secrets.token_hex(24)
        now = int(__import__("time").time())
        exp = now + 604800
        row = json.dumps({"token": token, "iat": now, "exp": exp, "enabled": True})
        tf  = STATE_DIR / "whitelist_tokens.jsonl"
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(tf, "a") as f: f.write(row+"\n")
        import os as _os; _os.chmod(tf, 0o640)
        self.json_response({"token": token, "expires": datetime.datetime.utcfromtimestamp(exp).isoformat()+"Z"})

    def serve_dashboard(self):
        svc = {
            "nginx":  sh("systemctl is-active nginx"),
            "fpm":    sh("systemctl is-active php8.3-fpm"),
            "tep":    sh("systemctl is-active hashburst-tep"),
            "node":   sh("systemctl is-active hashburst-node"),
        }
        tep  = get_tep_status()
        node = get_node_status()
        peers_online = tep.get("stats",{}).get("peers_online","?") if "error" not in tep else "ERR"
        block_height = node.get("blockHeight","?") if "error" not in node else "ERR"
        qs = parse_qs(urlparse(self.path).query)
        secret = qs.get("secret",[""])[0]

        def badge(s): return f'<span class="{"ok" if s=="active" else "fail"}">{s}</span>'
        def link(path, label): return f'<a href="{path}?secret={secret}">{label}</a>'

        html = f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><title>HashBurst Admin Panel</title>
<style>{CSS}</style></head><body>
<header>
  <h1>HashBurst Admin Panel</h1>
  <span>{sh("hostname")} &nbsp;|&nbsp; {datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}</span>
</header>
<main>

<div class="card">
<h2>Services</h2>
<table>
<tr><th>Service</th><th>Status</th></tr>
<tr><td>nginx</td><td>{badge(svc["nginx"])}</td></tr>
<tr><td>php8.3-fpm</td><td>{badge(svc["fpm"])}</td></tr>
<tr><td>hashburst-tep</td><td>{badge(svc["tep"])}</td></tr>
<tr><td>hashburst-node</td><td>{badge(svc["node"])}</td></tr>
</table>
</div>

<div class="card">
<h2>Network</h2>
<table>
<tr><th>Parameter</th><th>Value</th></tr>
<tr><td>Block Height</td><td><strong style="color:#2ECC71">{block_height}</strong></td></tr>
<tr><td>TEP Peers Online</td><td><strong style="color:#2ECC71">{peers_online}</strong></td></tr>
<tr><td>TEP UDP Port</td><td><code>47777</code></td></tr>
<tr><td>TEP Crypto</td><td><code>{tep.get("crypto_mode","—")}</code></td></tr>
<tr><td>Node RPC Port</td><td><code>8009</code></td></tr>
<tr><td>Node P2P Port</td><td><code>30307</code></td></tr>
</table>
</div>

<div class="card">
<h2>Actions</h2>
<ul>
  <li>{link("/health", "Full Health JSON")}</li>
  <li>{link("/token/create", "Generate whitelist token (7 days)")}</li>
  <li>{link("/node", "HashBurst Node status")}</li>
  <li>{link("/tep", "HB-TEP status")}</li>
  <li>{link("/logs/clusters", "clusters.php log")}</li>
  <li>{link("/logs/tep", "HB-TEP log")}</li>
  <li>{link("/logs/node", "Node log")}</li>
  <li>{link("/logs/nginx", "nginx error log")}</li>
</ul>
</div>

<div class="card">
<h2>SSH Tunnel Access</h2>
<p>From your local machine:</p>
<pre>ssh -L 8088:127.0.0.1:8088 root@SERVER_IP</pre>
<p>Then open in browser:</p>
<pre>http://127.0.0.1:8088/?secret=YOUR_PANEL_SECRET</pre>
</div>

</main>
<footer>HashBurst Admin Panel &nbsp;|&nbsp; localhost only &nbsp;|&nbsp; SSH tunnel required</footer>
</body></html>"""
        self.html_response(html)

if __name__ == "__main__":
    print(f"[HashBurst Panel] Starting on http://{HOST}:{PORT}")
    HTTPServer((HOST, PORT), PanelHandler).serve_forever()
