/* Upload many memes: the files go to the server one at a time (a single request over ~4.5 MB is refused by the host). */
(function () {
  var root = document.getElementById("kcBulk"); if (!root) return;
  var url = root.dataset.url, max = parseInt(root.dataset.max, 10), maxText = root.dataset.maxText;
  var csrf = root.querySelector("[name=csrfmiddlewaretoken]").value;
  var input = document.getElementById("kcFiles"), drop = document.getElementById("kcDrop"), list = document.getElementById("kcList");
  var start = document.getElementById("kcStart"), summary = document.getElementById("kcSummary");
  var items = [], running = false;
  var okType = /\.(png|jpe?g|webp|svg)$/i;

  function mb(n) { return (n / 1048576).toFixed(1) + " MB"; }
  function add(files) {
    Array.prototype.forEach.call(files, function (f) {
      var li = document.createElement("li"), img = document.createElement("img"), mid = document.createElement("div"), st = document.createElement("span");
      img.alt = ""; if (/^image\//.test(f.type)) { img.src = URL.createObjectURL(f); }
      mid.innerHTML = '<div class="nm"></div><div class="msg"></div>'; mid.firstChild.textContent = f.name; st.className = "st"; st.textContent = "Waiting";
      li.appendChild(img); li.appendChild(mid); li.appendChild(st); list.appendChild(li);
      var it = { file: f, li: li, st: st, msg: mid.lastChild, state: "wait" };
      if (!okType.test(f.name)) { fail(it, "Not a PNG, JPG, WebP or SVG picture."); }
      else if (f.size > max) { fail(it, "This picture is " + mb(f.size) + ". Please keep it under " + maxText + " (tinypng.com can shrink it)."); }
      items.push(it);
    });
    refresh();
  }
  function fail(it, text) { it.state = "bad"; it.li.className = "bad"; it.st.textContent = "Skipped"; it.msg.textContent = text; }
  function pending() { return items.filter(function (i) { return i.state === "wait"; }); }
  function refresh() {
    var n = pending().length; start.disabled = running || n === 0;
    start.innerHTML = '<i class="bi bi-upload me-1"></i>Upload ' + (n ? n + " picture" + (n === 1 ? "" : "s") : "");
  }
  input.addEventListener("change", function () { add(input.files); input.value = ""; });
  ["dragenter", "dragover"].forEach(function (e) { drop.addEventListener(e, function (ev) { ev.preventDefault(); drop.classList.add("is-over"); }); });
  ["dragleave", "drop"].forEach(function (e) { drop.addEventListener(e, function (ev) { ev.preventDefault(); drop.classList.remove("is-over"); }); });
  drop.addEventListener("drop", function (ev) { add(ev.dataTransfer.files); });

  async function send(it) {
    it.state = "run"; it.li.className = "run"; it.st.textContent = "Uploading…";
    var body = new FormData();
    body.append("module", document.getElementById("kcModule").value);
    body.append("publish", document.getElementById("kcPublish").checked ? "1" : "");
    body.append("file", it.file);
    try {
      var r = await fetch(url, { method: "POST", body: body, headers: { "X-CSRFToken": csrf, "Accept": "application/json" } });
      var d = {}; try { d = await r.json(); } catch (e) { d = { error: r.status === 413 ? "The file was too large for the server." : "The server could not read the file." }; }
      if (r.ok && d.ok) { it.state = "ok"; it.li.className = "ok"; it.st.textContent = "Added"; it.msg.innerHTML = '<a href="' + d.edit + '">Edit title and text</a>'; }
      else { fail(it, d.error || "Could not add this picture."); if (r.status === 401) { it.msg.textContent = d.error; return false; } }
    } catch (e) { it.state = "wait"; it.li.className = ""; it.st.textContent = "Waiting"; it.msg.textContent = "Could not reach the server. Press Upload to try again."; return false; }
    return true;
  }

  start.addEventListener("click", async function () {
    if (running) return; running = true; refresh();
    var queue = pending(), done = 0;
    for (var i = 0; i < queue.length; i++) {
      summary.textContent = "Uploading " + (i + 1) + " of " + queue.length + "…";
      var cont = await send(queue[i]); if (queue[i].state === "ok") done++;
      if (cont === false) break;
    }
    running = false;
    var bad = items.filter(function (x) { return x.state === "bad"; }).length, left = pending().length;
    summary.textContent = (left ? "Stopped with " + left + " still waiting. " : "Done. ") + done + " added" + (bad ? ", " + bad + " skipped." : ".");
    refresh();
  });
})();
