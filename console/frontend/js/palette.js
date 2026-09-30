/* The species palette: the one copy every view colors motes with
 * (docs/console.md → *Visual system* → *Color*).
 *
 * Mosaic privileges no type, so a mote's color is its species'. The
 * palette has 24 entries: 12 hues walked by the golden angle, at two
 * tones. Lightness and chroma come from the band in tokens.css
 * (--species-l1/--species-c1, --species-l2/--species-c2), so no color is
 * written down here. A species mote itself is drawn --species-lift
 * lighter (toward --bright); a mote with no species is --dim.
 *
 * Which entry a species gets depends on which other species exist, so
 * the universe must be the *garden's* whole species list (the table
 * payload's `speciesIds`), never just the species on screen: otherwise
 * one species would get one color here and another in a pane showing
 * fewer motes. Each species prefers the entry its id hashes to, so a
 * color is stable across sessions and unaffected by most additions, and
 * probes forward when that entry is taken, so no two species share one
 * until the palette is full. The ids are claimed in sorted order, so
 * the result doesn't depend on the order they arrive in. This is the
 * same algorithm (and hash) Monolith's old window used. */

const HUE_STEP = 137.508;   // the golden angle, in degrees
const LAP = 12;             // hues per tone
const TONES = 2;
export const PALETTE_SIZE = LAP * TONES;

let band = null;

/** The band's numbers, read from tokens.css once. */
function bandTokens() {
  if (band) return band;
  const css = getComputedStyle(document.documentElement);
  const num = (name) => parseFloat(css.getPropertyValue(name));
  band = {
    tones: [{ l: num("--species-l1"), c: num("--species-c1") }, { l: num("--species-l2"), c: num("--species-c2") }],
    lift: num("--species-lift"),
    offset: num("--species-hue-offset"),
  };
  return band;
}

/** A 32-bit string hash (murmur-style mixing), as the old window had it. */
export function hashString(text) {
  let h = 1779033703 ^ text.length;
  for (let i = 0; i < text.length; i++) {
    h = Math.imul(h ^ text.charCodeAt(i), 3432918353);
    h = (h << 13) | (h >>> 19);
  }
  h = Math.imul(h ^ (h >>> 16), 2246822519);
  h = Math.imul(h ^ (h >>> 13), 3266489917);
  return (h ^= h >>> 16) >>> 0;
}

/** species id → slot, over the garden's whole species list. */
export function claimSlots(ids) {
  const slots = new Map();
  const taken = new Set();
  for (const id of [...new Set(ids || [])].sort()) {
    let slot = hashString(id) % PALETTE_SIZE;
    for (let step = 0; step < PALETTE_SIZE && taken.has(slot); step++) slot = (slot + 1) % PALETTE_SIZE;
    taken.add(slot);
    slots.set(id, slot);
  }
  return slots;
}

/** The oklch() string for palette slot `slot`; `species` lifts it toward --bright. */
export function slotColor(slot, species = false) {
  const { tones, lift, offset } = bandTokens();
  const index = ((slot % PALETTE_SIZE) + PALETTE_SIZE) % PALETTE_SIZE;
  const tone = tones[Math.floor(index / LAP)];
  const hue = (index * HUE_STEP + offset) % 360;
  const l = species ? tone.l + lift : tone.l;
  return `oklch(${l.toFixed(3)} ${tone.c.toFixed(3)} ${hue.toFixed(1)})`;
}

/**
 * A palette over one garden's species: {colorOf(mote), slotOf(speciesId)}.
 * `mote` is {id, speciesId, kinds}. A species created since the payload
 * was fetched falls back to its unprobed slot, which is almost always
 * the one it will claim.
 */
export function createPalette(speciesIds) {
  const slots = claimSlots(speciesIds);
  const slotOf = (id) => (slots.has(id) ? slots.get(id) : hashString(id) % PALETTE_SIZE);
  function colorOf(mote) {
    if (!mote) return "var(--dim)";
    if ((mote.kinds || []).includes("species")) return slotColor(slotOf(mote.id || ""), true);
    if (!mote.speciesId) return "var(--dim)";
    return slotColor(slotOf(mote.speciesId));
  }
  return { colorOf, slotOf };
}
