# GitHub publication checklist — v2.1.6

Current published release: **v2.1.6**.

The canonical release commit is `d031185e83dbbce805d31ae9317d0c8398a3cc90` and the
annotated tag is `v2.1.6`.

## Publication / verification checklist

1. Run the repository GitHub Actions workflow and require a green result on the exact release commit.
2. Keep historical compatibility regressions for v2.1.2-v2.1.5 enabled; their versioned names are intentional.
3. Run the v2.1.6 TEP identity/reconciliation tests, installer upgrade/idempotency sandbox and release contract.
4. Confirm the storage port contract:
   - 8091 storage summary
   - 8093 mining aggregator
   - 8094 storage network aggregator
5. Confirm static edge entries explicitly use `role: "edge"` and `capacity_class: "best-effort"`.
6. Verify the release/tag points at the exact main commit that passed CI.
7. Verify the GitHub Release is public (`draft=false`, `prerelease=false`) and the source `.zip` / `.tar.gz` archives are available.
8. Confirm final field validation and the five-node TEP rollout are recorded in `docs/RELEASE-v2.1.6.md`.
9. For an existing blockchainapi.one-style explorer, apply any explorer changes only through a backup/candidate, run `php -l`, and verify strict CSP before replacing production.

## Licensing

The repository is released under the MIT license; see `LICENSE`.

## Historical documentation

Older release notes and regression files remain in the tree as historical and compatibility evidence. They should not be renamed to v2.1.6 because doing so would erase the provenance of the release contracts they test.
