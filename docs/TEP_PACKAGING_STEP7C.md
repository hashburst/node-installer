# HB-TEP canonical packaging (Step 7C)

The production daemon is `tep/hb_tep.py`. `patched/`, `staging/`, and `baseline/` remain evidence/regression material and are not installed by the node installer.

The installer packages the `tep` Python package under `/opt/hashburst-tep/tep`, installs `systemd/hashburst-tep.service`, and creates `/etc/hashburst/hashburst-tep.env` from the example only when no existing file is present.

Packaging is additive: the installer never enables, starts, or restarts `hashburst-tep.service`. Production activation is an explicit rollout operation performed one node at a time after backup and validation.

The service executes `python3 -m tep.hb_tep`, reads local configuration from `/etc/hashburst/hashburst-tep.env`, keeps relay disabled by default, writes state only below `/var/lib/hashburst` and logs below `/var/log/hashburst`, and retains the Step 7A loopback IPC on `127.0.0.1:47778`.

HB-TEP-APP requires Python `cryptography` for X25519/AES-256-GCM. Production rollout must verify this dependency before service activation. No installer path uses `pip --break-system-packages`.
