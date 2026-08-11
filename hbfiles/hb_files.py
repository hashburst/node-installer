#!/usr/bin/env python3
"""
HB-Files v1.0 -- HashBurst Private Cloud Storage
Tenant-aware file server with TEP addressing and shareable TEP links.

TEP share link format:
  tep://<node_tep_pubkey>/<share_token>

HTTP share link (direct IP, no DNS required):
  http://85.233.199.35/files/<share_token>

Domain share link (optional, if domain associated):
  https://files.example.com/files/<share_token>

API endpoints:
  POST   /api/upload              upload file (multipart/form-data)
  GET    /api/files               list files (tenant scoped)
  GET    /api/files/<id>          file metadata
  DELETE /api/files/<id>          delete file
  POST   /api/share/<file_id>     create TEP share link
  GET    /api/share/<token>       resolve share token
  GET    /files/<token>           public download (nginx or direct)
  GET    /api/storage             storage stats (admin)
  GET    /api/tenants             list tenants (admin)
  POST   /api/tenants             create tenant (admin)
  GET    /health                  health + TEP info
"""

from __future__ import annotations
import argparse, hashlib, json, logging, mimetypes, os
import secrets, shutil, threading, time, uuid
from dataclasses import dataclass, asdict, field
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

# PATCH-STRATO-A: auth apikey+signature contro list.json
import hb_registry
import hb_ipfs
import hb_capacity
# PATCH-GRADINO-3: contabilita' capacita' (accountant creato dopo _storage/registry)
# PATCH-STRATO-B1: client IPFS piano privato (cloud sovrano)
IPFS_PRIVATE_API = os.environ.get('HB_IPFS_PRIVATE_API', 'http://127.0.0.1:5011')
_ipfs = hb_ipfs.IPFSClient(IPFS_PRIVATE_API)
_accountant = None  # PATCH-GRADINO-3: inizializzato in main() con il registry

def _ipfs_peer_count():
    # PATCH-PUBLIC-SUMMARY: conta i peer della rete IPFS privata (solo numero)
    import urllib.request, json as _json
    try:
        api = os.environ.get('HB_IPFS_PRIVATE_API', 'http://127.0.0.1:5011')
        req = urllib.request.Request(api + '/api/v0/swarm/peers', method='POST')
        with urllib.request.urlopen(req, timeout=5) as r:
            data = _json.loads(r.read().decode('utf-8', 'replace'))
        peers = data.get('Peers') or []
        return len(peers)
    except Exception:
        return None
APIKEY_HEADER    = 'X-Api-Key'
SIGNATURE_HEADER = 'X-Signature'

# ── Configuration ──────────────────────────────────────────────────────────────
STORAGE_BASE  = Path(os.environ.get('HB_FILES_STORAGE', '/var/lib/hashburst/files'))
METADATA_DIR  = Path(os.environ.get('HB_FILES_META',    '/var/lib/hashburst/files-meta'))
LOG_FILE      = Path('/var/log/hashburst/hb-files.log')
ADMIN_SECRET  = os.environ.get('HB_ADMIN_SECRET', '')
BIND_ADDRESS  = os.environ.get('HB_FILES_BIND', '127.0.0.1')
TOKEN_HEADER  = 'X-HB-Token'
MAX_UPLOAD_MB = int(os.environ.get('HB_FILES_MAX_MB', '10240'))
SHARE_TTL_SEC = int(os.environ.get('HB_FILES_SHARE_TTL', '604800'))  # 7 days
PORT          = int(os.environ.get('HB_FILES_PORT', '8091'))
TEP_API       = 'http://127.0.0.1:47778/'
LOG           = logging.getLogger('hb-files')

# v2.1.3 replication hook. Disabled by default during rollout.
REPL_HOOK_ENABLED = os.environ.get('HB_REPL_HOOK_ENABLED', '0') == '1'
REPL_CONTROLLER = os.environ.get('HB_REPL_CONTROLLER', 'http://127.0.0.1:8095').rstrip('/')
REPL_HOOK_TOKEN = os.environ.get('HB_REPL_HOOK_TOKEN', '')
REPL_NODE_ID = os.environ.get('NODE_ID', '').strip()

def _register_replication(cid: str, size_bytes: int, reference_id: str) -> dict:
    """Register a successful local IPFS upload with the replication controller.

    This hook is intentionally fail-open for the upload itself: a locally pinned
    file remains usable even if the controller is unavailable. The response
    explicitly reports registration failure, so HB-Files never claims durable
    replication that was not registered.
    """
    if not REPL_HOOK_ENABLED:
        return {'enabled': False, 'registered': False, 'state': 'disabled'}
    if not REPL_HOOK_TOKEN or not REPL_NODE_ID:
        LOG.error('Replication hook enabled but token/NODE_ID is missing')
        return {'enabled': True, 'registered': False, 'state': 'misconfigured'}
    try:
        import hb_replication_client
        client = hb_replication_client.ReplicationClient(REPL_CONTROLLER, REPL_HOOK_TOKEN)
        result = client.register(
            cid, int(size_bytes), REPL_NODE_ID, reference_id=reference_id
        )
        return {
            'enabled': True,
            'registered': True,
            'state': str(result.get('state') or 'pending'),
            'target_replicas': result.get('target_replicas'),
            'required_committable': result.get('required_committable'),
            'confirmed_total': result.get('confirmed_total'),
            'confirmed_committable': result.get('confirmed_committable'),
        }
    except Exception as e:
        LOG.error('Replication registration failed for CID %s: %s', cid, e)
        return {'enabled': True, 'registered': False, 'state': 'registration-failed'}


# ── Data models ────────────────────────────────────────────────────────────────

@dataclass
class Tenant:
    id:         str
    name:       str
    token:      str
    quota_gb:   float = 100.0
    created_at: int   = field(default_factory=lambda: int(time.time()))
    enabled:    bool  = True
    domain:     str   = ''   # optional: files.example.com

@dataclass
class FileRecord:
    id:          str
    tenant_id:   str
    filename:    str
    mime_type:   str
    size_bytes:  int
    sha256:      str
    path:        str          # legacy disco; con IPFS resta '' 
    uploaded_at: int          = field(default_factory=lambda: int(time.time()))
    tags:        list         = field(default_factory=list)
    cid:         str          = ''     # PATCH-STRATO-B1: CID IPFS (backend unico)
    encrypted:   bool         = False  # blob cifrato lato client?

@dataclass
class ShareLink:
    token:          str
    file_id:        str
    tenant_id:      str
    created_at:     int = field(default_factory=lambda: int(time.time()))
    expires_at:     int = field(default_factory=lambda: int(time.time()) + SHARE_TTL_SEC)
    download_count: int = 0
    tep_address:    str = ''  # tep://pubkey/token
    http_address:   str = ''  # http://ip/files/token
    domain_address: str = ''  # https://domain/files/token
    max_downloads:  int = 0   # 0 = unlimited


# ── Storage backend ────────────────────────────────────────────────────────────

class Storage:

    def __init__(self):
        for d in [STORAGE_BASE, METADATA_DIR,
                  METADATA_DIR / 'tenants',
                  METADATA_DIR / 'files',
                  METADATA_DIR / 'shares',
                  METADATA_DIR / 'keystores']:
            d.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._bootstrap_default_tenant()

    def _bootstrap_default_tenant(self):
        if not list((METADATA_DIR / 'tenants').glob('*.json')):
            t = Tenant(id='default', name='Default Tenant',
                       token=secrets.token_hex(24), quota_gb=1000.0)
            self._write('tenants', t.id, t)
            (STORAGE_BASE / t.id).mkdir(exist_ok=True)
            LOG.info("Default tenant created — token: %s", t.token)
            print(f"\n[HB-Files] Default tenant API token: {t.token}")
            print(f"           Save to /etc/hashburst/env as HB_FILES_DEFAULT_TOKEN\n")

    def _write(self, kind: str, key: str, obj):
        p = METADATA_DIR / kind / f'{key}.json'
        with self._lock:
            p.write_text(json.dumps(asdict(obj), indent=2))

    def _read(self, kind: str, key: str, cls):
        p = METADATA_DIR / kind / f'{key}.json'
        if p.exists():
            try:
                return cls(**json.loads(p.read_text()))
            except Exception:
                return None
        return None

    def _list(self, kind: str, cls) -> list:
        result = []
        for p in (METADATA_DIR / kind).glob('*.json'):
            try:
                result.append(cls(**json.loads(p.read_text())))
            except Exception:
                pass
        return result

    # Tenants
    def get_tenant_by_token(self, token: str) -> Optional[Tenant]:
        for t in self._list('tenants', Tenant):
            if t.token == token and t.enabled:
                return t
        return None

    def get_tenant(self, tid: str) -> Optional[Tenant]:
        return self._read('tenants', tid, Tenant)

    def list_tenants(self) -> list:
        result = []
        for t in self._list('tenants', Tenant):
            d = asdict(t)
            d['token'] = d['token'][:8] + '...'
            d['used_gb'] = round(self.tenant_usage_bytes(t.id) / 1024**3, 4)
            result.append(d)
        return result

    def create_tenant(self, name: str, quota_gb: float = 100.0,
                      domain: str = '') -> Tenant:
        tid = 'tenant_' + secrets.token_hex(6)
        t = Tenant(id=tid, name=name, token=secrets.token_hex(24),
                   quota_gb=quota_gb, domain=domain)
        self._write('tenants', tid, t)
        (STORAGE_BASE / tid).mkdir(exist_ok=True)
        LOG.info("Tenant created: %s (%s)  quota=%.0f GB  domain=%s",
                 tid, name, quota_gb, domain or 'none')
        return t

    # Files
    def get_file(self, fid: str) -> Optional[FileRecord]:
        return self._read('files', fid, FileRecord)

    def list_files(self, tenant_id: str) -> list:
        files = [asdict(f) for f in self._list('files', FileRecord)
                 if f.tenant_id == tenant_id]
        return sorted(files, key=lambda x: x['uploaded_at'], reverse=True)

    def save_file(self, f: FileRecord):
        self._write('files', f.id, f)

    def delete_file(self, fid: str, tenant_id: str) -> bool:
        f = self.get_file(fid)
        if not f or f.tenant_id != tenant_id:
            return False
        # PATCH-STRATO-B1: unpin da IPFS (best effort). Nota: su IPFS il blob
        # resta finche' il GC non gira; unpin lo rende idoneo alla rimozione.
        if getattr(f, 'cid', ''):
            # A CID may be referenced by multiple logical records because IPFS is
            # content-addressed. Unpin only when this is the final reference.
            refs = sum(1 for other in self._list('files', FileRecord)
                       if other.id != fid and getattr(other, 'cid', '') == f.cid)
            if refs == 0:
                try:
                    _ipfs.unpin(f.cid)
                except Exception:
                    pass
        fp = STORAGE_BASE / f.path if f.path else None
        if fp and fp.exists():
            fp.unlink()
        (METADATA_DIR / 'files' / f'{fid}.json').unlink(missing_ok=True)
        return True

    def tenant_usage_bytes(self, tenant_id: str) -> int:
        return sum(f.size_bytes for f in self._list('files', FileRecord)
                   if f.tenant_id == tenant_id)

    # Shares
    def get_share(self, token: str) -> Optional[ShareLink]:
        return self._read('shares', token, ShareLink)

    def create_share(self, file_id: str, tenant_id: str,
                     tep_pubkey: str, server_ip: str,
                     tenant_domain: str = '',
                     ttl_sec: int = SHARE_TTL_SEC,
                     max_downloads: int = 0) -> ShareLink:
        token = secrets.token_urlsafe(24)
        s = ShareLink(
            token=token,
            file_id=file_id,
            tenant_id=tenant_id,
            expires_at=int(time.time()) + ttl_sec,
            tep_address=f'tep://{tep_pubkey}/{token}',
            http_address=f'http://{server_ip}/files/{token}',
            domain_address=(f'https://{tenant_domain}/files/{token}'
                            if tenant_domain else ''),
            max_downloads=max_downloads,
        )
        self._write('shares', token, s)
        return s

    def increment_download(self, token: str):
        s = self.get_share(token)
        if s:
            s.download_count += 1
            self._write('shares', token, s)

    # PATCH-STRATO-B2: keystore per-utente (blob opaco, il server non lo apre)
    def get_keystore(self, apikey: str):
        p = METADATA_DIR / 'keystores' / f'{apikey}.json'
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                return None
        return None

    def save_keystore(self, apikey: str, keystore: dict):
        d = METADATA_DIR / 'keystores'
        d.mkdir(parents=True, exist_ok=True)
        with self._lock:
            (d / f'{apikey}.json').write_text(json.dumps(keystore))

    def storage_stats(self) -> dict:
        total, used, free = shutil.disk_usage(STORAGE_BASE)
        return {
            'total_gb':     round(total / 1024**3, 2),
            'used_gb':      round(used  / 1024**3, 2),
            'free_gb':      round(free  / 1024**3, 2),
            'files':        sum(1 for _ in (METADATA_DIR/'files').glob('*.json')),
            'tenants':      sum(1 for _ in (METADATA_DIR/'tenants').glob('*.json')),
            'shares':       sum(1 for _ in (METADATA_DIR/'shares').glob('*.json')),
            'storage_path': str(STORAGE_BASE),
        }


# ── TEP / network helpers ──────────────────────────────────────────────────────

def get_tep_pubkey() -> str:
    try:
        import urllib.request
        with urllib.request.urlopen(TEP_API, timeout=3) as r:
            return json.loads(r.read()).get('pubkey', '')
    except Exception:
        return os.environ.get('TEP_PUBKEY', '')

def get_server_ip() -> str:
    return os.environ.get('EXTERNAL_IP', '85.233.199.35')


# ── HTTP request handler ───────────────────────────────────────────────────────

_storage = Storage()


class HBFilesHandler(BaseHTTPRequestHandler):
    server_version = 'HB-Files/1.0'
    sys_version    = ''

    def log_message(self, *a):
        pass

    def send_json(self, data, code=200):
        body = json.dumps(data, indent=2, default=str).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def send_err(self, msg, code=400):
        self.send_json({'error': msg}, code)

    def auth_tenant(self) -> Optional[Tenant]:
        # PATCH-STRATO-A: identita' = apikey+signature verificati su list.json.
        # Il Tenant e' costruito al volo dall'apikey (id = apikey), non piu'
        # letto da un file di tenant. La cartella storage e' STORAGE_BASE/<apikey>.
        apikey = self.headers.get(APIKEY_HEADER, '').strip()
        signature = self.headers.get(SIGNATURE_HEADER, '').strip()
        if not apikey or not signature:
            return None
        reg = hb_registry.get_registry()
        if not reg.verify(apikey, signature):
            return None
        akey = apikey.lower()
        # crea la cartella storage del tenant se non esiste
        (STORAGE_BASE / akey).mkdir(parents=True, exist_ok=True)
        return Tenant(
            id=akey,
            name=f'stakeholder:{akey[:8]}',
            token='',                       # non piu' usato per auth
            quota_gb=reg.quota_gb(apikey),  # quota sovrana
            enabled=True,
        )

    def auth_admin(self) -> bool:
        return self.headers.get(TOKEN_HEADER, '') == ADMIN_SECRET

    def read_json_body(self) -> dict:
        n = int(self.headers.get('Content-Length', 0))
        if n > 0:
            try:
                return json.loads(self.rfile.read(n))
            except Exception:
                pass
        return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods',
                         'GET,POST,DELETE,OPTIONS')
        self.send_header('Access-Control-Allow-Headers',
                         f'Content-Type,{TOKEN_HEADER}')
        self.end_headers()

    # ── GET ────────────────────────────────────────────────────────────────────
    def do_GET(self):
        path = urlparse(self.path).path.rstrip('/')

        if path == '/health':
            pk = get_tep_pubkey()
            self.send_json({
                'status':    'ok',
                'version':   '1.0.0',
                'tep_pubkey': pk,
                'tep_node':  f'tep://{pk}' if pk else '',
                'server_ip': get_server_ip(),
                'storage':   _storage.storage_stats(),
                'ipfs_private': _ipfs.is_alive(),
                'capacity': (_accountant.commerciable() if _accountant else None),
            })
            return

        if path.startswith('/files/'):
            self._serve_download(path[len('/files/'):])
            return

        if path == '/api/files':
            t = self.auth_tenant()
            if not t:
                self.send_err('Unauthorized', 401); return
            self.send_json(_storage.list_files(t.id))
            return

        if path.startswith('/api/files/') and len(path.split('/')) == 4:
            t = self.auth_tenant()
            if not t:
                self.send_err('Unauthorized', 401); return
            f = _storage.get_file(path.split('/')[-1])
            if not f or f.tenant_id != t.id:
                self.send_err('Not found', 404); return
            self.send_json(asdict(f))
            return

        if path.startswith('/api/share/') and len(path.split('/')) == 4:
            s = _storage.get_share(path.split('/')[-1])
            if not s:
                self.send_err('Not found', 404); return
            self.send_json(asdict(s))
            return

        if path == '/api/keystore':
            t = self.auth_tenant()
            if not t:
                self.send_err('Unauthorized', 401); return
            ks = _storage.get_keystore(t.id)
            if ks is None:
                self.send_err('No keystore', 404); return
            self.send_json(ks)
            return

        if path == '/api/storage':
            if not self.auth_admin():
                self.send_err('Unauthorized', 401); return
            self.send_json(_storage.storage_stats())
            return

        if path == '/api/public/storage-summary':
            # PATCH-SUMMARY-ROLE applicato
            # PATCH-PUBLIC-SUMMARY: aggregati pubblici NON sensibili.
            # Nessuna apikey, nessun CID, nessuna quota per-stakeholder.
            if _accountant is None:
                self.send_json({'available': False}); return
            try:
                # PATCH-SUMMARY-ROLE: distingue primario (contabilita' sovrana
                # di rete) da secondario (solo capacita' fisica + uso).
                phys = _accountant.physical()
                role = os.environ.get('HB_STORAGE_ROLE',
                                      'primary' if phys.get('zfs_available') else 'secondary')
                summary = {
                    'available': True,
                    'role': role,
                    'capacity_class': 'best-effort' if role == 'edge' else 'committable',
                    'node_id': os.environ.get('NODE_ID', 'hb-node'),
                    'ipfs_private_peers': _ipfs_peer_count(),
                    'capacity_total_gb': phys.get('physical_total_gb'),
                    'capacity_total_tb': phys.get('physical_total_tb'),
                    'used_gb': phys.get('used_gb'),
                    'capacity_source': phys.get('source'),
                    'timestamp': int(time.time()),
                }
                if role == 'primary':
                    # solo il primario espone la contabilita' sovrana (di rete)
                    sov  = _accountant.sovereign()
                    comm = _accountant.commerciable()
                    summary.update({
                        'reserved_stakeholders_gb': sov.get('sovereign_assigned_gb'),
                        'sold_active_gb': sov.get('external_granted_gb'),
                        'free_sellable_gb': comm.get('surplus_gb') if comm.get('available') else None,
                        'stakeholders': sov.get('stakeholders'),
                    })
                self.send_json(summary)
            except Exception as e:
                self.send_json({'available': False, 'error': 'summary error'})
            return

        if path == '/api/capacity':
            if not self.auth_admin():
                self.send_err('Unauthorized', 401); return
            if _accountant is None:
                self.send_err('Accountant not ready', 503); return
            self.send_json(_accountant.report())
            return

        if path == '/api/grants':
            if not self.auth_admin():
                self.send_err('Unauthorized', 401); return
            self.send_json(_accountant.list_grants() if _accountant else {})
            return

        if path == '/api/tenants':
            if not self.auth_admin():
                self.send_err('Unauthorized', 401); return
            self.send_json(_storage.list_tenants())
            return

        self.send_err('Not found', 404)

    # ── POST ───────────────────────────────────────────────────────────────────
    def do_POST(self):
        path = urlparse(self.path).path.rstrip('/')

        if path == '/api/upload':
            t = self.auth_tenant()
            if not t:
                self.send_err('Unauthorized', 401); return
            self._handle_upload(t)
            return

        if path.startswith('/api/share/') and len(path.split('/')) == 4:
            t = self.auth_tenant()
            if not t:
                self.send_err('Unauthorized', 401); return
            self._handle_create_share(t, path.split('/')[-1])
            return

        if path == '/api/keystore':
            t = self.auth_tenant()
            if not t:
                self.send_err('Unauthorized', 401); return
            ks = self.read_json_body()
            if not ks or 'pw' not in ks or 'rk' not in ks:
                self.send_err('Invalid keystore', 400); return
            _storage.save_keystore(t.id, ks)
            self.send_json({'saved': True}, 201)
            return

        if path == '/api/grant':
            if not self.auth_admin():
                self.send_err('Unauthorized', 401); return
            if _accountant is None:
                self.send_err('Accountant not ready', 503); return
            body = self.read_json_body()
            apikey = body.get('apikey', '')
            gb = body.get('gb', 0)
            ref = body.get('payment_ref', '')
            if not apikey or not gb:
                self.send_err('apikey e gb richiesti', 400); return
            res = _accountant.grant_quota(apikey, gb, ref)
            self.send_json(res, 200 if res.get('ok') else 400)
            return

        if path == '/api/revoke':
            if not self.auth_admin():
                self.send_err('Unauthorized', 401); return
            body = self.read_json_body()
            apikey = body.get('apikey', '')
            res = _accountant.revoke_quota(apikey) if _accountant else {'ok': False}
            self.send_json(res, 200 if res.get('ok') else 404)
            return

        if path == '/api/tenants':
            if not self.auth_admin():
                self.send_err('Unauthorized', 401); return
            self._handle_create_tenant()
            return

        self.send_err('Not found', 404)

    # ── DELETE ─────────────────────────────────────────────────────────────────
    def do_DELETE(self):
        path = urlparse(self.path).path.rstrip('/')
        if path.startswith('/api/files/'):
            t = self.auth_tenant()
            if not t:
                self.send_err('Unauthorized', 401); return
            ok = _storage.delete_file(path.split('/')[-1], t.id)
            self.send_json({'deleted': ok}, 200 if ok else 404)
            return
        self.send_err('Not found', 404)

    # ── Upload handler ─────────────────────────────────────────────────────────
    def _handle_upload(self, tenant: Tenant):
        ct = self.headers.get('Content-Type', '')
        if 'multipart/form-data' not in ct:
            self.send_err('Expected multipart/form-data'); return

        used = _storage.tenant_usage_bytes(tenant.id)
        cl = int(self.headers.get('Content-Length', 0))
        if cl > MAX_UPLOAD_MB * 1024**2:
            self.send_err(f'File too large (max {MAX_UPLOAD_MB} MB)', 413); return

        body = self.rfile.read(cl)

        # Extract multipart boundary
        boundary = None
        for part in ct.split(';'):
            p = part.strip()
            if p.startswith('boundary='):
                boundary = p[9:].strip('"')
        if not boundary:
            self.send_err('No boundary in Content-Type'); return

        # Minimal multipart parser
        sep = ('--' + boundary).encode()
        filename, file_data = None, None
        for chunk in body.split(sep):
            if b'Content-Disposition' not in chunk:
                continue
            hdr_block, _, content = chunk.partition(b'\r\n\r\n')
            # Preserve ciphertext byte-for-byte. split(sep) already removed the boundary;
            # remove only the multipart framing CRLF, never arbitrary CR/LF/'-' bytes.
            if content.startswith(b'\r\n'):
                content = content[2:]
            if content.endswith(b'\r\n'):
                content = content[:-2]
            if b'filename=' in hdr_block:
                for line in hdr_block.split(b'\r\n'):
                    if b'filename=' in line:
                        raw = line.decode('utf-8', 'replace')
                        filename = raw.split('filename=')[-1].strip().strip('"')
                file_data = content

        if not filename or file_data is None:
            self.send_err('No file in multipart body'); return

        # Enforce quota against the incoming payload, not only current usage.
        used = _storage.tenant_usage_bytes(tenant.id)
        quota_bytes = int(tenant.quota_gb * 1024**3)
        if used + len(file_data) > quota_bytes:
            self.send_err('Storage quota exceeded', 413); return

        filename = Path(filename).name  # strip any path components
        file_id  = str(uuid.uuid4())
        sha256   = hashlib.sha256(file_data).hexdigest()
        mime     = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
        # PATCH-STRATO-B1: pinna su IPFS privato (no disco). file_data e' gia'
        # cifrato lato client: byte opachi. Il client segnala X-HB-Encrypted.
        try:
            cid = _ipfs.add(file_data, filename=file_id)
        except hb_ipfs.IPFSError as e:
            self.send_err(f'IPFS unavailable: {e}', 502); return
        is_enc = self.headers.get('X-HB-Encrypted', '').lower() in ('1','true','yes')
        rec = FileRecord(
            id=file_id, tenant_id=tenant.id, filename=filename,
            mime_type=mime, size_bytes=len(file_data),
            sha256=sha256, path='', cid=cid, encrypted=is_enc,
        )
        _storage.save_file(rec)
        replication = _register_replication(cid, len(file_data), file_id)
        LOG.info("Upload: %s  %d bytes  tenant=%s replication=%s",
                 filename, len(file_data), tenant.id, replication.get('state'))
        self.send_json({
            'id':         file_id,
            'filename':   filename,
            'size_bytes': len(file_data),
            'sha256':     sha256,
            'mime_type':  mime,
            'cid':        cid,
            'encrypted':  is_enc,
            'replication': replication,
        }, 201)

    # ── Share link creation ────────────────────────────────────────────────────
    def _handle_create_share(self, tenant: Tenant, file_id: str):
        f = _storage.get_file(file_id)
        if not f or f.tenant_id != tenant.id:
            self.send_err('File not found', 404); return
        opts = self.read_json_body()
        ttl  = int(opts.get('ttl_sec', SHARE_TTL_SEC))
        maxd = int(opts.get('max_downloads', 0))
        pk   = get_tep_pubkey()
        ip   = get_server_ip()
        s = _storage.create_share(
            file_id, tenant.id, pk, ip, tenant.domain, ttl, maxd)
        LOG.info("Share: %s -> tep://%s/...  tenant=%s",
                 f.filename, pk[:16], tenant.id)
        self.send_json({
            'token':          s.token,
            'tep_address':    s.tep_address,
            'http_address':   s.http_address,
            'domain_address': s.domain_address,
            'expires_at':     s.expires_at,
            'filename':       f.filename,
            'size_bytes':     f.size_bytes,
            'mime_type':      f.mime_type,
        }, 201)

    # ── Download handler ───────────────────────────────────────────────────────
    def _serve_download(self, token: str):
        s = _storage.get_share(token)
        if not s:
            self.send_err('Share link not found', 404); return
        if s.expires_at < int(time.time()):
            self.send_err('Share link expired', 410); return
        if s.max_downloads > 0 and s.download_count >= s.max_downloads:
            self.send_err('Download limit reached', 410); return

        f = _storage.get_file(s.file_id)
        if not f:
            self.send_err('File not found', 404); return
        # PATCH-STRATO-B1: recupera da IPFS via CID (backend unico)
        try:
            data = _ipfs.cat(f.cid)
        except hb_ipfs.IPFSError as e:
            self.send_err(f'IPFS retrieval failed: {e}', 502); return

        _storage.increment_download(token)

        self.send_response(200)
        self.send_header('Content-Type',        f.mime_type)
        self.send_header('Content-Length',      str(len(data)))
        self.send_header('Content-Disposition',
                         f'attachment; filename="{f.filename}"')
        self.send_header('X-HB-TEP-Address',   s.tep_address)
        self.send_header('X-HB-Share-Token',    token)
        self.send_header('X-HB-Downloads',
                         f'{s.download_count}/{s.max_downloads or "unlimited"}')
        self.end_headers()
        self.wfile.write(data)

    # ── Tenant creation ────────────────────────────────────────────────────────
    def _handle_create_tenant(self):
        opts  = self.read_json_body()
        name  = opts.get('name', 'New Tenant')
        quota = float(opts.get('quota_gb', 100.0))
        domain = opts.get('domain', '')
        t = _storage.create_tenant(name, quota, domain)
        self.send_json({
            'id':       t.id,
            'name':     t.name,
            'token':    t.token,
            'quota_gb': t.quota_gb,
            'domain':   t.domain,
        }, 201)


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    global _accountant  # PATCH-GRADINO-3
    ap = argparse.ArgumentParser(
        description='HB-Files — HashBurst Private Cloud Storage')
    ap.add_argument('--port',      type=int, default=PORT)
    ap.add_argument('--log-level', default='INFO',
                    choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])
    args = ap.parse_args()

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(),
        ]
    )
    LOG.info("HB-Files v1.0 starting — port=%d  storage=%s",
             args.port, STORAGE_BASE)
    LOG.info("TEP pubkey: %s", get_tep_pubkey() or '(not yet available)')
    LOG.info("Server IP:  %s", get_server_ip())
    # PATCH-GRADINO-3: crea l'accountant con il registry condiviso
    try:
        _reg = hb_registry.get_registry()
        _accountant = hb_capacity.CapacityAccountant(_reg)
        LOG.info("Capacity accountant pronto (dataset %s)", hb_capacity.ZFS_DATASET)
    except Exception as e:
        LOG.warning("Accountant non inizializzato: %s", e)
    if not ADMIN_SECRET or ADMIN_SECRET == 'CHANGE_ME':
        raise SystemExit('HB_ADMIN_SECRET missing/refuses default secret')
    HTTPServer((BIND_ADDRESS, args.port), HBFilesHandler).serve_forever()


if __name__ == '__main__':
    main()
