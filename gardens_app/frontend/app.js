// gardens_app/frontend/app.js
//
// The Gardens window: one list, one detail pane, and the four verbs the
// six app windows could not do for themselves -- create, rename,
// import, export -- plus delete and per-garden sync.
//
// What this window deliberately does *not* do is switch anybody's
// garden. It is its own process (gardens_app/app.py explains why), so
// the app that opened it is out of reach; "Open in" launches a window
// already on the chosen garden instead, and an app that is already
// running finds a new garden in its own switcher, which reads the
// directory every time it opens.

let api;
let gardens = [];        // the rows api.list_gardens() last returned
let selected = null;     // garden name, or null when nothing is selected
let sync = null;         // the selected garden's api.sync_status(), lazily
let toastTimer = null;
let promptResolve = null;   // the open prompt modal's resolver, if any
let promptValidate = nameProblem;  // whichever check submitPrompt() runs
let confirmResolve = null;

const $ = (id) => document.getElementById(id);

// ---- bridge ---------------------------------------------------------

async function call(promise, failure) {
  try {
    return await promise;
  } catch (error) {
    toast(failure || String(error && error.message ? error.message : error), true);
    return null;
  }
}

// `action` turns the toast into the undo affordance delete needs: the
// garden is only moved aside on disk (core/gardens.py trashes rather
// than removes), so the undo is real and not a five-second grace timer.
function toast(message, isError, action) {
  const el = $("toast");
  el.textContent = message;
  el.classList.toggle("error", !!isError);
  if (action) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = action.label;
    btn.onclick = () => { el.classList.add("hidden"); action.run(); };
    el.appendChild(btn);
  }
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), isError || action ? 6000 : 2600);
}

// ---- formatting -----------------------------------------------------

function formatCount(n) {
  return n === null || n === undefined ? "—" : String(n);
}

function formatBytes(bytes) {
  if (!bytes) return "0 KB";
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Coarse on purpose: the question a garden list answers with a date is
 *  "is this the one I was in yesterday, or the one I abandoned in
 *  spring", and a timestamp to the minute answers it worse than this. */
function formatWhen(epochSeconds) {
  if (!epochSeconds) return "—";
  const days = Math.floor((Date.now() / 1000 - epochSeconds) / 86400);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 30) return `${days} days ago`;
  if (days < 365) return `${Math.round(days / 30)} months ago`;
  return `${Math.round(days / 365)} years ago`;
}

// ---- where this garden can be opened ----------------------------------
//
// Only the console now: every old window has been ported into it
// (docs/console.md). api_common.APPS is the Python-side source of the ids.

const APPS = [
  {
    id: "console", name: "Mosaic", icon: `<svg width="20" height="20" viewBox="0 0 40 40" aria-hidden="true">
        <rect x="6" y="9" width="28" height="22" fill="none" stroke="var(--fg)" stroke-width="1.6" opacity="0.6"></rect>
        <rect x="6" y="9" width="10" height="7" fill="var(--accent)"></rect>
        <rect x="20" y="9" width="1.6" height="22" fill="var(--fg)" opacity="0.4"></rect>
      </svg>`,
  },
];

// ---- list -----------------------------------------------------------

function renderList() {
  const host = $("list");
  host.innerHTML = "";
  $("list-empty").classList.toggle("hidden", gardens.length > 0);
  $("garden-count").textContent =
    `${gardens.length} garden${gardens.length === 1 ? "" : "s"}`;

  for (const garden of gardens) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "garden-row" + (garden.name === selected ? " on" : "");

    const col = document.createElement("div");
    col.className = "col";
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = garden.name;
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = garden.motes === null
      ? "unreadable"
      : `${garden.motes} motes · ${formatWhen(garden.modified)}`;
    col.append(name, meta);
    row.appendChild(col);

    if (garden.sync_dir) {
      const dot = document.createElement("span");
      dot.className = "sync-dot";
      dot.title = "Syncing";
      row.appendChild(dot);
    }

    row.onclick = () => select(garden.name);
    host.appendChild(row);
  }
}

// ---- detail ---------------------------------------------------------

function current() {
  return gardens.find((g) => g.name === selected) || null;
}

async function select(name, { openSync = false } = {}) {
  selected = name;
  sync = null;
  renderList();
  renderDetail();
  if (!name) return;
  // Sync status needs its own connection per garden (gardens_app/api.py),
  // so it is fetched for the selected garden only, after the pane is
  // already up rather than before it.
  sync = await call(api.sync_status(name));
  if (selected === name) renderSync();
  // Tools -> Sync in an app opens this window straight at that garden's
  // sync settings (run_gardens.py --sync), which is the one deep link
  // worth having: it is where a feature the apps used to own now lives.
  if (openSync) $("sync-section").scrollIntoView({ behavior: "smooth", block: "center" });
}

function renderDetail() {
  const garden = current();
  $("detail-empty").classList.toggle("hidden", !!garden);
  $("detail-body").classList.toggle("hidden", !garden);
  if (!garden) return;

  $("detail-name").textContent = garden.name;
  $("detail-path").textContent = `gardens/${garden.name}.db`;
  $("stat-motes").textContent = formatCount(garden.motes);
  $("stat-rels").textContent = formatCount(garden.relationships);
  $("stat-size").textContent = formatBytes(garden.bytes);
  $("stat-modified").textContent = formatWhen(garden.modified);
  $("detail-sync-pill").classList.toggle("hidden", !garden.sync_dir);

  const host = $("open-in");
  host.innerHTML = "";
  for (const app of APPS) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "pill-btn app-chip";
    chip.innerHTML = app.icon;
    chip.append(app.name);
    chip.onclick = async () => {
      await call(api.open_app(app.id, garden.name));
      toast(`Opening ${app.name} on ‘${garden.name}’…`);
    };
    host.appendChild(chip);
  }

  renderSync();
}

/** The token in a server URL is a bearer credential, not a bearer of
 *  the human's attention -- shown as-is it's one screen-share or
 *  screenshot away from a leaked secret, so this window never displays
 *  it. */
function maskSyncTarget(target) {
  return target.replace(/\/\/[^@]*@/, "//•••@");
}

function renderSync() {
  const garden = current();
  if (!garden) return;
  const on = !!(sync ? sync.sync_dir : garden.sync_dir);
  $("sync-off").classList.toggle("hidden", on);
  $("sync-on").classList.toggle("hidden", !on);
  if (!on || !sync) return;

  $("sync-folder").textContent = maskSyncTarget(sync.sync_dir);
  $("sync-auto").checked = !!sync.auto_sync;

  // The two numbers worth showing: what this device has written but not
  // yet pushed to the server, and how many other devices have ever
  // written into it. Both come straight from core/sync.py::status.
  const pending = sync.local_ops_pending;
  const remotes = (sync.remotes || []).length;
  $("sync-lines").textContent =
    `${pending === 0 ? "Nothing waiting to push" : `${pending} change${pending === 1 ? "" : "s"} waiting to push`}` +
    ` · ${remotes} other device${remotes === 1 ? "" : "s"} seen · this device ${sync.device_id}`;
}

// ---- modals ---------------------------------------------------------
//
// window.prompt/confirm is what Monarch's gardens modal used, and it is
// the one part of it that was never in the design language -- a WebKit
// system dialog in the middle of a dark window. These two are the same
// questions in the window's own type.

function askName({
  title,
  note,
  value = "",
  okLabel = "Create",
  placeholder = "garden-name",
  validate = nameProblem,
}) {
  $("prompt-title").textContent = title;
  $("prompt-note").textContent = note;
  $("prompt-error").classList.add("hidden");
  $("prompt-ok").textContent = okLabel;
  const input = $("prompt-input");
  input.value = value;
  input.placeholder = placeholder;
  // What a rename started from: submitPrompt() treats that one name as
  // free rather than as a collision with the garden being renamed.
  input.dataset.original = value;
  promptValidate = validate;
  $("prompt-overlay").classList.remove("hidden");
  input.focus();
  input.select();
  return new Promise((resolve) => { promptResolve = resolve; });
}

function closePrompt(answer) {
  $("prompt-overlay").classList.add("hidden");
  const resolve = promptResolve;
  promptResolve = null;
  if (resolve) resolve(answer);
}

/** The same rule core/db.py::validate_garden_name enforces, checked here
 *  too so a mistyped name is a hint under the field rather than a
 *  round-trip that comes back as a toast. Python stays the authority --
 *  this is the second line of defence, not the only one. */
function nameProblem(name) {
  if (!name) return "A garden needs a name.";
  if (!/^[A-Za-z0-9_-]{1,64}$/.test(name)) {
    return "Use 1–64 letters, digits, hyphen or underscore.";
  }
  if (gardens.some((g) => g.name === name)) return `‘${name}’ already exists.`;
  return null;
}

/** Loose on purpose -- core/sync.py::init is the real authority on what
 *  it'll accept as a remote target, and rejects it with a toast if not.
 *  This just catches the empty field and the obviously-not-a-URL typo
 *  (a folder path pasted here, say) before the round trip. */
function urlProblem(url) {
  if (!url) return "A server needs a URL.";
  if (!/^https?:\/\//.test(url)) return "Paste the server URL, starting with https://.";
  return null;
}

function confirmDelete(name, motes) {
  $("confirm-title").textContent = `Delete ‘${name}’?`;
  $("confirm-note").textContent =
    `${motes === null ? "This garden" : `${motes} mote${motes === 1 ? "" : "s"}`}` +
    " goes with it. The file is moved aside rather than erased, so this can be" +
    " undone right afterwards — and any window already open on this garden keeps" +
    " running until it is closed.";
  $("confirm-overlay").classList.remove("hidden");
  return new Promise((resolve) => { confirmResolve = resolve; });
}

function closeConfirm(answer) {
  $("confirm-overlay").classList.add("hidden");
  const resolve = confirmResolve;
  confirmResolve = null;
  if (resolve) resolve(answer);
}

// ---- the verbs ------------------------------------------------------

/** Re-reads the whole list from disk. Every verb here ends in one of
 *  these rather than patching the row it changed: the counts, the size
 *  and the timestamp all move under us anyway -- six other windows are
 *  writing to these files -- so a list rebuilt from the directory is
 *  both simpler and more honest than one kept in step by hand. A
 *  selection whose garden is gone (deleted, renamed) drops. */
async function refresh() {
  gardens = (await call(api.list_gardens())) || [];
  if (!gardens.some((g) => g.name === selected)) selected = null;
  renderList();
  renderDetail();
}

async function newGarden() {
  const name = await askName({
    title: "New garden",
    note: "Letters, digits, hyphen and underscore. Up to 64.",
  });
  if (name === null) return;
  if (!(await call(api.create_garden(name)))) return;
  await refresh();
  await select(name);
  toast(`Created ‘${name}’.`);
}

async function renameGarden() {
  const garden = current();
  if (!garden) return;
  const name = await askName({
    title: `Rename ‘${garden.name}’`,
    note: garden.sync_dir
      ? "This garden syncs through a sync server, which tracks it by name — stop syncing first."
      : "Every window already open on this garden keeps its own file handle, so rename before reopening them.",
    value: garden.name,
    okLabel: "Rename",
  });
  if (name === null || name === garden.name) return;
  if (!(await call(api.rename_garden(garden.name, name)))) return;
  await refresh();
  await select(name);
  toast(`Renamed to ‘${name}’.`);
}

async function deleteGarden() {
  const garden = current();
  if (!garden) return;
  if (!(await confirmDelete(garden.name, garden.motes))) return;
  const name = garden.name;
  if (!(await call(api.delete_garden(name)))) return;
  selected = null;
  await refresh();
  toast(`Deleted ‘${name}’.`, false, {
    label: "Undo",
    run: async () => {
      if (!(await call(api.restore_garden(name)))) return;
      await refresh();
      await select(name);
      toast(`Restored ‘${name}’.`);
    },
  });
}

async function exportGarden() {
  const garden = current();
  if (!garden) return;
  const result = await call(api.export_garden_to_file(garden.name));
  if (!result || result.cancelled) return;
  toast(`Exported to ${result.path}`);
}

/** An import always lands in a new garden -- core/garden_io.py refuses
 *  to merge into one that already holds any of the incoming mote ids --
 *  so the name is asked for before the file, not after. */
async function importGarden() {
  const name = await askName({
    title: "Import a garden",
    note: "The export lands in a new garden under this name; you'll pick the file next.",
    okLabel: "Choose file…",
  });
  if (name === null) return;
  const result = await call(api.import_garden_into_new(name));
  if (!result || result.cancelled) return;
  await refresh();
  await select(result.garden);
  toast(`Imported ${result.motes} motes and ${result.relationships} relationships.`);
}

// ---- sync actions ---------------------------------------------------

async function connectServer() {
  const garden = current();
  if (!garden) return;
  const url = await askName({
    title: garden.sync_dir ? `Change ‘${garden.name}’ sync server` : "Connect to a sync server",
    note: "The server URL from `docker compose up`, including its token: http://<token>@host:8080/ (or https:// behind a TLS proxy).",
    value: garden.sync_dir || "",
    okLabel: "Connect",
    placeholder: "http://<token>@host:8080/",
    validate: urlProblem,
  });
  if (url === null) return;
  if (!(await call(api.enable_sync(garden.name, url)))) return;
  await refresh();
  await select(garden.name);
  toast(`‘${garden.name}’ now syncs through ${maskSyncTarget(url)}.`);
}

async function syncNow() {
  const garden = current();
  if (!garden) return;
  const result = await call(api.sync_now(garden.name));
  if (!result) return;
  await refresh();
  await select(garden.name);
  toast(`Pushed ${result.push.pushed}, applied ${result.pull.applied}.`);
}

async function stopSync() {
  const garden = current();
  if (!garden) return;
  if (!(await call(api.disable_sync(garden.name)))) return;
  await refresh();
  await select(garden.name);
  toast(`‘${garden.name}’ no longer syncs. Its history is kept, so turning it back on resumes.`);
}

// ---- wiring ---------------------------------------------------------

function wire() {
  $("new-btn").onclick = newGarden;
  $("import-btn").onclick = importGarden;
  $("rename-btn").onclick = renameGarden;
  $("export-btn").onclick = exportGarden;
  $("delete-btn").onclick = deleteGarden;

  $("sync-enable").onclick = connectServer;
  $("sync-change").onclick = connectServer;
  $("sync-now").onclick = syncNow;
  $("sync-disable").onclick = stopSync;
  $("sync-auto").onchange = async (event) => {
    const garden = current();
    if (!garden) return;
    await call(api.set_auto_sync(garden.name, event.target.checked));
  };

  $("prompt-cancel").onclick = () => closePrompt(null);
  $("prompt-ok").onclick = submitPrompt;
  $("prompt-overlay").onmousedown = (e) => {
    if (e.target === $("prompt-overlay")) closePrompt(null);
  };
  $("prompt-input").onkeydown = (e) => {
    if (e.key === "Enter") submitPrompt();
    if (e.key === "Escape") closePrompt(null);
  };

  $("confirm-cancel").onclick = () => closeConfirm(false);
  $("confirm-ok").onclick = () => closeConfirm(true);
  $("confirm-overlay").onmousedown = (e) => {
    if (e.target === $("confirm-overlay")) closeConfirm(false);
  };

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (promptResolve) closePrompt(null);
      else if (confirmResolve) closeConfirm(false);
    }
  });
}

function submitPrompt() {
  const name = $("prompt-input").value.trim();
  // Renaming to the name it already has is a no-op, not a collision --
  // nameProblem() only knows the list, so the exception lives here.
  const problem = name === $("prompt-input").dataset.original ? null : promptValidate(name);
  if (problem) {
    $("prompt-error").textContent = problem;
    $("prompt-error").classList.remove("hidden");
    return;
  }
  closePrompt(name);
}

// ---- start ----------------------------------------------------------

window.addEventListener("pywebviewready", async () => {
  api = window.pywebview.api;
  wire();
  const start = await call(api.initial());
  if (!start) return;
  gardens = start.gardens;
  renderList();
  // Whichever app opened this window passed its own garden along
  // (api_common.open_gardens), so the list comes up on the garden the
  // user was just looking at rather than on nothing.
  if (start.select && gardens.some((g) => g.name === start.select)) {
    await select(start.select, { openSync: start.open_sync });
  } else {
    renderDetail();
  }
});
