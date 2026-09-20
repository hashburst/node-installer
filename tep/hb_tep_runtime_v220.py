#!/usr/bin/env python3
from __future__ import annotations

"""HashBurst v2.2 TEP runtime.

This runtime extends the field-validated v2.1.6 transport only with the
HashBurst HA application service. Project-specific workloads are intentionally
kept outside the transport runtime.
"""

from . import hb_tep as core
from .hb_tep_runtime_ha import TepEngine


def main() -> None:
    core.TepEngine = TepEngine
    core.main()


if __name__ == "__main__":
    main()
