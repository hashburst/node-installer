#!/usr/bin/env python3
"""
HB-Files — Registry & Auth (Strato A)
=====================================
Autenticazione apikey + signature contro list.json (scelta B: file locale sul
nodo 0). Sostituisce il vecchio token-tenant singolo.

MODELLO
  L'utente e' identificato dalla sua apikey HashBurst (hex-40). La signature
  e' la credenziale segreta, verificata con compare_digest contro list.json.
  Le apikey sono pubbliche (compaiono come worker name sulle pool): NON bastano
  da sole. list.json e' la stessa struttura servita da download_list.php:
    [ { "apikey": "...", "signature": "...", "type": [...],
        "wallets": { "COIN": [addr...] } }, ... ]

FONTE list.json
  File locale su /etc/hashburst/list.json (nodo 0), aggiornato via:
    curl -s -X POST https://api.synapta.net/download_list.php \
      -H "Content-Type: application/json" -d '{"token":"<TOKEN>"}' \
      -o /etc/hashburst/list.json
  (l'IP del nodo 0 e' in whitelist per quella chiamata)

  Cache invalidata su mtime: non rilegge/riparsa 1862 entry ad ogni richiesta.
  Nessun contenuto di list.json finisce mai in output o nei log.

QUOTA SOVRANA
  La quota per-stakeholder puo' venire da list.json (campo "storage_quota_gb"
  se presente) o dal default DEFAULT_QUOTA_GB. E' il fondamento della
  contabilita' sovrana: ogni apikey ha una quota garantita, e la somma delle
  quote assegnate vs la capacita' fisica determina l'eccedenza commerciabile.
"""

from __future__ import annotations
import hmac
import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

LIST_JSON_PATH   = Path(os.environ.get("HB_LIST_JSON_PATH", "/etc/hashburst/list.json"))
DEFAULT_QUOTA_GB = float(os.environ.get("HB_FILES_DEFAULT_QUOTA_GB", "100"))

# Header usati dal client (coerenti con /wallets/me e /workers/search)
APIKEY_HEADER    = "X-Api-Key"
SIGNATURE_HEADER = "X-Signature"


class Registry:
    """Legge list.json con cache su mtime e autentica apikey+signature.

    Thread-safe. Non espone mai la signature verso l'esterno: la usa solo per
    il confronto a tempo costante."""

    def __init__(self, path: Path = LIST_JSON_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._stat_key = None
        self._by_apikey: dict[str, dict] = {}

    # ---- caricamento con cache su mtime ------------------------------------
    def _entries(self, raw):
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            for k in ("users", "list", "entries", "data"):
                v = raw.get(k)
                if isinstance(v, list):
                    return v
            return [v for v in raw.values() if isinstance(v, dict)]
        return []

    def _reload_if_changed(self):
        try:
            st = self.path.stat()
            stat_key = (st.st_mtime_ns, st.st_size)
        except OSError:
            # file assente: registro vuoto (nega tutto), ma non solleva
            with self._lock:
                self._by_apikey = {}
                self._stat_key = None
            return

        with self._lock:
            if self._stat_key == stat_key and self._by_apikey:
                return

        try:
            raw = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return  # tiene la cache precedente, non azzera su errore transitorio

        by_apikey = {}
        for e in self._entries(raw):
            if not isinstance(e, dict):
                continue
            apikey = e.get("apikey")
            if apikey:
                by_apikey[str(apikey).strip().lower()] = e

        with self._lock:
            self._by_apikey = by_apikey
            self._stat_key = stat_key

    # ---- API pubblica -------------------------------------------------------
    def get_entry(self, apikey: str) -> Optional[dict]:
        self._reload_if_changed()
        with self._lock:
            return self._by_apikey.get(str(apikey or "").strip().lower())

    def verify(self, apikey: str, signature: str) -> bool:
        """True se apikey esiste e la signature combacia (tempo costante)."""
        entry = self.get_entry(apikey)
        if not entry:
            return False
        expected = str(entry.get("signature", ""))
        if not expected or not signature:
            return False
        return hmac.compare_digest(expected, str(signature))

    def quota_gb(self, apikey: str) -> float:
        """Quota sovrana dello stakeholder. Da list.json se presente, altrimenti
        default. E' la base della contabilita' di capacita' (Strato successivo)."""
        entry = self.get_entry(apikey)
        if entry:
            q = entry.get("storage_quota_gb")
            if q is not None:
                try:
                    return float(q)
                except (TypeError, ValueError):
                    pass
        return DEFAULT_QUOTA_GB

    def count(self) -> int:
        self._reload_if_changed()
        with self._lock:
            return len(self._by_apikey)

    def sum_assigned_quota_gb(self) -> float:
        """Somma delle quote assegnate a TUTTI gli stakeholder registrati.
        Serve al calcolo dell'eccedenza commerciabile:
            eccedenza = capacita_fisica - quote_sovrane_assegnate
        (usato dallo storage_stats esteso nello Strato B/C)."""
        self._reload_if_changed()
        with self._lock:
            entries = list(self._by_apikey.values())
        total = 0.0
        for e in entries:
            q = e.get("storage_quota_gb")
            try:
                total += float(q) if q is not None else DEFAULT_QUOTA_GB
            except (TypeError, ValueError):
                total += DEFAULT_QUOTA_GB
        return total


# singleton condiviso
_registry: Optional[Registry] = None
_registry_lock = threading.Lock()


def get_registry() -> Registry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = Registry()
    return _registry
