# Public explorer integration (v2.1.2)

The storage aggregator API changed from the legacy `capacity_total_gb` view to two explicit classes:

- `capacity_committable_gb` — primary/secondary capacity eligible for sovereign commitments and sellable headroom.
- `capacity_best_effort_gb` — edge replica capacity; never increases sellable capacity.

`patch_hashburst_explorer.py` upgrades an existing HashBurst `index.php` storage panel without adding inline style attributes or JavaScript `element.style` assignments. This preserves strict CSP deployments using nonce-based `script-src` and `style-src`.

Always back up the production explorer before applying the patch and run `php -l` on the candidate before replacement.
