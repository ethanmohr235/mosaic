// Minimal standalone reimplementation of the two lodash functions
// cytoscape-edgehandles needs (memoize, throttle -- both called with plain
// default-behavior signatures, no custom resolver/options), so the full
// lodash library doesn't need to be vendored just to satisfy them.
window._ = window._ || {};

window._.memoize = function (fn) {
  const cache = new Map();
  const memoized = function (arg) {
    if (!cache.has(arg)) {
      cache.set(arg, fn.apply(this, arguments));
    }
    return cache.get(arg);
  };
  memoized.cache = cache;
  return memoized;
};

window._.throttle = function (fn, wait) {
  let lastCall = 0;
  let timeout = null;
  let lastArgs = null;
  let lastThis = null;

  function invoke() {
    lastCall = Date.now();
    timeout = null;
    fn.apply(lastThis, lastArgs);
  }

  return function throttled(...args) {
    lastArgs = args;
    lastThis = this;
    const remaining = wait - (Date.now() - lastCall);
    if (remaining <= 0) {
      if (timeout) {
        clearTimeout(timeout);
        timeout = null;
      }
      invoke();
    } else if (!timeout) {
      timeout = setTimeout(invoke, remaining);
    }
  };
};
