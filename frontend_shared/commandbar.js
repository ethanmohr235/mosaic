/* The "/" command bar -- one line, every window, no mouse required.
 *
 * `R1.resistance = 500 ohms` sets a property (creating R1, and the
 * property, if either is new). `R1.resistance units kohm` annotates an
 * existing (or not-yet-set) property with a unit without touching its
 * value. `R1.voltage == R1.current * R1.resistance`
 * writes a rule. `R1.resistance ?` toggles resistance as a freedom of R1
 * (see core/rules.py) -- typing it again un-frees it. `R1 -> R2` and
 * `R1 -> owns -> R2` wire a relationship. `R1 : Resistor` sets a species.
 * `delete R1.resistance` / `delete R1` remove a property or a mote. A
 * bare `R1` jumps the inspector to it. Tab completes the mote name (or,
 * right after `Mote.`, that mote's property names) the cursor is
 * sitting at the end of; repeated Tabs cycle through the rest. The
 * grammar itself is
 * core/command_bar.py; this is only the surface -- the input box,
 * history, and what an ambiguous name or a pending delete look like
 * while you're typing.
 *
 * Docked at the bottom with no backdrop, so the graph underneath stays
 * visible while you type. It stays open across Enter -- a command clears
 * the input and keeps focus rather than closing, since one edit is
 * rarely the only one you came to make -- and the last few submitted
 * commands hang above the box as a fading trail, oldest faintest, so
 * you can see what you've already done without the bar getting in the
 * way. Escape is the one way out; there's no backdrop left to click.
 *
 * Host apps drive it with two calls, same shape as Inspector's:
 *
 *   CommandBar.install({ api, onChanged, onNavigate })   once, after the bridge is up
 *   CommandBar.open()                                     also bound to "/" globally
 *
 * onNavigate(moteId) is how a command that resolved or created a mote
 * hands off to whatever this window uses to show it -- normally
 * `Inspector.open`, but a caller is free to do its own thing (Monarch
 * might also want to pan the graph there).
 *
 * Kept in frontend_shared/ and symlinked into each app's frontend/, like
 * inspector.js and menu.js; see docs/architecture.md. */

const CommandBar = (() => {
  "use strict";

  const HISTORY_KEY = "commandbar.history";
  const HISTORY_MAX = 50;
  const TRAIL_LINES = 3; // how many past commands fade in above the box

  const EXAMPLES = [
    "R1.resistance = 500 ohms",
    "R1.resistance units kohm",
    "R1.voltage == R1.current * R1.resistance",
    "R1.resistance ?",
    "R1 -> R2",
    "R1 -> owns -> R2",
    "R1 : Resistor",
    "delete R1.resistance",
  ];

  let api = null;
  let onChanged = () => {};
  let onNavigate = () => {};

  let root, input, highlight, message, candidates, hint, trail;
  let history = [];
  let historyIndex = -1;
  let pendingConfirm = null; // the exact text a second Enter would confirm-delete
  let clearMessageTimer = null;
  let tabState = null; // { suggestions, index, tokenStart, tail, resultValue, resultCursor } -- see handleTab

  function install(options) {
    api = options.api;
    onChanged = options.onChanged || (() => {});
    onNavigate = options.onNavigate || (() => {});
    history = loadHistory();
    build();

    document.addEventListener("keydown", (event) => {
      if (event.key === "/" && !isTypingElsewhere(event.target)) {
        // Already open just means visible -- a click on the canvas or a
        // panel button blurs the input without closing the bar, and "/"
        // typed at that point should return you to it rather than being
        // silently swallowed (nothing else has focus to type it into).
        if (isOpen()) {
          if (document.activeElement !== input) {
            event.preventDefault();
            input.focus();
          }
        } else {
          event.preventDefault();
          open();
        }
      } else if (event.key === "Escape" && isOpen()) {
        event.preventDefault();
        close();
      }
    });
  }

  function isTypingElsewhere(target) {
    if (!(target instanceof Node)) return false;
    const tag = (target.tagName || "").toLowerCase();
    return tag === "input" || tag === "textarea" || tag === "select"
      || (target.isContentEditable === true);
  }

  const isOpen = () => !!root && !root.classList.contains("hidden");

  function open() {
    root.classList.remove("hidden");
    setValue("");
    historyIndex = history.length;
    pendingConfirm = null;
    setMessage("");
    setCandidates(null);
    renderTrail();
    hint.textContent = EXAMPLES[Math.floor(Math.random() * EXAMPLES.length)];
    setTimeout(() => input.focus(), 0);
  }

  function close() {
    root.classList.add("hidden");
    pendingConfirm = null;
    if (clearMessageTimer) { clearTimeout(clearMessageTimer); clearMessageTimer = null; }
  }

  function build() {
    root = document.createElement("div");
    root.className = "cmdbar hidden";
    root.innerHTML = `
      <div class="cmdbar-history hidden"></div>
      <div class="cmdbar-box">
        <span class="cmdbar-slash">/</span>
        <div class="cmdbar-input-wrap">
          <div class="cmdbar-highlight" aria-hidden="true"></div>
          <input class="cmdbar-input" spellcheck="false" autocomplete="off"
                 placeholder="R1.resistance = 500 ohms">
        </div>
        <span class="cmdbar-hint"></span>
      </div>
      <div class="cmdbar-message"></div>
      <div class="cmdbar-candidates hidden"></div>`;
    document.body.appendChild(root);

    trail = root.querySelector(".cmdbar-history");
    input = root.querySelector(".cmdbar-input");
    highlight = root.querySelector(".cmdbar-highlight");
    message = root.querySelector(".cmdbar-message");
    candidates = root.querySelector(".cmdbar-candidates");
    hint = root.querySelector(".cmdbar-hint");

    // Docked at the bottom, the bar now sits right where a middle-drag
    // pans the graph. monarch/frontend/graph.js already guards its own
    // canvas against X11/GTK's middle-click-paste; the bar needs the same
    // guard for itself, or a middle click that starts on the box (a real
    // focused <input>, unlike the canvas) pastes the primary selection
    // into whatever's typed instead of doing nothing.
    root.addEventListener("mousedown", (event) => {
      if (event.button === 1) event.preventDefault();
    });
    input.addEventListener("keydown", onKeydown);
    input.addEventListener("input", () => {
      pendingConfirm = null;
      tabState = null;
      setCandidates(null);
      if (clearMessageTimer) { clearTimeout(clearMessageTimer); clearMessageTimer = null; }
      setMessage("");
      renderHighlight(input.value);
    });
    // The highlight layer has no scrollbar of its own -- it just tracks
    // the real input's, so a command too long to fit keeps its colored
    // text under the visible, scrolled portion instead of the part
    // that's since scrolled out of view.
    input.addEventListener("scroll", syncHighlightScroll);
  }

  /** The last few submitted commands, oldest faintest, directly above the
   *  box -- reused from the same `history` array arrow-key recall reads,
   *  so nothing extra is tracked just for display. */
  function renderTrail() {
    const shown = history.slice(-TRAIL_LINES);
    if (!shown.length) {
      trail.classList.add("hidden");
      trail.innerHTML = "";
      return;
    }
    trail.classList.remove("hidden");
    trail.innerHTML = "";
    shown.forEach((text, i) => {
      const line = document.createElement("div");
      line.className = "cmdbar-history-line";
      // Oldest shown line is faintest, newest is closest to full opacity.
      line.style.opacity = 0.15 + (0.4 * (i + 1)) / shown.length;
      line.textContent = text;
      trail.appendChild(line);
    });
  }

  function onKeydown(event) {
    if (event.key === "Tab") {
      event.preventDefault();
      handleTab();
    } else if (event.key === "Enter") {
      event.preventDefault();
      tabState = null;
      submit();
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      tabState = null;
      recall(-1);
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      tabState = null;
      recall(1);
    }
  }

  function recall(direction) {
    if (!history.length) return;
    historyIndex = Math.max(0, Math.min(history.length, historyIndex + direction));
    setValue(historyIndex === history.length ? "" : history[historyIndex]);
    pendingConfirm = null;
  }

  /** Sets the input's value and repaints the color layer underneath it
   *  in one place -- every spot that changes `input.value` from script
   *  (as opposed to the user typing, which the "input" listener already
   *  covers) goes through here so the highlight never has a chance to
   *  show stale text. */
  function setValue(text) {
    input.value = text;
    renderHighlight(text);
  }

  function syncHighlightScroll() {
    highlight.scrollLeft = input.scrollLeft;
  }

  /** Tab-completes the mote or property name the cursor is sitting at
   *  the end of, via core/command_bar.py's `suggest` (api.command_suggest).
   *  A first Tab fills in the top match; repeated Tabs (as long as
   *  nothing else has been typed in between -- `input.value` still
   *  matches what the last completion produced) cycle through the rest,
   *  wrapping back to the top. Setting `input.value` from script doesn't
   *  fire an "input" event, so this doesn't fight the listener above
   *  that clears `tabState` on real typing. */
  async function handleTab() {
    const value = input.value;
    const cursor = input.selectionStart == null ? value.length : input.selectionStart;

    if (tabState && value === tabState.resultValue && cursor === tabState.resultCursor) {
      tabState.index = (tabState.index + 1) % tabState.suggestions.length;
      applySuggestion();
      return;
    }

    let suggestions;
    try {
      suggestions = await api.command_suggest(value, cursor);
    } catch {
      return;
    }
    if (!suggestions || !suggestions.length) return;
    // The input may have moved on while we were awaiting the bridge.
    if (input.value !== value || input.selectionStart !== cursor) return;

    const head = value.slice(0, cursor);
    const propMatch = /([A-Za-z_]\w*)\.(\w*)$/.exec(head);
    const nameMatch = !propMatch && /([A-Za-z_]\w*)$/.exec(head);
    const partial = propMatch ? propMatch[2] : nameMatch ? nameMatch[1] : null;
    if (partial === null) return;

    tabState = {
      suggestions,
      index: 0,
      tokenStart: cursor - partial.length,
      tail: value.slice(cursor),
    };
    applySuggestion();
  }

  function applySuggestion() {
    const s = tabState.suggestions[tabState.index];
    const before = input.value.slice(0, tabState.tokenStart);
    const newValue = before + s + tabState.tail;
    const newCursor = before.length + s.length;
    setValue(newValue);
    input.setSelectionRange(newCursor, newCursor);
    tabState.resultValue = newValue;
    tabState.resultCursor = newCursor;
    if (tabState.suggestions.length > 1) {
      setMessage(`${tabState.index + 1}/${tabState.suggestions.length} -- Tab for next`, "hint");
    } else {
      setMessage("");
    }
  }

  async function submit() {
    const text = input.value.trim();
    if (!text) return;

    const confirm = pendingConfirm === text;
    pendingConfirm = null;
    let result;
    try {
      result = await api.command_run(text, confirm);
    } catch (err) {
      setMessage(String((err && err.message) || err), "error");
      return;
    }

    if (result.needsConfirm) {
      pendingConfirm = text;
      setMessage(result.message, "confirm");
      return;
    }
    if (!result.ok) {
      setMessage(result.message || "didn't understand that", "error");
      setCandidates(result.choices, text);
      return;
    }

    remember(text);
    renderTrail();
    // The bar stays open -- a command is rarely the only edit you came
    // to make -- so the input clears and refocuses instead of closing,
    // and the confirmation just fades from the message line on its own.
    setValue("");
    setCandidates(null);
    onChanged();
    if (result.moteId) onNavigate(result.moteId);
    setMessage(result.message, "ok");
    if (clearMessageTimer) clearTimeout(clearMessageTimer);
    clearMessageTimer = setTimeout(() => setMessage(""), 2500);
  }

  function setCandidates(choices, sourceText) {
    if (!choices || !choices.length) {
      candidates.classList.add("hidden");
      candidates.innerHTML = "";
      return;
    }
    candidates.classList.remove("hidden");
    candidates.innerHTML = "";
    for (const choice of choices) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "cmdbar-candidate";
      button.textContent = choice.speciesLabel
        ? `${choice.label} — ${choice.speciesLabel}`
        : choice.label;
      button.addEventListener("click", () => {
        setValue(resolveWithId(sourceText, choice.id));
        setCandidates(null);
        input.focus();
      });
      candidates.appendChild(button);
    }
  }

  /** Swap the ambiguous name for `#<id>` so resubmitting picks the
   *  chosen mote instead of asking again -- core/command_bar.py treats
   *  `#<id>` as a direct reference, bypassing the name search entirely.
   *  Only the first occurrence: a command names the ambiguous mote once. */
  function resolveWithId(text, id) {
    const match = /^([^.=:]+?)[.=:]/.exec(text) || /^(\S+)/.exec(text);
    if (!match) return text;
    const token = match[1].trim();
    const index = text.indexOf(token);
    if (index === -1) return text;
    return text.slice(0, index) + `#${id}` + text.slice(index + token.length);
  }

  function setMessage(text, kind) {
    message.textContent = text || "";
    message.className = "cmdbar-message" + (kind ? ` ${kind}` : "");
  }

  function remember(text) {
    history = history.filter((line) => line !== text);
    history.push(text);
    if (history.length > HISTORY_MAX) history.shift();
    historyIndex = history.length;
    try { localStorage.setItem(HISTORY_KEY, JSON.stringify(history)); } catch { /* private window etc. */ }
  }

  function loadHistory() {
    try {
      const raw = localStorage.getItem(HISTORY_KEY);
      const parsed = raw ? JSON.parse(raw) : [];
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  }

  // ---- coloring: a light-touch mirror of core/command_bar.py's own
  // dispatch order, so the line lights up the way it's about to be
  // parsed rather than guessing at a grammar of its own. Punctuation
  // (., =, ==, ->, :, ?) is left uncolored on purpose -- see the CSS
  // comment above .cmdbar-tok-kw. A line that doesn't (yet) match any
  // form -- still being typed, or just wrong -- falls through to
  // `tokenizeTarget`, which is the same "is it Mote.prop or a bare
  // name" split the navigate/delete forms use, so partial input still
  // gets some color instead of looking broken.

  const _ID = "[A-Za-z_]\\w*";

  function tokenize(text) {
    if (!text) return [];

    const del = /^(\s*)(delete|del)\b(.*)$/i.exec(text);
    if (del) {
      const [, lead, kw, rest] = del;
      return [{ t: lead }, { t: kw, c: "kw" }, ...tokenizeTarget(rest)];
    }

    if (text.includes("?")) {
      const m = new RegExp(`^(\\s*)(.+?)\\.(${_ID})(\\s*\\?\\s*)$`).exec(text);
      if (m) {
        const [, lead, mote, prop, tail] = m;
        return [{ t: lead }, { t: mote, c: "mote" }, { t: "." }, { t: prop, c: "prop" }, { t: tail }];
      }
    }

    if (/\bunits\b/i.test(text)) {
      const m = new RegExp(`^(\\s*)(.+?)\\.(${_ID})(\\s+units\\s+)(.+?)\\s*$`, "i").exec(text);
      if (m) {
        const [, lead, mote, prop, kw, unit] = m;
        return [
          { t: lead }, { t: mote, c: "mote" }, { t: "." }, { t: prop, c: "prop" },
          { t: kw, c: "kw" }, { t: unit, c: "unit" },
        ];
      }
    }

    if (text.includes("==")) {
      const m = new RegExp(`^(\\s*)(${_ID})\\.(${_ID})(\\s*==\\s*)(.*)$`).exec(text);
      if (m) {
        const [, lead, mote, prop, op, rhs] = m;
        return [
          { t: lead }, { t: mote, c: "mote" }, { t: "." }, { t: prop, c: "prop" },
          { t: op }, { t: rhs, c: "expr" },
        ];
      }
    }

    if (text.includes("=")) {
      const m = new RegExp(`^(\\s*)(.+?)\\.(${_ID})(\\s*=\\s*)(.*)$`).exec(text);
      if (m) {
        const [, lead, mote, prop, op, value] = m;
        return [
          { t: lead }, { t: mote, c: "mote" }, { t: "." }, { t: prop, c: "prop" },
          { t: op }, ...tokenizeValue(value),
        ];
      }
    }

    if (text.includes("->")) {
      const typed = new RegExp(`^(\\s*)(.+?)(\\s*->\\s*)(${_ID})(\\s*->\\s*)(.*)$`).exec(text);
      if (typed) {
        const [, lead, from, arrow1, type, arrow2, to] = typed;
        return [
          { t: lead }, ...tokenizeMoteRef(from),
          { t: arrow1 }, { t: type, c: "relType" }, { t: arrow2 },
          ...tokenizeMoteRef(to),
        ];
      }
      const m = /^(\s*)(.+?)(\s*->\s*)(.*)$/.exec(text);
      if (m) {
        const [, lead, from, arrow, to] = m;
        return [{ t: lead }, ...tokenizeMoteRef(from), { t: arrow }, ...tokenizeMoteRef(to)];
      }
    }

    if (text.includes(":")) {
      const m = /^(\s*)(.+?)(\s*:\s*)(.*)$/.exec(text);
      if (m) {
        const [, lead, mote, colon, species] = m;
        return [{ t: lead }, ...tokenizeMoteRef(mote), { t: colon }, { t: species, c: "species" }];
      }
    }

    return tokenizeTarget(text);
  }

  /** The rest of a `delete ...` or a bare navigate line: `Mote.prop` or
   *  just `Mote`. Shared because both forms resolve a target the same
   *  way once the leading "delete" (if any) is stripped. */
  function tokenizeTarget(text) {
    const m = /^(\s*)(.*)$/.exec(text);
    const [, lead, rest] = m;
    if (!rest) return [{ t: lead }];
    const dot = new RegExp(`^(.+?)\\.(${_ID})$`).exec(rest);
    if (dot) return [{ t: lead }, { t: dot[1], c: "mote" }, { t: "." }, { t: dot[2], c: "prop" }];
    return [{ t: lead }, ...tokenizeMoteRef(rest)];
  }

  /** One relationship/species endpoint: `#<id>` gets the same faint,
   *  italic treatment core/command_bar.py's `_ID_REF_RE` gives it --
   *  already resolved, not a name being searched -- anything else is an
   *  ordinary mote-name reference. */
  function tokenizeMoteRef(token) {
    return [{ t: token, c: /^#/.test(token.trim()) ? "id" : "mote" }];
  }

  /** A property value: `500 ohms` splits into a number and a unit
   *  (core/command_bar.py's `_parse_value`/`_resolve_unit` do the real
   *  resolution -- this is just the same "number, then a trailing word"
   *  shape, not a unit-table lookup) so the two read as distinct rather
   *  than one undifferentiated blob. */
  function tokenizeValue(value) {
    const withUnit = /^(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)(\s+)([A-Za-z]+)$/.exec(value);
    if (withUnit) {
      const [, num, sp, unit] = withUnit;
      return [{ t: num, c: "num" }, { t: sp }, { t: unit, c: "unit" }];
    }
    if (/^-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$/.test(value)) return [{ t: value, c: "num" }];
    return [{ t: value }];
  }

  function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function renderHighlight(text) {
    highlight.innerHTML = tokenize(text).map((tok) => {
      if (!tok.t) return "";
      const cls = tok.c ? ` class="cmdbar-tok-${tok.c}"` : "";
      return `<span${cls}>${escapeHtml(tok.t)}</span>`;
    }).join("");
  }

  return { install, open, close, isOpen };
})();
