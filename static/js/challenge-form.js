/* Card form: character counters and a live preview of the card as the trainer types. */
(function () {
  var form = document.getElementById("kcForm"); if (!form) return;
  var img = document.getElementById("kcPreview"), note = document.getElementById("kcPreviewNote"), timer = null, lastUrl = null, seq = 0;

  [].slice.call(form.querySelectorAll("[data-count]")).forEach(function (el) {
    var out = document.createElement("span"); out.className = "kc-counter"; out.setAttribute("aria-hidden", "true");
    var label = form.querySelector('label[for="' + el.id + '"]'); if (label) { label.classList.add("kc-label"); label.appendChild(out); }
    function update() { out.textContent = el.value.length + " / " + el.dataset.count; }
    el.addEventListener("input", update); update();
  });

  function refresh() {
    var body = new FormData(form); body.delete("svg_file"); body.set("number", form.dataset.number || "0");
    var mine = ++seq;
    fetch(form.dataset.preview, { method: "POST", body: body, headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (r) { if (!r.ok) { throw new Error("preview"); } return r.blob(); })
      .then(function (blob) {
        if (mine !== seq) return;
        var url = URL.createObjectURL(blob); img.src = url;
        if (lastUrl) { URL.revokeObjectURL(lastUrl); } lastUrl = url; note.textContent = "Preview";
      })
      .catch(function () { note.textContent = "Preview is not available right now. You can still save the card."; });
  }
  function later() { clearTimeout(timer); timer = setTimeout(refresh, 350); }
  form.addEventListener("input", later); form.addEventListener("change", later);
  refresh();
})();
