/* Command deck helpers. Press "/" anywhere (outside a field) to jump to the search box. */
(function () {
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
