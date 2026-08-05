# Vendored assets

Vendored locally rather than loaded from a CDN so the app doesn't depend on
network access or an external host at runtime.

## fonts/

IBM Plex Sans (400, 500, 600) and IBM Plex Serif (400, 500, 400 italic).

- Version: v23 (as served by Google Fonts on 2026-08-05)
- Source: https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Serif:ital,wght@0,400;0,500;1,400
- License: SIL Open Font License 1.1
- Fetched: 2026-08-05

Latin subset only — the Cyrillic, Greek and Vietnamese subsets Google also
serves are dropped, taking the set from 21 files to 6 (~193 KB total).
The `@font-face` rules live in `../style.css`.

## Removed

`cytoscape.min.js` (v3.34.0, MIT) was vendored here until 2026-08-05, when
the graph moved to a hand-written canvas renderer (`../graph.js`). At 425 KB
it roughly covered the fonts' 193 KB twice over. `cytoscape-edgehandles`
and a lodash shim were also briefly vendored during the drag-to-connect
work, and removed once that gesture was hand-rolled instead.
