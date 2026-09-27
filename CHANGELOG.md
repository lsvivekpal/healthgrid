# Changelog

Notable changes to HealthGrid are documented here.

## 1.0.0 — 2026-09-27

### Security

- Added a Content Security Policy and browser security headers, including Permissions Policy and same-origin cross-origin policies.
- Disabled wildcard CORS headers for WhiteNoise static assets.
- Marked the Django CSRF cookie as HttpOnly.
- Added Subresource Integrity to the pinned HTMX 1.9.12 script.
- Removed third-party Google Fonts requests; system font fallbacks are used.
- Switched inline script/style blocks to per-request CSP nonces and removed the broad `unsafe-inline` source from `script-src` and `style-src`.
- Added explicit no-store cache headers for public metadata endpoints.
