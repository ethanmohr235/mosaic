let api;
let cy;
let currentGarden = null;
let focusId = null;
let connectSourceId = null;

window.addEventListener("error", (e) => {
  console.error(e.error || e.message);
  showToast(String((e.error && e.error.message) || e.message));
});
window.addEventListener("unhandledrejection", (e) => {
  console.error(e.reason);
  showToast(String((e.reason && e.reason.message) || e.reason));
});

function lastFocusKey(garden) {
  return `monoss:lastFocus:${garden}`;
}

function positionsKey(garden) {
  return `monoss:positions:${garden}`;
}

function loadPositions(garden) {
  try {
    return JSON.parse(localStorage.getItem(positionsKey(garden))) || {};
  } catch {
    return {};
  }
}

function saveAllPositions(garden) {
  if (!cy) return;
  const positions = loadPositions(garden);
  cy.nodes().forEach((n) => {
    const p = n.position();
    positions[n.id()] = { x: p.x, y: p.y };
  });
  localStorage.setItem(positionsKey(garden), JSON.stringify(positions));
}

function removePosition(garden, moteId) {
  const positions = loadPositions(garden);
  delete positions[moteId];
  localStorage.setItem(positionsKey(garden), JSON.stringify(positions));
}

function showToast(message) {
  const toast = document.getElementById("toast");
  toast.textContent = message;
  toast.classList.remove("hidden");
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => toast.classList.add("hidden"), 3500);
}

async function call(promise) {
  try {
    return await promise;
  } catch (err) {
    showToast(String(err.message || err));
    throw err;
  }
}

function nodeLabel(mote) {
  const props = mote.properties || {};
  const name = props.name;
  if (typeof name === "string" && name.trim()) return name;

  const keys = Object.keys(props).sort();
  if (keys.length > 0) {
    const key = keys[0];
    const value = typeof props[key] === "string" ? props[key] : JSON.stringify(props[key]);
    return `${key}: ${value}`;
  }
  return mote.id.slice(0, 8) + "…";
}

function buildElements(payload) {
  const nodes = payload.motes.map((m) => ({
    data: { id: m.id, label: nodeLabel(m) },
  }));
  const edges = payload.relationships.map((r) => ({
    data: {
      id: r.id,
      source: r.from_id,
      target: r.to_id,
      label: r.type,
    },
  }));
  return [...nodes, ...edges];
}

function renderGraph(payload) {
  focusId = payload.focus_id;
  const cyEl = document.getElementById("cy");
  const emptyEl = document.getElementById("empty-state");

  if (focusId === null) {
    cyEl.classList.add("hidden");
    emptyEl.classList.remove("hidden");
    return;
  }
  emptyEl.classList.add("hidden");
  cyEl.classList.remove("hidden");
  // #cy may have just gone from display:none to visible -- Cytoscape's
  // canvas doesn't pick that up on its own, so it must be told to
  // re-measure its container before adding/laying out elements.
  cy.resize();

  cy.elements().remove();
  cy.add(buildElements(payload));
  cy.getElementById(focusId).addClass("focused");

  // Nodes with a remembered position are pinned there and excluded from
  // the force simulation (locked), so re-focusing/re-opening a garden
  // doesn't reshuffle everything you've already looked at. Cytoscape's
  // layouts skip repositioning locked nodes regardless of `randomize`,
  // so leaving randomize at its default (true) still places brand-new
  // nodes normally -- it only randomizes the ones that are unlocked.
  const stored = loadPositions(currentGarden);
  cy.nodes().forEach((n) => {
    const pos = stored[n.id()];
    if (pos) {
      n.position(pos);
      n.lock();
    }
  });

  const layout = cy.layout({
    name: "cose",
    animate: true,
    animationDuration: 500,
    fit: true,
    padding: 60,
    nodeDimensionsIncludeLabels: true,
    idealEdgeLength: 90,
    nodeRepulsion: 9000,
  });
  layout.one("layoutstop", () => {
    cy.nodes().unlock();
    // A single node has nothing to be positioned relative to -- saving it
    // would later lock it (and whatever it's eventually connected to) at
    // a spot that was never actually laid out against anything, silently
    // defeating the layout once real relationships bring it into a bigger
    // view.
    if (cy.nodes().length > 1) {
      saveAllPositions(currentGarden);
    }
  });
  layout.run();
}

async function refreshMoteSelect() {
  const select = document.getElementById("jump-select");
  const motes = await call(api.list_motes());
  select.innerHTML = '<option value="">Jump to mote…</option>';
  for (const m of motes) {
    const opt = document.createElement("option");
    opt.value = m.id;
    opt.textContent = nodeLabel(m);
    select.appendChild(opt);
  }
  select.value = focusId || "";
}

async function refocus(moteId) {
  const payload = await call(api.focus(moteId));
  localStorage.setItem(lastFocusKey(currentGarden), payload.focus_id || "");
  renderGraph(payload);
  await refreshMoteSelect();
  return payload;
}

async function loadGarden(garden) {
  currentGarden = garden;
  connectSourceId = null;
  closeDetailPanel();
  const stored = localStorage.getItem(lastFocusKey(garden)) || null;
  await refocus(stored);
}

async function openDetailPanel(moteId) {
  const [mote, rels] = await Promise.all([
    call(api.get_mote(moteId)),
    call(api.mote_relationships(moteId)),
  ]);
  if (!mote) return;

  document.getElementById("detail-id").textContent = mote.id;
  renderProperties(mote);
  renderRelationships(moteId, rels);
  document.getElementById("detail-panel").classList.remove("hidden");
}

function closeDetailPanel() {
  document.getElementById("detail-panel").classList.add("hidden");
  if (cy) cy.elements().unselect();
}

function renderProperties(mote) {
  const container = document.getElementById("detail-properties");
  container.innerHTML = "";
  for (const [key, value] of Object.entries(mote.properties)) {
    const row = document.createElement("div");
    row.className = "detail-row";

    const keyEl = document.createElement("span");
    keyEl.textContent = key;
    keyEl.style.minWidth = "70px";
    keyEl.style.color = "var(--muted)";

    const valueInput = document.createElement("input");
    valueInput.type = "text";
    valueInput.value = typeof value === "string" ? value : JSON.stringify(value);
    valueInput.addEventListener("change", async () => {
      await call(api.update_mote(mote.id, { [key]: valueInput.value }));
    });

    const delBtn = document.createElement("button");
    delBtn.textContent = "×";
    delBtn.addEventListener("click", async () => {
      await call(api.delete_property(mote.id, key));
      openDetailPanel(mote.id);
    });

    row.append(keyEl, valueInput, delBtn);
    container.appendChild(row);
  }
}

function renderRelationships(moteId, rels) {
  const container = document.getElementById("detail-relationships");
  container.innerHTML = "";
  if (rels.length === 0) {
    const empty = document.createElement("div");
    empty.style.color = "var(--muted)";
    empty.textContent = "None";
    container.appendChild(empty);
  }
  for (const r of rels) {
    const row = document.createElement("div");
    row.className = "rel-row";

    const other = r.from_id === moteId ? r.to_id : r.from_id;
    const arrow = r.from_id === moteId ? "→" : "←";
    const label = document.createElement("span");
    label.className = "rel-label";
    label.textContent = `${arrow} ${r.type} ${other.slice(0, 8)}…`;
    label.title = other;

    const delBtn = document.createElement("button");
    delBtn.textContent = "×";
    delBtn.addEventListener("click", async () => {
      await call(api.delete_relationship(r.id));
      openDetailPanel(moteId);
      refocus(focusId);
    });

    row.append(label, delBtn);
    container.appendChild(row);
  }

  const connectBtn = document.createElement("button");
  connectBtn.textContent = "Connect to…";
  connectBtn.style.marginTop = "0.5rem";
  connectBtn.addEventListener("click", () => {
    connectSourceId = moteId;
    showToast("Click another mote on the graph to connect to it (click it again to cancel).");
  });
  container.appendChild(connectBtn);
}

async function connectNodes(sourceId, targetId) {
  const type = window.prompt("Relationship type:");
  if (!type) return;
  await call(api.create_relationship(sourceId, targetId, type));
  await refocus(sourceId);
  await openDetailPanel(sourceId);
}

function setupQuickAdd() {
  const rowsEl = document.getElementById("quick-add-rows");

  function addRow() {
    const row = document.createElement("div");
    row.className = "prop-row";
    const keyInput = document.createElement("input");
    keyInput.placeholder = "key";
    const valueInput = document.createElement("input");
    valueInput.placeholder = "value";
    row.append(keyInput, valueInput);
    rowsEl.appendChild(row);
  }
  addRow();

  document.getElementById("add-property-row").addEventListener("click", addRow);

  document.getElementById("create-mote-btn").addEventListener("click", async () => {
    const props = {};
    for (const row of rowsEl.querySelectorAll(".prop-row")) {
      const [keyInput, valueInput] = row.querySelectorAll("input");
      if (keyInput.value.trim()) props[keyInput.value.trim()] = valueInput.value;
    }
    const mote = await call(api.create_mote(props));
    rowsEl.innerHTML = "";
    addRow();
    await refocus(mote.id);
  });
}

function setupDetailPanel() {
  document.getElementById("close-panel").addEventListener("click", closeDetailPanel);

  document.getElementById("detail-add-property-btn").addEventListener("click", async () => {
    const keyInput = document.getElementById("detail-new-key");
    const valueInput = document.getElementById("detail-new-value");
    if (!keyInput.value.trim() || !focusId) return;
    await call(api.update_mote(focusId, { [keyInput.value.trim()]: valueInput.value }));
    keyInput.value = "";
    valueInput.value = "";
    await openDetailPanel(focusId);
  });

  document.getElementById("delete-mote-btn").addEventListener("click", async () => {
    if (!focusId) return;
    if (!window.confirm("Delete this mote and all its relationships?")) return;
    await call(api.delete_mote(focusId));
    localStorage.removeItem(lastFocusKey(currentGarden));
    removePosition(currentGarden, focusId);
    closeDetailPanel();
    await refocus(null);
  });
}

function setupMoteJump() {
  const select = document.getElementById("jump-select");
  select.addEventListener("change", async () => {
    if (!select.value) return;
    const moteId = select.value;
    await refocus(moteId);
    await openDetailPanel(moteId);
  });
}

async function refreshGardenSelect() {
  const select = document.getElementById("garden-select");
  const gardens = await call(api.list_gardens());
  select.innerHTML = "";
  for (const g of gardens) {
    const opt = document.createElement("option");
    opt.value = g;
    opt.textContent = g;
    select.appendChild(opt);
  }
  select.value = currentGarden;
}

function setupGardenSwitcher() {
  const select = document.getElementById("garden-select");
  select.addEventListener("change", async () => {
    await call(api.switch_garden(select.value));
    await loadGarden(select.value);
  });

  document.getElementById("new-garden-btn").addEventListener("click", async () => {
    const name = window.prompt("New garden name:");
    if (!name) return;
    await call(api.create_garden(name));
    await call(api.switch_garden(name));
    await refreshGardenSelect();
    await loadGarden(name);
  });
}

function setupCytoscape() {
  // Cytoscape renders on <canvas>, not real DOM -- its style parser can't
  // resolve CSS custom properties, so the current theme's colors have to
  // be read out as concrete values before being handed to it.
  const rootStyle = getComputedStyle(document.documentElement);
  const fgColor = rootStyle.getPropertyValue("--fg").trim();
  const mutedColor = rootStyle.getPropertyValue("--muted").trim();
  const bgColor = rootStyle.getPropertyValue("--bg").trim();

  cy = cytoscape({
    container: document.getElementById("cy"),
    maxZoom: 2,
    minZoom: 0.1,
    style: [
      {
        selector: "node",
        style: {
          "background-color": "#3462eb",
          label: "data(label)",
          color: fgColor,
          "font-size": 9,
          "text-valign": "bottom",
          "text-margin-y": 4,
          "text-background-color": bgColor,
          "text-background-opacity": 0.85,
          "text-background-padding": 2,
          width: 18,
          height: 18,
        },
      },
      {
        selector: "node.focused",
        style: {
          "border-width": 3,
          "border-color": "#6d8dff",
        },
      },
      {
        selector: "edge",
        style: {
          width: 1.5,
          "line-color": "#9a9a9a",
          "target-arrow-color": "#9a9a9a",
          "target-arrow-shape": "triangle",
          "curve-style": "bezier",
          label: "data(label)",
          "font-size": 8,
          color: mutedColor,
          "text-background-color": bgColor,
          "text-background-opacity": 0.85,
          "text-background-padding": 2,
        },
      },
    ],
  });

  cy.on("tap", "node", (evt) => {
    const nodeId = evt.target.id();
    if (connectSourceId !== null) {
      const source = connectSourceId;
      connectSourceId = null;
      if (nodeId === source) {
        showToast("Connection cancelled.");
        return;
      }
      connectNodes(source, nodeId);
      return;
    }
    refocus(nodeId).then(() => openDetailPanel(nodeId));
  });

  cy.on("tap", (evt) => {
    if (evt.target === cy) {
      connectSourceId = null;
    }
  });

  cy.on("dragfree", "node", (evt) => {
    const p = evt.target.position();
    const positions = loadPositions(currentGarden);
    positions[evt.target.id()] = { x: p.x, y: p.y };
    localStorage.setItem(positionsKey(currentGarden), JSON.stringify(positions));
  });
}

async function init() {
  api = window.pywebview.api;
  setupCytoscape();
  setupQuickAdd();
  setupDetailPanel();
  setupGardenSwitcher();
  setupMoteJump();

  currentGarden = await call(api.current_garden());
  await refreshGardenSelect();
  await loadGarden(currentGarden);
}

window.addEventListener("pywebviewready", init);
