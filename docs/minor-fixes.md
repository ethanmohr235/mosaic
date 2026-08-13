- [x] ~~ctrl~~ shift + lmb + drag = multi select box that only selects relations, with a different color. lmb + drag still selects only motes. (Shipped as shift+drag, not ctrl+drag as originally written here -- ctrl was already taken by drag-to-connect. Additive mote box-select, shift+drag's old meaning, is gone; shift+click or repeated box-selects cover that instead.)
- [x] aesthetics pass. note how the color scheme changed slightly and we condensed the top bar buttons into dropdowns for future expansion. See the Claude Design project below.

Both shipped: the accent retuned to `oklch(0.74 0.10 68)` (from `oklch(0.78 0.13 70)`), and the topbar condensed to a garden dropdown (switch/create, with "Manage gardens…" bridging to the existing modal for Delete/Export/Import) plus File/View/Tools dropdowns hosting Export to Calendar, Reset view, a new Hide/Show controls toggle, the simulation controls, and inert "soon"-badged stubs for the mockup's placeholder items.

Use the claude_design MCP (https://api.anthropic.com/v1/design/mcp, auth via /design-login) to import this project:
https://claude.ai/design/p/41befa85-7d74-43d5-ac9c-9957286f0eeb?file=MonOSS+Graph.dc.html

Focus on these files (the whole project is readable):
- `MonOSS Graph.dc.html`

Also read these files the selection imports:
- `support.js`

Implement: `MonOSS Graph.dc.html`
