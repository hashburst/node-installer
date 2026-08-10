# Changelog

## 2.1.2
- Reserve 8093 for mining and make 8094 the storage aggregator default everywhere.
- Set production storage aggregator timeout to 3 seconds.
- Fail safe to `capacity_class: unknown` for offline nodes without explicit class/role.
- Add explicit roles to the storage node example configuration.
- Add CSP-safe public explorer storage-schema patcher and regression tests.

## 2.1.1
- Preserve configured `best-effort` classification for offline edge nodes.

## 2.1.0
- Introduce primary/secondary/edge roles and committable vs best-effort capacity accounting.
