/* Command deck helpers. Press "/" anywhere (outside a field) to jump to the search box. */
(function () {
  // Hide / show the menu column on desktop. The choice is remembered; theme.js restores it before paint.
  var root = document.documentElement, KEY = "nit-rail";
  function syncRail() {
    var hidden = root.classList.contains("rail-hidden"), btn = document.getElementById("railToggle");
    if (!btn) return;
    btn.setAttribute("aria-expanded", hidden ? "false" : "true");
    btn.setAttribute("aria-label", hidden ? "Show menu" : "Hide menu");
    btn.setAttribute("title", hidden ? "Show menu" : "Hide menu");
  }
  function toggleRail() {
    var hidden = root.classList.toggle("rail-hidden");
    try { localStorage.setItem(KEY, hidden ? "hidden" : "shown"); } catch (e) { /* private mode: lasts for this page */ }
    syncRail();
  }
  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("railToggle");
    if (btn) btn.addEventListener("click", toggleRail);
    syncRail();
  });
  document.addEventListener("keydown", function (e) {       // "[" toggles the menu, outside form fields
    if (e.key !== "[" || e.ctrlKey || e.metaKey || e.altKey) return;
    var t = e.target, tag = t && t.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (t && t.isContentEditable)) return;
    if (window.matchMedia("(min-width: 992px)").matches) toggleRail();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
    var t = e.target, tag = t && t.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (t && t.isContentEditable)) return;
    var box = document.querySelector(".nav-search input");
    if (!box) return;
    e.preventDefault();
    box.focus();
    box.select();
  });
})();
