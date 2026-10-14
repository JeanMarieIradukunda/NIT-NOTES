/* Light / dark theme switch. Loaded in <head> so the saved choice is applied before the page paints
   (no flash). The default is dark, as before; the choice is remembered in this browser only. */
(function () {
  var KEY = "nit-theme", root = document.documentElement;

  function saved() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
  function remember(t) { try { localStorage.setItem(KEY, t); } catch (e) { /* private mode: still works for this page */ } }

  function apply(theme) {
    root.setAttribute("data-bs-theme", theme);
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", theme === "light" ? "#f4f6fb" : "#0a101d");
    var btn = document.getElementById("themeToggle");
    if (btn) {
      btn.setAttribute("aria-pressed", theme === "light" ? "true" : "false");
      btn.setAttribute("aria-label", theme === "light" ? "Switch to dark theme" : "Switch to light theme");
      btn.setAttribute("title", theme === "light" ? "Switch to dark theme" : "Switch to light theme");
    }
  }

  apply(saved() === "light" ? "light" : "dark");

  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("themeToggle");
    if (!btn) return;
    apply(root.getAttribute("data-bs-theme"));            // sync the button's label with the theme
    btn.addEventListener("click", function () {
      var next = root.getAttribute("data-bs-theme") === "light" ? "dark" : "light";
      remember(next);
      apply(next);
    });
  });
})();
