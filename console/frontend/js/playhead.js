/* The shared playhead's clock and keys (docs/console.md → Settled for U5).
 *
 * The playhead itself is store state (store.state.playhead): one tick
 * every Motion pane reads, owned by the sim pane that last ran or was
 * focused. This module is what moves it on its own -- `play` -- and the
 * keys every Motion pane shares:
 *
 *   space  play / pause        , .  one tick back / on        [ ]  ten
 *
 * The clock is the one animation the console has besides the cursor
 * blink (docs/console.md → Visual system). It stops at the last tick. */

export const TICKS_PER_SECOND = 12;

export const PLAYHEAD_HINTS = [["space", "play"], [", .", "tick"], ["[ ]", "±10"]];

export function createPlayer({ store }) {
  let timer = 0;

  function stop() {
    clearInterval(timer);
    timer = 0;
  }

  function tick() {
    const p = store.state.playhead;
    if (p.tick >= p.count - 1) {
      store.setPlaying(false);
      return;
    }
    store.seek(p.tick + 1);
  }

  store.subscribe("playhead", (what) => {
    const p = store.state.playhead;
    if (!what || !what.playing) return;
    stop();
    if (!p.playing) return;
    if (p.tick >= p.count - 1) store.seek(0);
    timer = setInterval(tick, 1000 / TICKS_PER_SECOND);
  });
  store.subscribe("garden", stop);
  return { stop };
}

/** A Motion pane's playhead keys. True when `event` was one. */
export function playheadKey(store, event) {
  if (event.ctrlKey || event.metaKey || event.altKey) return false;
  const p = store.state.playhead;
  if (!p.count) return false;
  const moves = { ",": -1, ".": 1, "[": -10, "]": 10 };
  if (event.key === " ") {
    store.setPlaying(!p.playing);
    return true;
  }
  if (!(event.key in moves)) return false;
  store.setPlaying(false);
  store.step(moves[event.key]);
  return true;
}

/** The value at `tick` of a series, clamped to its length (a run that
 * failed early holds fewer ticks than the playhead's owner). */
export function at(series, tick) {
  if (!series || !series.length) return null;
  return series[Math.min(tick, series.length - 1)];
}
