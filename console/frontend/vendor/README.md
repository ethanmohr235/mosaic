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

## katex/

KaTeX, which typesets a note's maths in the read pane (`docs/modoc.md`).
Moved here from Mora's old window (`mora/frontend/vendor/katex/`) in U6,
unmodified: `katex.min.js` (loaded as a classic script, the global
`katex`), `katex.min.css` and its `fonts/`. MIT licence (`katex/LICENSE`).
