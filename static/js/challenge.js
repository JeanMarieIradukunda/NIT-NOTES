/* IT Knowledge Challenge gallery: topic chips and the tap-to-enlarge viewer with Previous / Next.
   Everything also works without this script: chips are links and every card has its own page. */
(function () {
  var grid = document.getElementById("kcGrid"); if (!grid) return;
  var viewer = document.getElementById("kcViewer"), img = document.getElementById("kcImg"), cap = document.getElementById("kcCap"),
      count = document.getElementById("kcCount"), live = document.getElementById("kcLive"), openLink = document.getElementById("kcOpen"),
      download = document.getElementById("kcDownload"), closeBtn = document.getElementById("kcClose"),
      prev = document.getElementById("kcPrev"), next = document.getElementById("kcNext"), shown = document.getElementById("kcShown"),
      chips = [].slice.call(document.querySelectorAll("#kcChips .kc-chip")), items = [].slice.call(grid.querySelectorAll(".kc-item"));
  var list = [], current = 0, lastFocus = null;

  function visibleTiles() {
    return items.filter(function (li) { return !li.hidden; }).map(function (li) { return li.querySelector(".kc-tile"); });
  }

  /* ---- topic chips ---- */
  function applyFilter(topic, updateUrl) {
    items.forEach(function (li) { li.hidden = !!topic && li.dataset.topic !== topic; });
    chips.forEach(function (c) {
      var on = (c.dataset.topic || "") === (topic || "");
      c.classList.toggle("active", on);
      if (on) { c.setAttribute("aria-current", "true"); } else { c.removeAttribute("aria-current"); }
    });
    var n = visibleTiles().length;
    shown.textContent = topic ? n + " card" + (n === 1 ? "" : "s") + " in this topic" : "";
    if (updateUrl !== false && window.history && history.replaceState) {
      history.replaceState(null, "", topic ? "?topic=" + encodeURIComponent(topic) : location.pathname);
    }
  }
  chips.forEach(function (c) {
    c.addEventListener("click", function (e) { e.preventDefault(); applyFilter(c.dataset.topic || ""); });
  });
  var activeChip = document.querySelector("#kcChips .kc-chip.active");
  applyFilter(activeChip ? (activeChip.dataset.topic || "") : "", false);

  /* ---- viewer ---- */
  function preload(i) { var t = list[(i + list.length) % list.length]; if (t) { (new Image()).src = t.dataset.svg; } }

  function show(i) {
    current = (i + list.length) % list.length;
    var t = list[current];
    img.src = t.dataset.svg; img.alt = t.dataset.alt;
    cap.textContent = t.dataset.title + " · " + t.dataset.module;
    count.textContent = (current + 1) + " / " + list.length;
    openLink.href = t.dataset.page; download.href = t.dataset.download;
    live.textContent = "Card " + (current + 1) + " of " + list.length + ": " + t.dataset.title;
    prev.disabled = next.disabled = list.length < 2;
    if (window.history && history.replaceState) { history.replaceState(null, "", location.pathname + location.search + "#card-" + t.dataset.id); }
    preload(current + 1); preload(current - 1);
  }
  function open(tile) {
    list = visibleTiles(); lastFocus = tile;
    viewer.hidden = false; document.body.classList.add("kc-lock");
    show(Math.max(list.indexOf(tile), 0)); closeBtn.focus();
  }
  function close() {
    if (viewer.hidden) return;
    viewer.hidden = true; document.body.classList.remove("kc-lock");
    if (window.history && history.replaceState) { history.replaceState(null, "", location.pathname + location.search); }
    if (lastFocus) { lastFocus.focus(); }
  }

  grid.addEventListener("click", function (e) {
    var tile = e.target.closest(".kc-tile");
    if (!tile || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || e.button === 1) return;   // let "open in new tab" work
    e.preventDefault(); open(tile);
  });
  prev.addEventListener("click", function () { show(current - 1); });
  next.addEventListener("click", function () { show(current + 1); });
  closeBtn.addEventListener("click", close);
  viewer.addEventListener("click", function (e) { if (e.target === viewer || e.target.classList.contains("kc-stage") || e.target.classList.contains("kc-fig")) close(); });

  document.addEventListener("keydown", function (e) {
    if (viewer.hidden) return;
    if (e.key === "Escape") { close(); }
    else if (e.key === "ArrowLeft") { show(current - 1); }
    else if (e.key === "ArrowRight") { show(current + 1); }
    else if (e.key === "Tab") {                                   // keep keyboard focus inside the viewer
      var f = [closeBtn, download, openLink, prev, next].filter(function (x) { return x.offsetParent !== null && !x.disabled; });
      if (!f.length) return;
      var first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });

  var x0 = null, y0 = null;                                       // swipe left / right on phones
  viewer.addEventListener("touchstart", function (e) { var t = e.changedTouches[0]; x0 = t.clientX; y0 = t.clientY; }, { passive: true });
  viewer.addEventListener("touchend", function (e) {
    if (x0 === null) return;
    var t = e.changedTouches[0], dx = t.clientX - x0, dy = t.clientY - y0; x0 = null;
    if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.5) { show(current + (dx < 0 ? 1 : -1)); }
  }, { passive: true });

  /* ---- a shared link such as /challenge/#card-12 opens that card ---- */
  var m = /^#card-(\d+)$/.exec(location.hash);
  if (m) {
    var tile = document.getElementById("card-" + m[1]);
    if (tile) { if (tile.closest(".kc-item").hidden) { applyFilter("", false); } open(tile); }
  }
})();
