# GitHub publication checklist — v2.1.2

Before creating the public release/tag:

1. Run `python3 tests/test_release.py` and `python3 tests/test_multipart_bytes.py`.
2. Run Python and shell syntax checks (the included GitHub Actions workflow does this automatically).
3. Confirm the storage port contract:
   - 8091 storage summary
   - 8093 mining aggregator
   - 8094 storage network aggregator
4. Confirm static edge entries explicitly use `role: "edge"` and `capacity_class: "best-effort"`.
5. Verify the release archive SHA-256 against the published checksum file.
6. For an existing blockchainapi.one-style explorer, apply `integrations/explorer/patch_hashburst_explorer.py` to a backup/candidate, run `php -l`, and verify strict CSP before replacing production.
7. Tag the exact commit used to generate the release archive.

## Licensing

This source tree intentionally does not invent a software license. The repository owner must choose and add the intended `LICENSE` before granting public reuse rights. Publishing without a license leaves default copyright restrictions in place.
