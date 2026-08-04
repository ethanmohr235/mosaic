# Vendored libraries

## cytoscape.min.js

- Version: 3.34.0
- Source: https://unpkg.com/cytoscape@3.34.0/dist/cytoscape.min.js
- License: MIT (see header comment in the file)
- Fetched: 2026-08-04

Vendored locally rather than loaded from a CDN so the app doesn't depend on
network access or an external host at runtime.

## cytoscape-edgehandles.js

- Version: 4.0.1
- Source: https://unpkg.com/cytoscape-edgehandles@4.0.1/cytoscape-edgehandles.js
- License: MIT (see LICENSE in the package)
- Fetched: 2026-08-04

Enables drag-to-connect between nodes. Not minified -- this release doesn't
ship a `dist/` bundle, only the plain webpack output at the package root.

## lodash-shim.js

Hand-written, not fetched from anywhere. `cytoscape-edgehandles`'s UMD build
expects a global `window._` with `.memoize`/`.throttle` (lodash's
conventional global), even outside a CommonJS/AMD environment. Rather than
vendor all of lodash to satisfy two functions, this defines minimal
standalone equivalents matching the exact (default-options, no custom
resolver) call signatures edgehandles actually uses. Must load before
`cytoscape-edgehandles.js`.
