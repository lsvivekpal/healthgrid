# Changelog

Notable changes to HealthGrid are documented here.

## 1.0.0 — 2026-09-27

### Security

- Added a Content Security Policy and browser security headers, including Permissions Policy and same-origin cross-origin policies.
- Disabled wildcard CORS headers for WhiteNoise static assets.
- Marked the Django CSRF cookie as HttpOnly.
- Added Subresource Integrity to the pinned HTMX 1.9.12 script.
- Removed third-party Google Fonts requests; system font fallbacks are used.

### Notes

- The CSP currently permits inline scripts and styles to support existing templates. Replacing those with nonce-based scripts and removing inline event handlers would further strengthen the policy.
