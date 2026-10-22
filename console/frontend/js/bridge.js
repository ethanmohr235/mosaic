/* The one way into Python. pywebview injects `window.pywebview.api` and
 * fires `pywebviewready` once it has; everything in the shell waits on
 * `ready()` and then talks to the root Api (console/api.py) through the
 * object it resolves to. Later app namespaces are reached the same way
 * (`api.monolith.table(...)`) -- pywebview 6.2 exposes nested objects.
 *
 * The methods are the ones in the U1 bridge contract. They're listed
 * here so a typo is a loud error at the call site rather than an
 * `undefined is not a function` from deep inside a promise chain. */

const ROOT_METHODS = [
  "boot", "run", "resolve", "complete", "history",
  "layout_put", "layout_get", "layout_delete", "layout_set_active",
  "working_put", "undo", "redo", "refresh", "switch_garden", "shutdown", "sync_status",
];

function pywebviewApi() {
  return window.pywebview && window.pywebview.api;
}

function hasRoot(raw) {
  return !!raw && typeof raw.boot === "function";
}

function waitForApi() {
  return new Promise((resolve) => {
    if (hasRoot(pywebviewApi())) {
      resolve(pywebviewApi());
      return;
    }
    const onReady = () => {
      if (!hasRoot(pywebviewApi())) return;
      window.removeEventListener("pywebviewready", onReady);
      resolve(pywebviewApi());
    };
    window.addEventListener("pywebviewready", onReady);
  });
}

function wrap(raw) {
  const api = {};
  for (const name of ROOT_METHODS) {
    api[name] = (...args) => {
      if (typeof raw[name] !== "function") {
        return Promise.reject(new Error(`the console api has no ${name}()`));
      }
      return raw[name](...args);
    };
  }
  // Namespaces added by later steps (monolith, motion, ...) pass through
  // untouched; only the root's own methods are pinned down here.
  return new Proxy(api, {
    get: (target, key) => (key in target ? target[key] : raw[key]),
  });
}

let readyPromise = null;

/** Resolves to the wrapped root api once pywebview has injected it. */
export function ready() {
  if (!readyPromise) readyPromise = waitForApi().then(wrap);
  return readyPromise;
}
