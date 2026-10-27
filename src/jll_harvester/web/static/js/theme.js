// Runs synchronously in <head> to avoid a flash of the wrong colour scheme.
(function () {
  try {
    var stored = localStorage.getItem("iris-theme");
    var dark = stored ? stored === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.classList.toggle("dark", dark);
  } catch (e) { /* storage unavailable: keep light */ }
})();
