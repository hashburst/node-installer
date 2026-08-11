#!/usr/bin/env python3
"""
HB-Files — IPFS backend (Strato B1)
===================================
Pinning e retrieval di blob su un daemon IPFS locale via HTTP RPC API (/api/v0).

MODELLO (opzione A: IPFS backend unico)
  - I file NON stanno piu' su disco: vivono su IPFS, il FileRecord tiene solo il CID.
  - I blob arrivano GIA' cifrati dal client (opzione 2: cifratura lato browser).
    HB-Files li tratta come byte opachi: li pinna e li restituisce, senza mai
    vederne il contenuto in chiaro.

DUE PIANI IPFS
  - PRIVATO  (cloud sovrano):   API 127.0.0.1:5011  -> storage stakeholder
  - PUBBLICO (repository):      API 127.0.0.1:5001  -> repository (Strato D)
  Il piano si sceglie con l'URL API passato al costruttore.

API Kubo usate (tutte POST su /api/v0/):
  - /api/v0/add?pin=true&cid-version=1   -> aggiunge+pinna, risponde {"Hash": CID}
  - /api/v0/cat?arg=<CID>                -> restituisce i byte del blob
  - /api/v0/pin/rm?arg=<CID>             -> rimuove il pin (delete)
  - /api/v0/repo/stat                    -> statistiche repo (capacita')
  - /api/v0/id                           -> identita' nodo (health)
"""

from __future__ import annotations
import json
import urllib.request
import urllib.error
import urllib.parse
import uuid


class IPFSError(Exception):
    pass


class IPFSClient:
    """Client minimale per il daemon IPFS locale via HTTP RPC.

    Non usa dipendenze esterne (solo urllib): coerente con hb_files.py che e'
    gia' urllib-only."""

    def __init__(self, api_url: str = "http://127.0.0.1:5011", timeout: int = 120):
        # api_url e' la base del daemon (privato :5011 o pubblico :5001)
        self.api = api_url.rstrip("/")
        self.timeout = timeout

    # ---- helper HTTP --------------------------------------------------------
    def _post(self, path: str, data: bytes = None, headers: dict = None) -> bytes:
        url = f"{self.api}/api/v0/{path.lstrip('/')}"
        req = urllib.request.Request(url, data=data or b"", method="POST")
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:200]
            raise IPFSError(f"IPFS {path} HTTP {e.code}: {body}")
        except urllib.error.URLError as e:
            raise IPFSError(f"IPFS {path} irraggiungibile: {e.reason}")

    # ---- add: pinna un blob, ritorna il CID --------------------------------
    def add(self, blob: bytes, filename: str = "blob") -> str:
        """Aggiunge e pinna un blob. Ritorna il CID (v1).

        Il blob e' gia' cifrato: qui e' solo una sequenza di byte opachi."""
        boundary = "----hbfiles" + uuid.uuid4().hex
        body = self._multipart(blob, filename, boundary)
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        # pin=true: resta pinnato (non garbage-collected)
        # cid-version=1: CID moderni (base32, piu' robusti)
        raw = self._post("add?pin=true&cid-version=1&quieter=true", body, headers)
        # la risposta puo' contenere piu' righe JSON: prendo l'ultima valida
        cid = None
        for line in raw.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("Hash"):
                cid = obj["Hash"]
        if not cid:
            raise IPFSError(f"add: nessun CID nella risposta: {raw[:200]!r}")
        return cid

    def _multipart(self, blob: bytes, filename: str, boundary: str) -> bytes:
        pre = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        post = f"\r\n--{boundary}--\r\n".encode()
        return pre + blob + post

    # ---- cat: recupera un blob dato il CID ---------------------------------
    def cat(self, cid: str) -> bytes:
        """Restituisce i byte del blob pinnato. Blob cifrato: opaco."""
        if not cid:
            raise IPFSError("cat: CID vuoto")
        return self._post(f"cat?arg={cid}")

    # ---- replication primitives (v2.1.3 candidate) -------------------------
    def pin(self, cid: str) -> bool:
        """Recursively pin an existing CID via the local Kubo RPC.

        Kubo fetches any missing blocks from the private swarm. A successful
        request is not treated as durable replication until is_pinned() also
        confirms a recursive local pin.
        """
        if not cid:
            raise IPFSError("pin: CID vuoto")
        query = urllib.parse.urlencode({"arg": cid, "recursive": "true"})
        raw = self._post("pin/add?" + query)
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError as e:
            raise IPFSError("pin/add: risposta JSON non valida") from e
        pins = data.get("Pins") or []
        if pins and cid not in pins:
            raise IPFSError("pin/add: CID richiesto non confermato dalla risposta")
        return True

    def is_pinned(self, cid: str) -> bool:
        """Return True only when Kubo reports CID as a recursive local pin."""
        if not cid:
            return False
        query = urllib.parse.urlencode({"arg": cid, "type": "recursive"})
        try:
            raw = self._post("pin/ls?" + query)
        except IPFSError:
            return False
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return False
        keys = data.get("Keys") or {}
        entry = keys.get(cid)
        return bool(entry and str(entry.get("Type") or "").lower() == "recursive")

    # ---- pin rm: rimuove il pin (usato in delete) --------------------------
    def unpin(self, cid: str) -> bool:
        try:
            self._post(f"pin/rm?arg={cid}")
            return True
        except IPFSError:
            # gia' non pinnato o assente: non e' un errore fatale per la delete
            return False

    # ---- health -------------------------------------------------------------
    def node_id(self) -> dict:
        raw = self._post("id")
        return json.loads(raw.decode("utf-8", "replace"))

    def is_alive(self) -> bool:
        try:
            self.node_id()
            return True
        except IPFSError:
            return False

    # ---- repo stat: capacita' del datastore IPFS ---------------------------
    def repo_stat(self) -> dict:
        """RepoSize (byte usati), StorageMax, NumObjects. Base per la
        contabilita' di capacita' del piano privato."""
        raw = self._post("repo/stat?size-only=false")
        return json.loads(raw.decode("utf-8", "replace"))
