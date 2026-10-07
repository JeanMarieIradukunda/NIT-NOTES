/* Secure Online Assessment — exam page.

   What this can and cannot do: it runs in the candidate's browser. It detects
   and reports events (leaving full screen, tab/window changes, blocked
   actions) and the SERVER decides penalties, limits, time and marks. It
   cannot lock the operating system, other programs or other devices, and
   detection depends on the browser. The page says so to the candidate.

   Page-load flow: gate (Start) -> full screen -> exam. A refresh or reopen
   loads the gate again via the password screen and costs an open.
*/
(function () {
  "use strict";

  var cfg = JSON.parse(document.getElementById("exam-config").textContent);
  var $ = function (id) { return document.getElementById(id); };

  // ---- state ---------------------------------------------------------------
  var started = false, submitted = false, locked = false, submitting = false;
  var endAtMs = cfg.endAtMs, offset = cfg.serverNowMs - Date.now();
  var answers = Object.assign({}, cfg.answers || {});
  var dirty = {};                       // {qid: value|null} not yet confirmed by the server
  var saveTimer = null, saving = false;
  var episode = 0, inEpisode = false, episodeTypes = {};
  var violationQueue = [], flushing = false;   // violations waiting for a successful POST
  var graceUntil = 0;                   // brief window after entering full screen when a stray blur is ignored
  var lastFlag = {};                    // dedupe for single-action violations
  var tickTimer = null, beatTimer = null, pollTimer = null, saveEvery = null;
  var keysLocked = false;

  // ---- storage helpers (localStorage is only a backup) -----------------------
  function lsGet(k) { try { return JSON.parse(localStorage.getItem(k) || "null"); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }
  var ansKey = "nit-ans:" + cfg.storageKey;
  try { localStorage.setItem("nit-device", cfg.deviceToken); } catch (e) {}
  lsSet("nit-open:" + cfg.storageKey, { opens: cfg.opens, epoch: cfg.epoch });
  lsSet("nit-last-candidate:" + cfg.storageKey.split(":")[0], { name: cfg.candidate.name, reg: cfg.candidate.regNo });

  // ---- network ------------------------------------------------------------
  function post(action, body, opts) {
    var payload = Object.assign({ tab: cfg.tabToken, device: cfg.deviceToken }, body || {});
    return fetch(cfg.api[action], {
      method: "POST", credentials: "same-origin", keepalive: !!(opts && opts.keepalive),
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload)
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) { return { status: r.status, data: data }; });
    });
  }

  // Returns true when the response ended or locked this page.
  function handleState(res) {
    var d = res.data || {};
    if (d.superseded) {
      lockPage("This page is no longer active",
        "The assessment was opened in another tab or window, so this one has stopped. Close it.");
      return true;
    }
    if (res.status === 403 && d.error === "device") {
      lockPage("This browser is not recognised", "Reload and enter the assessment again, or ask your trainer.");
      return true;
    }
    if (d.endAtMs) { endAtMs = d.endAtMs; offset = d.serverNowMs - Date.now(); }
    if (typeof d.violationCount === "number") updateHud(d.violationCount, d.penaltyTotal);
    if (d.submitted) { finish(d.submitReason); return true; }
    return false;
  }

  // ---- HUD ------------------------------------------------------------------
  var hud = { timer: $("hud-timer"), answered: $("hud-answered"), viol: $("hud-violations"),
              pen: $("hud-penalty"), save: $("hud-save") };
  function fmtMarks(n) { return String(Math.round(n * 100) / 100); }
  function updateHud(count, penalty) {
    hud.viol.textContent = cfg.exam.maxViolations ? count + " / " + cfg.exam.maxViolations : String(count);
    hud.viol.className = "hud-val" + (cfg.exam.maxViolations && count >= cfg.exam.maxViolations - 1 && count > 0 ? " crit" : count ? " warn" : "");
    hud.pen.textContent = "\u2212" + fmtMarks(penalty);
  }
  function answeredCount() { return Object.keys(answers).length; }
  function updateAnswered() {
    hud.answered.textContent = answeredCount() + "/" + cfg.questions.length;
    document.querySelectorAll(".q-card").forEach(function (c) {
      c.classList.toggle("answered", answers[c.dataset.qid] !== undefined);
    });
  }
  function setSaveText(text, bad) { hud.save.textContent = text; hud.save.className = "small " + (bad ? "text-danger" : "text-muted-2"); }

  function remainingMs() { return endAtMs ? endAtMs - (Date.now() + offset) : cfg.exam.durationMinutes * 60000; }
  function fmtClock(ms) {
    var s = Math.max(0, Math.ceil(ms / 1000)), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    var p = function (n) { return (n < 10 ? "0" : "") + n; };
    return (h ? h + ":" + p(m) : p(m)) + ":" + p(sec);
  }
  function tick() {
    var ms = remainingMs();
    hud.timer.textContent = fmtClock(ms);
    hud.timer.className = "hud-val" + (ms <= 60000 ? " crit" : ms <= 300000 ? " warn" : "");
    if (ms <= 0 && !submitted && !submitting) submitNow("time");
  }

  // ---- toast ------------------------------------------------------------------
  var toastEl = null, toastTimer = null;
  function toast(text) {
    if (!toastEl) {
      toastEl = document.createElement("div");
      toastEl.className = "toast-lite"; toastEl.setAttribute("role", "status"); toastEl.setAttribute("aria-live", "polite");
      document.body.appendChild(toastEl);
    }
    toastEl.textContent = text; toastEl.classList.add("show");
    clearTimeout(toastTimer); toastTimer = setTimeout(function () { toastEl.classList.remove("show"); }, 3500);
  }

  // ---- gate ---------------------------------------------------------------------
  function setupGate() {
    var min = cfg.exam.durationMinutes;
    $("gate-time").textContent = endAtMs
      ? "The clock is already running: " + fmtClock(remainingMs()) + " left. Your saved answers will be restored."
      : "You have " + min + " minutes. The clock starts when you press Start and cannot be paused.";
    $("gate-opens").textContent = "This is open " + cfg.opens + " of " + cfg.maxOpens + " allowed on this computer.";
    $("gate-limit").textContent = cfg.exam.maxViolations
      ? "At " + cfg.exam.maxViolations + " counted violations the exam is submitted automatically."
      : "";
    var ul = $("gate-penalties");
    cfg.penalties.forEach(function (p) { var li = document.createElement("li"); li.textContent = p.label + ": \u2212" + fmtMarks(p.marks); ul.appendChild(li); });
    $("btn-start").textContent = endAtMs ? "Resume in full screen" : "Start in full screen";
    $("btn-start").addEventListener("click", startExam);
  }
  function gateError(text) { var e = $("gate-error"); e.textContent = text; e.classList.remove("d-none"); }

  function isExtended() { return !!(window.screen && window.screen.isExtended); }
  function canFullscreen() { return !!(document.documentElement.requestFullscreen); }
  function enterFullscreen() {
    var el = document.documentElement;
    if (document.fullscreenElement) return Promise.resolve();
    return el.requestFullscreen({ navigationUI: "hide" }).then(function () {
      graceUntil = Date.now() + 1000;
      if (navigator.keyboard && navigator.keyboard.lock && !keysLocked) {
        keysLocked = true;
        navigator.keyboard.lock().catch(function () {});   // best effort: Esc, Alt+Tab etc. where the browser allows
      }
    });
  }

  function startExam() {
    $("gate-error").classList.add("d-none");
    if (!canFullscreen()) {
      return gateError("This browser cannot enter full screen, so the assessment cannot run here. Use a recent desktop version of Chrome, Edge or Firefox.");
    }
    if (isExtended()) {
      return gateError("An extra display is connected. Disconnect it (or switch to a single screen) and press Start again.");
    }
    $("btn-start").disabled = true;
    enterFullscreen().then(function () {
      return post("start");
    }).then(function (res) {
      if (handleState(res)) return;
      if (!res.data.ok) throw new Error("start failed");
      beginExam();
    }).catch(function () {
      $("btn-start").disabled = false;
      if (document.fullscreenElement) document.exitFullscreen().catch(function () {});
      gateError("Could not start. Full screen must be allowed and you must be online. Try again.");
    });
  }

  // ---- questions ----------------------------------------------------------------
  function el(tag, cls, text) { var n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; }

  function setAnswer(qid, value) {
    var key = String(qid);
    var empty = value === null || value === undefined || value === "" ||
      (typeof value === "string" && !value.trim()) ||
      (typeof value === "object" && value !== null && !Object.keys(value).length);
    if (empty) { delete answers[key]; dirty[key] = null; } else { answers[key] = value; dirty[key] = value; }
    lsSet(ansKey, { ts: Date.now(), answers: answers, dirty: dirty });
    updateAnswered();
    clearTimeout(saveTimer); saveTimer = setTimeout(saveNow, 1200);
    setSaveText("Unsaved changes\u2026");
  }

  function renderQuestions() {
    var root = $("questions"); root.textContent = "";
    var n = 0, current = null;
    cfg.questions.forEach(function (q) {
      if (q.section !== current) {
        current = q.section;
        var label = (cfg.sections.filter(function (s) { return s.key === q.section; })[0] || {}).label || q.section;
        root.appendChild(el("h2", "q-section", label));
      }
      n += 1;
      var card = el("article", "q-card"); card.dataset.qid = q.id;
      var text = el("div", "q-text");
      text.appendChild(el("span", "q-num", n + "."));
      var saved = answers[String(q.id)];

      if (q.section === "fill") {
        var parts = q.text.split(/_{3,}/);
        var input = el("input", "form-control q-fill-inline"); input.type = "text"; input.maxLength = 500;
        input.autocomplete = "off"; input.spellcheck = false; input.setAttribute("aria-label", "Answer for question " + n);
        input.value = typeof saved === "string" ? saved : "";
        input.addEventListener("input", function () { setAnswer(q.id, input.value); });
        text.appendChild(document.createTextNode(parts[0]));
        if (parts.length > 1) { text.appendChild(input); text.appendChild(document.createTextNode(parts.slice(1).join("____"))); card.appendChild(text); }
        else { card.appendChild(text); input.className = "form-control"; card.appendChild(input); }
      } else {
        text.appendChild(document.createTextNode(q.text)); card.appendChild(text);
        if (q.section === "mcq") {
          // The server shuffles per candidate: optionIdx[j] is the saved value for the j-th displayed option.
          var idxs = q.optionIdx || q.options.map(function (_o, k) { return k; });
          if (q.multi) card.appendChild(el("div", "small text-muted-2 mb-1", "Select all that apply."));
          q.options.forEach(function (opt, j) {
            var orig = idxs[j];
            var lab = el("label", "q-opt"), r = el("input"); r.type = q.multi ? "checkbox" : "radio"; r.name = "q" + q.id; r.value = orig;
            r.checked = q.multi ? (Array.isArray(saved) && saved.indexOf(orig) > -1) : saved === orig;
            r.addEventListener("change", function () {
              if (!q.multi) { setAnswer(q.id, orig); return; }
              var picked = [];
              card.querySelectorAll('input[type="checkbox"]').forEach(function (b) { if (b.checked) picked.push(parseInt(b.value, 10)); });
              picked.sort(function (a, b) { return a - b; });
              setAnswer(q.id, picked);
            });
            lab.appendChild(r); lab.appendChild(el("span", "", opt)); card.appendChild(lab);
          });
        } else if (q.section === "open") {
          var ta = el("textarea", "form-control"); ta.rows = 7; ta.maxLength = 20000; ta.spellcheck = false; ta.autocomplete = "off";
          ta.setAttribute("aria-label", "Answer for question " + n); ta.value = typeof saved === "string" ? saved : "";
          ta.addEventListener("input", function () { setAnswer(q.id, ta.value); });
          card.appendChild(ta);
        } else if (q.section === "match") {
          var cur = (saved && typeof saved === "object") ? Object.assign({}, saved) : {};
          q.left.forEach(function (leftText, i) {
            var row = el("div", "match-row"), sel = el("select", "form-select");
            row.appendChild(el("span", "", leftText));
            sel.setAttribute("aria-label", "Match for: " + leftText);
            var blank = el("option", "", "Choose\u2026"); blank.value = ""; sel.appendChild(blank);
            q.right.forEach(function (r) { var o = el("option", "", r.text); o.value = r.id; sel.appendChild(o); });
            sel.value = cur[String(i)] || "";
            sel.addEventListener("change", function () {
              if (sel.value) cur[String(i)] = sel.value; else delete cur[String(i)];
              setAnswer(q.id, Object.assign({}, cur));
            });
            row.appendChild(sel); card.appendChild(row);
          });
        }
      }
      root.appendChild(card);
    });
    updateAnswered();
  }

  // ---- autosave ---------------------------------------------------------------------
  function saveNow() {
    clearTimeout(saveTimer);
    var keys = Object.keys(dirty);
    if (!keys.length || saving || submitted || locked) return Promise.resolve();
    var sent = {}; keys.forEach(function (k) { sent[k] = dirty[k]; });
    saving = true;
    return post("autosave", { answers: sent }).then(function (res) {
      saving = false;
      if (handleState(res)) return;
      if (res.data.ok) {
        keys.forEach(function (k) { if (JSON.stringify(dirty[k]) === JSON.stringify(sent[k])) delete dirty[k]; });
        lsSet(ansKey, { ts: Date.now(), answers: answers, dirty: dirty });
        setSaveText("Saved " + new Date().toLocaleTimeString());
      } else { setSaveText("Could not save, retrying\u2026", true); }
    }).catch(function () { saving = false; setSaveText("Offline \u2014 will keep retrying", true); });
  }

  // ---- violations ----------------------------------------------------------------------
  function penaltyFor(type) { var p = cfg.penalties.filter(function (x) { return x.key === type; })[0]; return p ? p.marks : 0; }

  function flushViolations() {
    if (flushing || !violationQueue.length || submitted || locked) return;
    flushing = true;
    var item = violationQueue[0];
    post("violation", item).then(function (res) {
      flushing = false;
      if (res.status >= 500 || res.status === 0) return;          // keep queued, retry on next beat
      violationQueue.shift();
      if (handleState(res)) return;
      if (res.data.autoSubmitted) finish("violations");
      flushViolations();
    }).catch(function () { flushing = false; });
  }
  function report(type, detail, ep) {
    violationQueue.push({ type: type, episode: ep, clientAt: new Date().toISOString(), detail: detail || "" });
    flushViolations();
  }

  function active() { return started && !submitted && !locked; }

  // "Away" events share one episode until the candidate is fully back.
  function away(type, detail) {
    if (!active()) return;
    if (type === "window_blur" && !inEpisode && Date.now() < graceUntil) return;
    if (!inEpisode) { inEpisode = true; episode += 1; episodeTypes = {}; }
    if (!episodeTypes[type]) { episodeTypes[type] = true; report(type, detail, episode); }
    showPause();
  }
  function presenceOk() {
    return !!document.fullscreenElement && document.visibilityState === "visible" && document.hasFocus() && !isExtended();
  }
  function pauseMessage() {
    var m = [];
    if (isExtended()) m.push("Disconnect the extra display.");
    if (document.visibilityState !== "visible") m.push("Come back to this tab.");
    else if (!document.hasFocus()) m.push("Click here to bring this window back into focus.");
    if (!document.fullscreenElement) m.push("Return to full screen.");
    return m.join(" ") || "Press the button to continue.";
  }
  function showPause() {
    $("pause-text").textContent = pauseMessage();
    $("pause").classList.remove("d-none");
    $("exam-root").setAttribute("aria-hidden", "true");
    if (document.visibilityState === "visible") $("btn-resume").focus({ preventScroll: true });
  }
  function evaluatePresence() {
    if (!active() || !inEpisode) return;
    if (presenceOk()) {
      inEpisode = false;
      $("pause").classList.add("d-none");
      $("exam-root").removeAttribute("aria-hidden");
    } else { $("pause-text").textContent = pauseMessage(); }
  }

  // Single actions: blocked, recorded, penalised per the exam's settings.
  var FLAG_TEXT = {
    clipboard: "Copy, cut and paste are blocked.",
    shortcut: "That shortcut is blocked.",
    context_menu: "Right-click is blocked."
  };
  function flag(type, detail) {
    if (!active()) return;
    var now = Date.now();
    if (lastFlag[type] && now - lastFlag[type] < 400) return;
    lastFlag[type] = now;
    var marks = penaltyFor(type);
    toast(FLAG_TEXT[type] + " This was recorded" + (marks ? " (\u2212" + fmtMarks(marks) + " marks)." : "."));
    report(type, detail);
  }

  var CTRL_BLOCK = { u: 1, s: 1, p: 1, r: 1, o: 1, w: 1, t: 1, n: 1, l: 1, d: 1, h: 1, j: 1, g: 1, f: 0, b: 1, e: 1 };
  function onKeydown(e) {
    if (!active()) return;
    var k = e.key, low = k && k.length === 1 ? k.toLowerCase() : k;
    var ctrl = e.ctrlKey || e.metaKey;
    var inField = e.target && (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA");

    if (ctrl && (low === "c" || low === "x" || low === "v" || k === "Insert")) {
      e.preventDefault(); e.stopPropagation(); return flag("clipboard", "Ctrl+" + (low || k).toUpperCase());
    }
    if (e.shiftKey && k === "Insert") { e.preventDefault(); return flag("clipboard", "Shift+Insert"); }
    if (ctrl && low === "a" && !inField) { e.preventDefault(); return; }              // select-all: silent
    var blocked = k === "F12" || k === "F5" || k === "F11" || k === "F3" || k === "F6" || k === "F7" ||
      (ctrl && e.shiftKey && (low === "i" || low === "j" || low === "c" || low === "r" || low === "delete")) ||
      (ctrl && CTRL_BLOCK[low]) ||
      (ctrl && (k === "Tab" || k === "PageUp" || k === "PageDown" || k === "F4")) ||
      (e.altKey && (k === "Tab" || k === "F4" || k === "ArrowLeft" || k === "ArrowRight" || k === "Home"));
    if (blocked) {
      e.preventDefault(); e.stopPropagation();
      var combo = (e.ctrlKey ? "Ctrl+" : "") + (e.metaKey ? "Cmd+" : "") + (e.altKey ? "Alt+" : "") + (e.shiftKey ? "Shift+" : "") + k;
      flag("shortcut", combo);
    }
  }
  function onKeyup(e) {
    if (!active()) return;
    if (e.key === "PrintScreen") {
      try { navigator.clipboard && navigator.clipboard.writeText(""); } catch (x) {}   // best effort only
      flag("shortcut", "PrintScreen");
    }
  }

  function attachLockdown() {
    document.addEventListener("fullscreenchange", function () {
      if (!document.fullscreenElement) away("fullscreen_exit"); else evaluatePresence();
    });
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "hidden") away("tab_switch", "Tab hidden or window minimised"); else evaluatePresence();
    });
    window.addEventListener("blur", function () { away("window_blur"); });
    window.addEventListener("focus", evaluatePresence);
    if (window.screen && window.screen.addEventListener) {
      window.screen.addEventListener("change", function () { if (isExtended()) away("extra_display"); else evaluatePresence(); });
    }

    document.addEventListener("keydown", onKeydown, true);
    document.addEventListener("keyup", onKeyup, true);
    ["copy", "cut", "paste"].forEach(function (name) {
      document.addEventListener(name, function (e) { if (!active()) return; e.preventDefault(); flag("clipboard", name); }, true);
    });
    document.addEventListener("contextmenu", function (e) { if (!active()) return; e.preventDefault(); flag("context_menu"); }, true);
    document.addEventListener("selectstart", function (e) {
      var t = e.target && e.target.nodeType === 1 ? e.target : (e.target && e.target.parentElement);
      if (!(t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA"))) e.preventDefault();
    }, true);
    ["dragstart", "drop", "dragover"].forEach(function (name) {
      document.addEventListener(name, function (e) { e.preventDefault(); }, true);
    });
    window.addEventListener("beforeprint", function () { flag("shortcut", "print"); });
    window.addEventListener("beforeunload", function (e) {
      if (submitted) return;
      e.preventDefault(); e.returnValue = "";          // browsers show their own generic warning
      return "";
    });
    window.addEventListener("pagehide", function () {
      if (submitted || !started) return;
      var body = JSON.stringify({ tab: cfg.tabToken, device: cfg.deviceToken });
      if (Object.keys(dirty).length) {
        try { navigator.sendBeacon(cfg.api.autosave, new Blob([JSON.stringify({ tab: cfg.tabToken, device: cfg.deviceToken, answers: dirty })], { type: "application/json" })); } catch (x) {}
      }
      try { navigator.sendBeacon(cfg.api.release, new Blob([body], { type: "application/json" })); } catch (x) {}
    });
  }

  // ---- begin / finish ---------------------------------------------------------------------
  function beginExam() {
    // Merge any unsaved local backup (newer than the server copy), then show the exam.
    var backup = lsGet(ansKey);
    if (backup && backup.dirty && Object.keys(backup.dirty).length) {
      Object.keys(backup.dirty).forEach(function (k) {
        var v = backup.dirty[k];
        if (v === null) delete answers[k]; else answers[k] = v;
        dirty[k] = v;
      });
    }
    renderQuestions();
    $("gate").classList.add("d-none");
    $("exam-root").hidden = false;
    started = true;
    updateHud(cfg.violationCount, cfg.penaltyTotal);
    attachLockdown();
    tick(); tickTimer = setInterval(tick, 250);
    beatTimer = setInterval(function () {
      post("heartbeat").then(handleState).catch(function () {});
      flushViolations();
    }, 8000);
    saveEvery = setInterval(saveNow, 15000);
    pollTimer = setInterval(function () {
      if (!active()) return;
      if (!inEpisode) {
        if (!document.fullscreenElement) away("fullscreen_exit");
        else if (document.visibilityState === "hidden") away("tab_switch");
        else if (isExtended()) away("extra_display");
      } else { evaluatePresence(); }
    }, 700);
    if (Object.keys(dirty).length) saveNow();
    if (isExtended()) away("extra_display");
  }

  function stopTimers() { [tickTimer, beatTimer, pollTimer, saveEvery, saveTimer].forEach(function (t) { clearInterval(t); clearTimeout(t); }); }

  function lockPage(title, text) {
    locked = true; stopTimers();
    $("exam-root").hidden = true; $("pause").classList.add("d-none"); $("confirm").classList.add("d-none"); $("gate").classList.add("d-none");
    $("locked-title").textContent = title; $("locked-text").textContent = text;
    $("locked").classList.remove("d-none");
  }

  var REASONS = {
    time: "Time ran out, so the assessment was submitted automatically.",
    violations: "The violation limit was reached, so the assessment was submitted automatically.",
    teacher: "Your trainer submitted the assessment.",
    manual: "Your assessment was submitted."
  };
  function finish(reason) {
    if (submitted) return;
    submitted = true; stopTimers();
    lockPage("Submitted", (REASONS[reason] || REASONS.manual) + " Taking you to your result\u2026");
    submitted = true; locked = true;
    setTimeout(function () {
      if (document.fullscreenElement) document.exitFullscreen().catch(function () {});
      window.location.replace(cfg.resultUrl);
    }, reason === "manual" ? 600 : 2500);
  }

  function submitNow(reason) {
    if (submitted || submitting) return;
    submitting = true;
    $("confirm").classList.add("d-none");
    setSaveText("Submitting\u2026");
    (function attempt() {
      post("submit", { answers: answers, reason: reason }).then(function (res) {
        if (res.status === 409 && !(res.data && res.data.submitted)) { submitting = false; handleState(res); return; }
        if (!handleState(res)) setTimeout(attempt, 2000);
      }).catch(function () { setSaveText("Could not submit \u2014 retrying\u2026", true); setTimeout(attempt, 2000); });
    })();
  }

  $("btn-submit").addEventListener("click", function () {
    var left = cfg.questions.length - answeredCount();
    $("confirm-text").textContent = left
      ? "You have " + left + " unanswered question" + (left === 1 ? "" : "s") + ". You cannot change anything after you submit."
      : "You cannot change anything after you submit.";
    $("confirm").classList.remove("d-none");
    $("btn-confirm-yes").focus();
  });
  $("btn-confirm-no").addEventListener("click", function () { $("confirm").classList.add("d-none"); });
  $("btn-confirm-yes").addEventListener("click", function () { submitNow("manual"); });
  $("btn-resume").addEventListener("click", function () {
    enterFullscreen().catch(function () {}).then(function () { window.focus(); evaluatePresence(); if (inEpisode) $("pause-text").textContent = pauseMessage(); });
  });

  setupGate();
})();
