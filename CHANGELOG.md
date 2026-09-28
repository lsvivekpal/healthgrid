# Changelog

Notable changes to HealthGrid are documented here.

## 1.1.0 — 2026-09-28

### Added

- MSSQL (SQL Server) support: sessions, idle-in-transaction detection, blocking-chain resolution, `KILL`, database vitals, and top-10 tables via `sys.dm_exec_*` dynamic management views, using the `pymssql` driver (bundled FreeTDS, no system ODBC driver needed).
- README section documenting MSSQL grants (`VIEW SERVER STATE`, `ALTER ANY CONNECTION`) and its SSL limitation.

### Fixed

- Restored the instance vitals tiles (Connections, Sessions, Longest active query, Database size), which rendered as unstyled stacked text. The CSP nonce hardening in 1.0.0 dropped inline `<style>` support for anything not sharing the parent document's nonce; the vitals partial is loaded via a separate HTMX request and so never matched. Moved the tile and query-copy-button CSS into the static `responsive.css`, served under `style-src 'self'`.
- Fixed the PWA service worker permanently pinning stale `/static/` assets: it cached `/static/*` cache-first under a hardcoded cache name, so once an old asset was cached it was never refetched, surviving normal reloads and most hard refreshes. Switched to network-first (cache is now an offline-only fallback) and bumped the cache name so existing installs purge the stale entry on next activation.

### Known limitations

- MSSQL connections cannot honor the "SSL required" instance setting with the bundled `pymssql`/FreeTDS driver; HealthGrid refuses the connection rather than silently connecting without that guarantee. Uncheck SSL required for MSSQL instances and encrypt the network path some other way if needed.
- MSSQL does not support replication-slot management; that section of the instance page is hidden for MSSQL instances, matching MySQL/MariaDB.

## 1.0.0 — 2026-09-27

### Security

- Added a Content Security Policy and browser security headers, including Permissions Policy and same-origin cross-origin policies.
- Disabled wildcard CORS headers for WhiteNoise static assets.
- Marked the Django CSRF cookie as HttpOnly.
- Added Subresource Integrity to the pinned HTMX 1.9.12 script.
- Removed third-party Google Fonts requests; system font fallbacks are used.
- Switched inline script/style blocks to per-request CSP nonces and removed the broad `unsafe-inline` source from `script-src` and `style-src`.
- Added explicit no-store cache headers for public metadata endpoints.
