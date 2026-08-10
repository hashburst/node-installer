#!/usr/bin/env python3
"""
HB-Files — Contabilita' di capacita' (Gradino 3)
================================================
Calcola i tre livelli del modello economico sovrano:

  1. CAPACITA' FISICA    : quota ZFS del dataset datapool/hashburst (5 TB)
  2. QUOTA SOVRANA       : somma delle quote garantite a tutti gli stakeholder
                           (ogni apikey in list.json) -> da hb_registry
  3. ECCEDENZA VENDIBILE : fisica - sovrana_assegnata - overhead
                           lo spazio libero commerciabile a esterni in HBT

Legge numeri REALI:
  - capacita' fisica: da ZFS (`zfs get -Hp quota,used,available <dataset>`)
  - usato reale:      da ZFS `used` del dataset
  - quote sovrane:    da hb_registry.sum_assigned_quota_gb()

GANCIO ECONOMICO (grant_quota): assegna quota a un cliente. Oggi chiamabile
da admin manualmente; domani attivato dal layer HBT al pagamento. NON scrive
codice di pagamento che non puo' girare: registra solo l'assegnazione + un
riferimento al pagamento (payment_ref) per l'audit futuro.
"""

from __future__ import annotations
import os
import json
import subprocess
import time
from pathlib import Path
from typing import Optional

# dataset ZFS di riferimento per la capacita' HashBurst
ZFS_DATASET = os.environ.get("HB_ZFS_DATASET", "datapool/hashburst")
GRANTS_FILE = Path("/var/lib/hashburst/files-meta/quota_grants.json")

GB = 1024 ** 3
TB = 1024 ** 4


def _zfs_get(prop: str, dataset: str = ZFS_DATASET) -> Optional[int]:
    """Legge una proprieta' ZFS in byte (-p = parsable/byte esatti).
    Ritorna int byte, o None se ZFS non disponibile."""
    try:
        out = subprocess.run(
            ["zfs", "get", "-Hp", "-o", "value", prop, dataset],
            capture_output=True, text=True, timeout=10
        )
        if out.returncode != 0:
            return None
        val = out.stdout.strip()
        if val in ("none", "-", ""):
            return None
        return int(val)
    except (subprocess.SubprocessError, ValueError, FileNotFoundError):
        return None


class CapacityAccountant:
    """Contabilita' di capacita' del nodo storage."""

    def __init__(self, registry, default_quota_gb: float = 100.0):
        # registry = istanza hb_registry.Registry (per le quote sovrane)
        self.registry = registry
        self.default_quota_gb = default_quota_gb

    # ---- capacita' fisica (da ZFS) -----------------------------------------
    def physical(self) -> dict:
        quota = _zfs_get("quota")          # tetto dedicato ZFS (es. 5 TB sul nodo 0)
        used = _zfs_get("used")            # usato reale dal dataset ZFS
        avail = _zfs_get("available")      # disponibile ZFS

        backend = os.environ.get("HB_STORAGE_BACKEND", "auto").lower()
        if backend == "zfs":
            if quota is None:
                return {"quota_bytes": None, "used_bytes": None, "available_bytes": None,
                        "physical_total_bytes": None, "physical_total_gb": None,
                        "physical_total_tb": None, "used_gb": None,
                        "zfs_available": False, "source": "zfs-unavailable"}
            source = "zfs"
            physical_bytes = quota
            used_bytes = used
        else:
            limit_gb = os.environ.get("HB_CAPACITY_LIMIT_GB")
            if limit_gb:
                physical_bytes = int(float(limit_gb) * GB)
                source = "logical"
            else:
                physical_bytes = self._disk_total()
                source = "disk"
            used_bytes = self._disk_used()

        return {
            "quota_bytes": quota,
            "used_bytes": used_bytes,
            "available_bytes": avail,
            "physical_total_bytes": physical_bytes,
            "physical_total_gb": round(physical_bytes / GB, 2) if physical_bytes else None,
            "physical_total_tb": round(physical_bytes / TB, 3) if physical_bytes else None,
            "used_gb": round(used_bytes / GB, 2) if used_bytes is not None else None,
            "zfs_available": backend == "zfs" and quota is not None,
            "source": source,   # "zfs" | "logical" | "disk"
        }

    def _storage_path(self) -> str:
        return os.environ.get("STORAGE_DIR", "/var/lib/hashburst")

    def _disk_total(self):
        try:
            import shutil as _sh
            return _sh.disk_usage(self._storage_path()).total
        except Exception:
            return None

    def _disk_used(self):
        # spazio realmente occupato dalla cartella di storage (non tutto il disco)
        try:
            import subprocess
            out = subprocess.run(["du", "-sb", self._storage_path()],
                                 capture_output=True, text=True, timeout=30)
            if out.returncode == 0:
                return int(out.stdout.split()[0])
        except Exception:
            pass
        return None

    # ---- quote sovrane assegnate agli stakeholder --------------------------
    def sovereign(self) -> dict:
        assigned_gb = self.registry.sum_assigned_quota_gb()
        granted_gb = self._sum_grants_gb()  # quote vendute a clienti esterni
        stakeholders = self.registry.count()
        return {
            "stakeholders": stakeholders,
            "sovereign_assigned_gb": round(assigned_gb, 2),
            "external_granted_gb": round(granted_gb, 2),
            "total_committed_gb": round(assigned_gb + granted_gb, 2),
        }

    # ---- eccedenza commerciabile -------------------------------------------
    def commerciable(self) -> dict:
        phys = self.physical()
        sov = self.sovereign()
        total = phys["physical_total_gb"]
        if total is None:
            return {"available": False, "reason": "capacita' fisica non leggibile"}
        committed = sov["total_committed_gb"]
        surplus = total - committed
        return {
            "available": True,
            "physical_total_gb": total,
            "committed_gb": committed,
            "surplus_gb": round(surplus, 2),
            "surplus_tb": round(surplus / 1024, 3),
            "utilization_pct": round(committed / total * 100, 1) if total else 0,
            "oversubscribed": surplus < 0,  # committed > fisico: attenzione!
        }

    # ---- report completo ----------------------------------------------------
    def report(self) -> dict:
        return {
            "timestamp": int(time.time()),
            "dataset": ZFS_DATASET,
            "physical": self.physical(),
            "sovereign": self.sovereign(),
            "commerciable": self.commerciable(),
        }

    # ---- GANCIO economico: assegna quota a un cliente esterno --------------
    def grant_quota(self, apikey: str, gb: float, payment_ref: str = "") -> dict:
        """Assegna `gb` di quota a `apikey`. GANCIO per la vendita in HBT.

        Oggi: chiamabile da admin (assegnazione manuale).
        Domani: invocato dal layer HBT quando un pagamento e' confermato.
        payment_ref: riferimento al pagamento (txid HBT futuro, o nota admin).

        NON esegue pagamenti: registra solo l'assegnazione per l'audit e per
        il calcolo dell'eccedenza. Verifica che non si sfori la capacita' fisica."""
        gb = float(gb)
        if gb <= 0:
            return {"ok": False, "error": "gb deve essere positivo"}

        # controllo capacita': non vendere piu' del fisico disponibile
        comm = self.commerciable()
        if comm.get("available") and gb > comm["surplus_gb"]:
            return {"ok": False, "error": "eccedenza insufficiente",
                    "requested_gb": gb, "surplus_gb": comm["surplus_gb"]}

        grants = self._load_grants()
        grants[apikey] = {
            "gb": gb,
            "payment_ref": payment_ref,
            "granted_at": int(time.time()),
        }
        self._save_grants(grants)
        return {"ok": True, "apikey": apikey, "gb": gb, "payment_ref": payment_ref}

    def revoke_quota(self, apikey: str) -> dict:
        grants = self._load_grants()
        if apikey in grants:
            del grants[apikey]
            self._save_grants(grants)
            return {"ok": True, "revoked": apikey}
        return {"ok": False, "error": "nessuna concessione per questa apikey"}

    def list_grants(self) -> dict:
        return self._load_grants()

    # ---- persistenza delle concessioni -------------------------------------
    def _load_grants(self) -> dict:
        if GRANTS_FILE.exists():
            try:
                return json.loads(GRANTS_FILE.read_text())
            except Exception:
                return {}
        return {}

    def _save_grants(self, grants: dict):
        GRANTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        GRANTS_FILE.write_text(json.dumps(grants, indent=2))

    def _sum_grants_gb(self) -> float:
        return sum(float(g.get("gb", 0)) for g in self._load_grants().values())
