// Small progressive enhancements shared by every page.
(function () {
  "use strict";

  document.querySelectorAll("[data-theme-toggle]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var dark = !document.documentElement.classList.contains("dark");
      document.documentElement.classList.toggle("dark", dark);
      try { localStorage.setItem("iris-theme", dark ? "dark" : "light"); } catch (e) {}
      document.dispatchEvent(new CustomEvent("iris:theme", { detail: { dark: dark } }));
    });
  });

  // auto-submit filter forms when a select changes
  document.querySelectorAll("form[data-autosubmit] select").forEach(function (el) {
    el.addEventListener("change", function () { el.form.requestSubmit(); });
  });

  // copy-to-clipboard buttons
  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var text = btn.getAttribute("data-copy");
      if (!navigator.clipboard) return;
      navigator.clipboard.writeText(text).then(function () {
        var original = btn.textContent;
        btn.textContent = "✓";
        setTimeout(function () { btn.textContent = original; }, 1200);
      });
    });
  });
})();
