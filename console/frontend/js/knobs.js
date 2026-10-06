/* Knobs in a pane (docs/console.md → Settled for knobs). `--set m.k=value
 * [unit]` on a pane's line tries a value and writes nothing; a pane that
 * took any answers with `sets` (each as typed) and `keep` (`{command,
 * cleared}`). `W` runs the keep command on the command line, so the write
 * is in the history, the log and undo like any typed line, and the pane
 * then takes its line without the knobs. */

export const KEEP_HINT = ["W", "keep knobs"];

export const hasKnobs = (payload) => !!(payload && payload.keep);

/** `set a.b=1, a.c=2 ohm` for a pane's header, or "" with no knobs. */
export function knobSummary(payload) {
  return payload && payload.sets && payload.sets.length ? `set ${payload.sets.join(", ")}` : "";
}

/** Write the knobs, then drop them from the line. A write that failed
 * (an archived garden, a gone mote) leaves the line as it was. */
export async function keepKnobs(ctx, payload) {
  if (!hasKnobs(payload)) return false;
  const outcome = await ctx.runCommand(payload.keep.command);
  if (outcome && outcome.level === "fault") return true;
  await ctx.setCommand(payload.keep.cleared);
  return true;
}
