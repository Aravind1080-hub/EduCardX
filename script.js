/* EduCardX Mentor: chat client
   Talks to the FastAPI backend on the same origin:
     POST   /chat/stream      streamed answer (Server-Sent Events)
     GET    /history/{id}     restore a conversation
     DELETE /history/{id}     start fresh
     GET    /health           server status
*/
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };

  var els = {
    side: $("side"), scrim: $("scrim"), menuBtn: $("menuBtn"),
    scroll: $("scroll"), thread: $("thread"), welcome: $("welcome"),
    composer: $("composer"), input: $("input"), sendBtn: $("sendBtn"), micBtn: $("micBtn"),
    newChat: $("newChat"), themeBtn: $("themeBtn"), toast: $("toast"),
    status: $("status"), statusText: $("statusText"), card: $("eduCard")
  };

  var SESSION_KEY = "educardx_session";
  var THEME_KEY = "educardx_theme";

  var INTENT_LABEL = {
    RAG: "From EduCardX documents",
    WEB_SEARCH: "From live web search",
    GENERAL: "General answer"
  };

  var state = {
    sessionId: safeGet(SESSION_KEY),
    busy: false,
    controller: null,
    lastQuestion: ""
  };

  function safeGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function safeSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* storage blocked */ } }
  function safeDel(k) { try { localStorage.removeItem(k); } catch (e) { /* storage blocked */ } }

  /* ---------------- Markdown (small, safe) ---------------- */

  function esc(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function inline(t) {
    var codes = [];
    t = t.replace(/`([^`]+)`/g, function (_, c) { codes.push(c); return "\u0000" + (codes.length - 1) + "\u0000"; });
    t = t.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    t = t.replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
    t = t.replace(/(^|[\s(])(https?:\/\/[^\s<)]*[^\s<).,;:!?])/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
    return t.replace(/\u0000(\d+)\u0000/g, function (_, i) { return "<code>" + codes[+i] + "</code>"; });
  }

  function markdown(src) {
    var lines = esc(src).split("\n");
    var html = "", list = null, para = [], code = null, m;

    function flushP() { if (para.length) { html += "<p>" + inline(para.join(" ")) + "</p>"; para = []; } }
    function flushL() { if (list) { html += "</" + list + ">"; list = null; } }

    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];

      if (code !== null) {
        if (/^```/.test(line)) { html += "<pre><code>" + code.join("\n") + "</code></pre>"; code = null; }
        else code.push(line);
        continue;
      }
      if (/^```/.test(line)) { flushP(); flushL(); code = []; continue; }
      if (!line.trim()) { flushP(); flushL(); continue; }

      if ((m = line.match(/^\s{0,3}(#{1,4})\s+(.*)/))) {
        flushP(); flushL();
        var n = Math.min(m[1].length + 2, 5);
        html += "<h" + n + ">" + inline(m[2]) + "</h" + n + ">";
        continue;
      }
      if ((m = line.match(/^\s*[-*\u2022]\s+(.*)/))) {
        flushP();
        if (list !== "ul") { flushL(); html += "<ul>"; list = "ul"; }
        html += "<li>" + inline(m[1]) + "</li>";
        continue;
      }
      if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) {
        flushP();
        if (list !== "ol") { flushL(); html += "<ol>"; list = "ol"; }
        html += "<li>" + inline(m[1]) + "</li>";
        continue;
      }
      flushL();
      para.push(line.trim());
    }
    if (code !== null) html += "<pre><code>" + code.join("\n") + "</code></pre>";
    flushP(); flushL();
    return html;
  }

  function plainText(md) {
    return md.replace(/```[\s\S]*?```/g, " ")
      .replace(/\[([^\]]+)\]\(https?:\/\/[^)]+\)/g, "$1")
      .replace(/https?:\/\/\S+/g, "")
      .replace(/[*_`#>]/g, "");
  }

  /* ---------------- Helpers ---------------- */

  function nearBottom() {
    var s = els.scroll;
    return s.scrollHeight - s.scrollTop - s.clientHeight < 140;
  }

  function toBottom(force) {
    if (force || nearBottom()) els.scroll.scrollTop = els.scroll.scrollHeight;
  }

  var toastTimer;
  function toast(msg) {
    els.toast.textContent = msg;
    els.toast.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { els.toast.classList.remove("show"); }, 1800);
  }

  function setBusy(on) {
    state.busy = on;
    els.sendBtn.classList.toggle("busy", on);
    els.sendBtn.setAttribute("aria-label", on ? "Stop answer" : "Send question");
    els.sendBtn.disabled = !on && !els.input.value.trim();
  }

  function hideWelcome() {
    if (els.welcome && els.welcome.parentNode) els.welcome.remove();
  }

  function autosize() {
    els.input.style.height = "auto";
    els.input.style.height = Math.min(els.input.scrollHeight, 160) + "px";
  }

  var ICON = {
    copy: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2.5"/><path d="M5 15V6.5A2.5 2.5 0 0 1 7.5 4H15"/></svg>',
    speak: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9.5v5h3.5L12 18.5v-13L7.5 9.5z"/><path d="M16 9a4 4 0 0 1 0 6M18.5 6.5a8 8 0 0 1 0 11"/></svg>',
    retry: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M20 12a8 8 0 1 1-2.6-5.9M20 4v5h-5"/></svg>'
  };

  /* ---------------- Message builders ---------------- */

  function addUser(text) {
    var row = document.createElement("article");
    row.className = "msg user";
    var b = document.createElement("div");
    b.className = "bubble";
    b.textContent = text;
    row.appendChild(b);
    els.thread.appendChild(row);
    return row;
  }

  function addBot(opts) {
    opts = opts || {};
    var row = document.createElement("article");
    row.className = "msg bot";
    row.innerHTML =
      '<div class="slip"' + (opts.intent ? ' data-intent="' + opts.intent + '"' : "") + ">" +
        '<div class="slip-head"><span class="tag"></span></div>' +
        '<div class="prose"></div>' +
        '<div class="slip-foot" hidden></div>' +
      "</div>";
    els.thread.appendChild(row);
    var ref = {
      row: row,
      slip: row.firstChild,
      tag: row.querySelector(".tag"),
      prose: row.querySelector(".prose"),
      foot: row.querySelector(".slip-foot"),
      raw: ""
    };
    ref.tag.textContent = opts.label || "";
    if (!opts.label) row.querySelector(".slip-head").hidden = true;
    return ref;
  }

  function showTyping(ref, withNote) {
    ref.prose.innerHTML = '<span class="typing" aria-label="Writing an answer"><i></i><i></i><i></i></span>' +
      (withNote ? '<p class="wake-note">The server is waking up after a quiet spell. The first answer can take up to a minute.</p>' : "");
  }

  function setIntent(ref, intent) {
    ref.slip.setAttribute("data-intent", intent);
    ref.tag.textContent = INTENT_LABEL[intent] || "Answer";
    ref.row.querySelector(".slip-head").hidden = false;
  }

  function renderBody(ref, streaming) {
    ref.prose.innerHTML = markdown(ref.raw);
    ref.prose.classList.toggle("streaming", !!streaming);
  }

  function finishBot(ref, seconds, note) {
    ref.prose.classList.remove("streaming");
    var parts = [];
    parts.push('<span class="time">' + (note ? esc(note) : (seconds ? "Answered in " + seconds.toFixed(1) + " s" : "")) + "</span>");
    if (ref.raw.trim()) {
      parts.push('<button type="button" class="mini-btn" data-act="copy">' + ICON.copy + "Copy</button>");
      if ("speechSynthesis" in window) {
        parts.push('<button type="button" class="mini-btn" data-act="speak">' + ICON.speak + "Listen</button>");
      }
    }
    ref.foot.innerHTML = parts.join("");
    ref.foot.hidden = false;
    ref.foot.addEventListener("click", function (e) {
      var btn = e.target.closest("button[data-act]");
      if (!btn) return;
      if (btn.dataset.act === "copy") copyText(ref.raw);
      if (btn.dataset.act === "speak") speak(ref.raw, btn);
    });
  }

  function showError(ref, message, question) {
    ref.slip.classList.add("error");
    ref.slip.removeAttribute("data-intent");
    ref.row.querySelector(".slip-head").hidden = false;
    ref.tag.textContent = "Something went wrong";
    ref.prose.classList.remove("streaming");
    ref.prose.innerHTML = "<p>" + esc(message) + "</p>";
    ref.foot.innerHTML = '<span class="time"></span><button type="button" class="mini-btn retry">' + ICON.retry + "Try again</button>";
    ref.foot.hidden = false;
    ref.foot.querySelector(".retry").addEventListener("click", function () {
      ref.row.remove();
      ask(question, { skipUser: true });
    });
  }

  /* ---------------- Copy and listen ---------------- */

  function copyText(text) {
    var done = function () { toast("Answer copied"); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { toast("Could not copy"); });
    } else {
      var ta = document.createElement("textarea");
      ta.value = text; document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); done(); } catch (e) { toast("Could not copy"); }
      ta.remove();
    }
  }

  function speak(text, btn) {
    var synth = window.speechSynthesis;
    if (synth.speaking) { synth.cancel(); resetSpeakButtons(); return; }
    var u = new SpeechSynthesisUtterance(plainText(text));
    u.lang = "en-IN";
    u.onend = u.onerror = resetSpeakButtons;
    btn.lastChild.textContent = "Stop";
    synth.speak(u);
  }

  function resetSpeakButtons() {
    document.querySelectorAll('button[data-act="speak"]').forEach(function (b) { b.lastChild.textContent = "Listen"; });
  }

  /* ---------------- Streaming ---------------- */

  function parseEvents(buffer, onEvent) {
    var idx;
    while ((idx = buffer.indexOf("\n\n")) !== -1) {
      var block = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      var ev = "message", data = "";
      block.split("\n").forEach(function (l) {
        if (l.indexOf("event:") === 0) ev = l.slice(6).trim();
        else if (l.indexOf("data:") === 0) data += l.slice(5).trim();
      });
      if (data) {
        try { onEvent(ev, JSON.parse(data)); } catch (e) { /* ignore malformed chunk */ }
      }
    }
    return buffer;
  }

  async function ask(text, opts) {
    opts = opts || {};
    text = (text || "").trim();
    if (!text || state.busy) return;

    state.lastQuestion = text;
    hideWelcome();
    if (!opts.skipUser) addUser(text);

    var bot = addBot();
    showTyping(bot, false);
    toBottom(true);

    setBusy(true);
    state.controller = new AbortController();

    var gotMeta = false;
    var wakeTimer = setTimeout(function () { if (!gotMeta) { showTyping(bot, true); } }, 5000);
    var finished = false;
    var streamError = null;

    try {
      var res = await fetch("/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: state.sessionId, message: text }),
        signal: state.controller.signal
      });

      if (!res.ok) {
        var detail = "The server returned an error (" + res.status + ").";
        try { var j = await res.json(); if (j && typeof j.detail === "string") detail = j.detail; } catch (e) { /* keep default */ }
        throw new Error(detail);
      }

      var reader = res.body.getReader();
      var decoder = new TextDecoder();
      var buffer = "";

      while (true) {
        var chunk = await reader.read();
        if (chunk.done) break;
        buffer = parseEvents(buffer + decoder.decode(chunk.value, { stream: true }).replace(/\r\n/g, "\n"), function (ev, data) {
          if (ev === "meta") {
            gotMeta = true;
            clearTimeout(wakeTimer);
            if (data.session_id) { state.sessionId = data.session_id; safeSet(SESSION_KEY, data.session_id); }
            setIntent(bot, data.intent);
            showTyping(bot, false);
          } else if (ev === "token") {
            bot.raw += data.text;
            renderBody(bot, true);
            toBottom(false);
          } else if (ev === "done") {
            finished = true;
            if (typeof data.answer === "string" && data.answer) bot.raw = data.answer;
            if (!bot.raw.trim()) bot.raw = "I could not find an answer to that. Try rephrasing your question.";
            renderBody(bot, false);
            var t = data.timings && data.timings.TOTAL_response_time;
            finishBot(bot, t);
            toBottom(false);
          } else if (ev === "error") {
            streamError = data.detail || "The assistant ran into a problem.";
          }
        });
      }

      if (streamError) throw new Error(streamError);

      if (!finished) {
        if (bot.raw.trim()) { renderBody(bot, false); finishBot(bot, 0, "Connection ended early"); }
        else throw new Error("The connection closed before an answer arrived.");
      }
    } catch (err) {
      if (err && err.name === "AbortError") {
        if (bot.raw.trim()) { renderBody(bot, false); finishBot(bot, 0, "Stopped"); }
        else { bot.row.remove(); }
      } else {
        var msg = (err && err.message) || "Something went wrong.";
        if (/Failed to fetch|NetworkError|Load failed/i.test(msg)) {
          msg = "Could not reach the server. Check your connection and try again.";
        }
        showError(bot, msg, text);
      }
    } finally {
      clearTimeout(wakeTimer);
      state.controller = null;
      setBusy(false);
      checkHealth();
    }
  }

  /* ---------------- Composer ---------------- */

  els.composer.addEventListener("submit", function (e) {
    e.preventDefault();
    if (state.busy) { if (state.controller) state.controller.abort(); return; }
    var text = els.input.value;
    if (!text.trim()) return;
    els.input.value = "";
    autosize();
    ask(text);
  });

  els.input.addEventListener("input", function () {
    autosize();
    if (!state.busy) els.sendBtn.disabled = !els.input.value.trim();
  });

  els.input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      els.composer.requestSubmit();
    }
  });

  document.getElementById("starters").addEventListener("click", function (e) {
    var b = e.target.closest(".starter");
    if (b) ask(b.dataset.q);
  });

  /* ---------------- Voice input (browser speech recognition) ---------------- */

  var Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  var recognizer = null;

  if (Recognition) {
    els.micBtn.hidden = false;

    els.micBtn.addEventListener("click", function () {
      if (recognizer) { recognizer.stop(); return; }
      if (state.busy) return;

      recognizer = new Recognition();
      recognizer.lang = "en-IN";
      recognizer.interimResults = true;
      recognizer.continuous = false;

      var finalText = "";
      els.micBtn.classList.add("listening");
      els.micBtn.setAttribute("aria-label", "Stop listening");
      els.input.placeholder = "Listening\u2026";

      recognizer.onresult = function (ev) {
        var interim = "";
        for (var i = ev.resultIndex; i < ev.results.length; i++) {
          var r = ev.results[i];
          if (r.isFinal) finalText += r[0].transcript; else interim += r[0].transcript;
        }
        els.input.value = (finalText + interim).trim();
        autosize();
        els.sendBtn.disabled = !els.input.value.trim();
      };

      recognizer.onerror = function (ev) {
        if (ev.error === "not-allowed" || ev.error === "service-not-allowed") toast("Allow microphone access to use voice");
        else if (ev.error === "no-speech") toast("No speech heard. Try again.");
      };

      recognizer.onend = function () {
        recognizer = null;
        els.micBtn.classList.remove("listening");
        els.micBtn.setAttribute("aria-label", "Speak your question");
        els.input.placeholder = "Ask about repayments, scholarships, colleges\u2026";
        var text = els.input.value.trim();
        if (finalText.trim() && text) {
          els.input.value = "";
          autosize();
          ask(text);
        }
      };

      try { recognizer.start(); } catch (e) { recognizer = null; }
    });
  }

  /* ---------------- New chat ---------------- */

  function resetThread() {
    els.thread.innerHTML = "";
    var w = document.createElement("section");
    w.className = "welcome";
    w.id = "welcome";
    w.innerHTML = welcomeHTML;
    els.thread.appendChild(w);
    els.welcome = w;
    w.querySelector("#starters").addEventListener("click", function (e) {
      var b = e.target.closest(".starter");
      if (b) ask(b.dataset.q);
    });
  }

  var welcomeHTML = els.welcome.innerHTML;

  els.newChat.addEventListener("click", function () {
    if (state.controller) state.controller.abort();
    if (window.speechSynthesis) window.speechSynthesis.cancel();
    var old = state.sessionId;
    state.sessionId = null;
    safeDel(SESSION_KEY);
    if (old) fetch("/history/" + encodeURIComponent(old), { method: "DELETE" }).catch(function () {});
    resetThread();
    els.input.focus();
    closeMenu();
  });

  /* ---------------- Restore previous conversation ---------------- */

  async function restore() {
    if (!state.sessionId) return;
    try {
      var res = await fetch("/history/" + encodeURIComponent(state.sessionId));
      if (!res.ok) return;
      var data = await res.json();
      if (!data.history || !data.history.length) return;
      hideWelcome();
      data.history.forEach(function (m) {
        if (m.role === "user") addUser(m.content);
        else {
          var b = addBot();
          b.raw = m.content;
          renderBody(b, false);
          finishBot(b, 0, "Earlier answer");
        }
      });
      toBottom(true);
    } catch (e) { /* offline: stay on the welcome screen */ }
  }

  /* ---------------- Server status ---------------- */

  async function checkHealth() {
    try {
      var ctl = new AbortController();
      var timer = setTimeout(function () { ctl.abort(); }, 6000);
      var res = await fetch("/health", { signal: ctl.signal });
      clearTimeout(timer);
      var h = await res.json();
      els.status.dataset.state = "ok";
      els.statusText.textContent = "Online, " + h.documents + " documents loaded";
    } catch (e) {
      els.status.dataset.state = "down";
      els.statusText.textContent = "Server not responding";
    }
  }

  /* ---------------- Theme ---------------- */

  function syncThemeLabel() {
    var dark = document.documentElement.getAttribute("data-theme") === "dark";
    els.themeBtn.querySelector(".theme-label").textContent = dark ? "Light mode" : "Dark mode";
  }

  els.themeBtn.addEventListener("click", function () {
    var dark = document.documentElement.getAttribute("data-theme") !== "dark";
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
    safeSet(THEME_KEY, dark ? "dark" : "light");
    syncThemeLabel();
  });

  /* ---------------- Mobile menu ---------------- */

  function openMenu() {
    els.side.classList.add("open");
    els.scrim.hidden = false;
    els.menuBtn.setAttribute("aria-expanded", "true");
  }
  function closeMenu() {
    els.side.classList.remove("open");
    els.scrim.hidden = true;
    els.menuBtn.setAttribute("aria-expanded", "false");
  }
  els.menuBtn.addEventListener("click", openMenu);
  els.scrim.addEventListener("click", closeMenu);
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeMenu(); });

  /* ---------------- Card tilt (pointer devices only) ---------------- */

  var canTilt = window.matchMedia("(hover: hover)").matches &&
                !window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  if (canTilt && els.card) {
    var stage = els.card.parentNode;
    stage.addEventListener("pointermove", function (e) {
      var r = stage.getBoundingClientRect();
      var x = (e.clientX - r.left) / r.width - 0.5;
      var y = (e.clientY - r.top) / r.height - 0.5;
      els.card.style.transform = "rotateY(" + (x * 14).toFixed(2) + "deg) rotateX(" + (-y * 14).toFixed(2) + "deg)";
    });
    stage.addEventListener("pointerleave", function () { els.card.style.transform = ""; });
  }

  /* ---------------- Start ---------------- */

  syncThemeLabel();
  els.sendBtn.disabled = true;
  checkHealth();
  restore();
  els.input.focus({ preventScroll: true });
})();
