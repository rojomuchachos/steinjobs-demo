// Executes the dashboard's inline script top-to-bottom with a stubbed DOM.
// Catches the TDZ class of bug (const declared after a top-level caller —
// `cap`, then `srcDisp`) that node --check can't see: the script parses fine
// and then dies at runtime, taking every button on the page down with it.
// Prints "script ran clean" and exits before any async continuation runs.
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const handler = {
  get: (t, k) => { if (k === Symbol.toPrimitive) return () => ""; return elProxy; },
  apply: () => elProxy, set: () => true, construct: () => elProxy,
};
const elProxy = new Proxy(function () {}, handler);
global.document = elProxy; global.window = global;
global.localStorage = { getItem: () => null, setItem: () => {} };
global.location = { reload: () => {}, protocol: "http:", href: "" };
global.navigator = {};
global.matchMedia = () => ({ matches: false, addEventListener: () => {} });
global.innerHeight = 800; global.requestAnimationFrame = () => {};
global.CSS = { escape: (x) => x }; global.addEventListener = () => {};
global.Option = function (t, v) { this.text = t; this.value = v; };
global.AudioContext = function () { return elProxy; };
global.IntersectionObserver = function () { return { observe: () => {}, unobserve: () => {} }; };
try {
  new Function(src)();
  console.log("script ran clean");
  process.exit(0);           // before async continuations hit stub edges
} catch (e) {
  console.log("THROW: " + e.message);
  console.log((e.stack || "").split("\n").slice(0, 4).join("\n"));
  process.exit(1);
}
