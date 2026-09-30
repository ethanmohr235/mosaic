# Vendored assets

Vendored locally rather than loaded from a CDN so the console doesn't
depend on network access or an external host at runtime.

## fonts/

Geist Mono 400 (`GeistMono-Regular`), 500 (`GeistMono-Medium`) and 600
(`GeistMono-SemiBold`). It is the only font family in the console
(`docs/console.md` → *Visual system*).

- Version: 1.7.2 of the `geist` npm package
- Source: https://cdn.jsdelivr.net/npm/geist@1.7.2/dist/fonts/geist-mono/
- License: SIL Open Font License 1.1 (`fonts/OFL.txt`)
- Fetched: 2026-09-29

The files are the package's static woff2 files, unmodified
(~150 KB total). The `@font-face` rules live in `../tokens.css`.
