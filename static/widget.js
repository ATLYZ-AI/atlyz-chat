(function () {
  "use strict";

  var scriptTag = document.currentScript || (function () {
    var s = document.getElementsByTagName("script");
    return s[s.length - 1];
  })();

  var src = scriptTag ? scriptTag.src : "";
  var params = {};
  try {
    (src.split("?")[1] || "").split("&").forEach(function (p) {
      var kv = p.split("=");
      if (kv[0]) params[kv[0]] = decodeURIComponent(kv[1] || "");
    });
  } catch (e) {}

  var BUSINESS_ID = params.id || "";
  var SERVER_URL  = src.split("/widget.js")[0] || "";
  var POSITION    = params.position || "bottom-right";
  var STORAGE_KEY = "atlyz_chat_" + BUSINESS_ID;

  if (!BUSINESS_ID) {
    console.warn("[Atlyz] No business ID — add ?id=your_id to the script tag.");
    return;
  }

  var sessionId    = null;
  var isOpen       = false;
  var businessName = "Atlyz";
  var botName      = "Aria";
  var botTagline   = "Your AI Assistant";
  var whiteLabel   = false;
  var greeting     = "Hi! How can I help you today?";
  var primaryColor = "#7c3aed";
  var leadMode     = false;
  var thinkingTimer = null;
  var thinkingIdx   = 0;

  function getThinkingPhrases() {
    return [
      botName + " is thinking…",
      "Analyzing your question…",
      "Looking that up…",
      "One moment…",
      botName + " is on it…",
      "Checking the knowledge base…",
      "Almost there…"
    ];
  }

  // ── Persistence ──────────────────────────────────────────────────────────────
  function saveSession() {
    try {
      var msgs = [];
      document.querySelectorAll(".atz-msg").forEach(function (el) {
        msgs.push({ text: el.textContent, fromUser: el.classList.contains("atz-user") });
      });
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ sessionId: sessionId, msgs: msgs }));
    } catch (e) {}
  }

  function loadSession() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return null;
      return JSON.parse(raw);
    } catch (e) { return null; }
  }

  function clearSession() {
    try { localStorage.removeItem(STORAGE_KEY); } catch (e) {}
  }

  // ── Color helpers ─────────────────────────────────────────────────────────────
  function applyColor(color) {
    primaryColor = color || "#7c3aed";
    var root = document.getElementById("atz-color-vars");
    if (!root) {
      root = document.createElement("style");
      root.id = "atz-color-vars";
      document.head.appendChild(root);
    }
    root.textContent = ":root{--atz-c:" + primaryColor + ";--atz-c2:" + shiftColor(primaryColor) + ";}";
  }

  function shiftColor(hex) {
    var companions = {
      "#7c3aed":"#2563eb","#6d28d9":"#1d4ed8","#8b5cf6":"#3b82f6",
      "#a855f7":"#7c3aed","#ec4899":"#8b5cf6","#ef4444":"#dc2626",
      "#10b981":"#059669","#f59e0b":"#d97706","#3b82f6":"#1d4ed8",
      "#0ea5e9":"#2563eb","#14b8a6":"#0891b2","#22c55e":"#16a34a",
      "#f43f5e":"#e11d48","#8b5cf6":"#7c3aed"
    };
    return companions[hex.toLowerCase()] || hex;
  }

  // ── CSS ──────────────────────────────────────────────────────────────────────
  function injectCSS() {
    applyColor(primaryColor);
    var isRight = POSITION.includes("right");
    var side    = isRight ? "right:24px;" : "left:24px;";

    var css = [
      "@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap');",

      /* ── FAB button ── */
      "#atz-btn{position:fixed;" + side + "bottom:24px;width:56px;height:56px;",
      "border-radius:50%;background:linear-gradient(135deg,var(--atz-c),var(--atz-c2));",
      "border:none;cursor:pointer;z-index:999998;",
      "box-shadow:0 0 0 1px rgba(124,58,237,.25),0 8px 28px rgba(124,58,237,.38);",
      "display:flex;align-items:center;justify-content:center;",
      "transition:transform .2s,box-shadow .2s;outline:none;overflow:visible;}",
      "#atz-btn:hover{transform:translateY(-2px);",
      "box-shadow:0 0 0 1px rgba(124,58,237,.4),0 14px 40px rgba(124,58,237,.48);}",

      /* pulse ring on FAB */
      "#atz-btn::before{content:'';position:absolute;inset:-4px;border-radius:50%;",
      "border:1.5px solid rgba(124,58,237,.35);",
      "animation:atz-ring 2.4s ease-in-out infinite;pointer-events:none;}",
      "@keyframes atz-ring{0%,100%{transform:scale(1);opacity:.6}50%{transform:scale(1.12);opacity:0}}",

      /* notification dot */
      "#atz-dot{position:absolute;top:1px;right:1px;width:11px;height:11px;",
      "border-radius:50%;background:#22c55e;border:2px solid #09090b;display:none;}",

      /* ── Chat window ── */
      "#atz-box{position:fixed;" + side + "bottom:92px;width:370px;",
      "max-width:calc(100vw - 48px);height:540px;max-height:calc(100vh - 116px);",
      "background:#0b0b0f;border:1px solid rgba(255,255,255,.07);",
      "border-radius:20px;z-index:999999;display:none;flex-direction:column;",
      "overflow:hidden;font-family:'Inter',system-ui,sans-serif;font-size:14px;",
      "box-shadow:0 0 0 1px rgba(124,58,237,.1),0 24px 80px rgba(0,0,0,.8);",
      "animation:atz-up .22s cubic-bezier(.34,1.56,.64,1);}",
      "#atz-box.atz-open{display:flex;}",
      "@keyframes atz-up{from{opacity:0;transform:translateY(16px) scale(.97)}",
      "to{opacity:1;transform:translateY(0) scale(1)}}",

      /* ── Header ── */
      "#atz-head{padding:14px 16px;display:flex;align-items:center;gap:12px;",
      "border-bottom:1px solid rgba(255,255,255,.06);flex-shrink:0;",
      "background:linear-gradient(135deg,rgba(124,58,237,.10),rgba(37,99,235,.06));}",

      "#atz-logo{width:34px;height:34px;border-radius:9px;flex-shrink:0;",
      "background:linear-gradient(135deg,var(--atz-c),var(--atz-c2));",
      "display:flex;align-items:center;justify-content:center;}",

      /* tooltip on FAB */
      "#atz-tooltip{position:absolute;bottom:calc(100% + 10px);right:0;",
      "background:#18181b;border:1px solid rgba(255,255,255,.1);",
      "color:#f4f4f5;font-size:12px;font-weight:500;letter-spacing:.2px;",
      "padding:6px 12px;border-radius:8px;white-space:nowrap;",
      "pointer-events:none;opacity:0;transform:translateY(4px);",
      "transition:opacity .2s,transform .2s;box-shadow:0 4px 16px rgba(0,0,0,.5);}",
      "#atz-tooltip::after{content:'';position:absolute;top:100%;right:14px;",
      "border:5px solid transparent;border-top-color:#18181b;}",
      "#atz-btn:hover #atz-tooltip{opacity:1;transform:translateY(0);}",
      "#atz-btn.atz-open #atz-tooltip{opacity:0!important;}",

      "#atz-head-info{flex:1;min-width:0;}",
      "#atz-head-name{font-size:14px;font-weight:600;color:#f4f4f5;letter-spacing:-.2px;",
      "white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}",
      "#atz-head-tagline{font-size:11px;color:#52525b;margin-top:1px;letter-spacing:.1px;}",
      "#atz-head-status{font-size:11px;color:#4ade80;margin-top:3px;display:flex;",
      "align-items:center;gap:5px;}",
      "#atz-head-status::before{content:'';width:6px;height:6px;border-radius:50%;",
      "background:#4ade80;flex-shrink:0;transition:background .3s;}",
      "#atz-head-status.atz-thinking-status{color:#a78bfa;}",
      "#atz-head-status.atz-thinking-status::before{background:#a78bfa;",
      "animation:atz-pulse 1s ease-in-out infinite;}",
      "@keyframes atz-pulse{0%,100%{opacity:1}50%{opacity:.3}}",

      "#atz-head-btns{display:flex;align-items:center;gap:2px;flex-shrink:0;}",
      ".atz-hbtn{background:none;border:none;color:rgba(255,255,255,.25);",
      "cursor:pointer;padding:6px;border-radius:7px;line-height:1;",
      "display:flex;align-items:center;justify-content:center;",
      "transition:color .15s,background .15s;}",
      ".atz-hbtn:hover{color:rgba(255,255,255,.7);background:rgba(255,255,255,.06);}",

      /* ── Messages area ── */
      "#atz-msgs{flex:1;overflow-y:auto;padding:16px;display:flex;",
      "flex-direction:column;gap:8px;background:#09090b;}",
      "#atz-msgs::-webkit-scrollbar{width:3px;}",
      "#atz-msgs::-webkit-scrollbar-thumb{background:rgba(255,255,255,.08);border-radius:3px;}",

      ".atz-msg{max-width:82%;padding:9px 13px;font-size:13.5px;",
      "line-height:1.55;word-wrap:break-word;animation:atz-msg .18s ease;}",
      "@keyframes atz-msg{from{opacity:0;transform:translateY(7px)}to{opacity:1;transform:none}}",

      ".atz-bot{background:#18181b;color:#e4e4e7;",
      "border-radius:4px 12px 12px 12px;align-self:flex-start;",
      "border:1px solid rgba(255,255,255,.06);}",
      ".atz-user{background:linear-gradient(135deg,var(--atz-c),var(--atz-c2));color:#fff;",
      "border-radius:12px 4px 12px 12px;align-self:flex-end;}",

      /* ── Typing / thinking bubble ── */
      "#atz-thinking-bubble{display:flex;align-items:center;gap:10px;",
      "padding:10px 14px;background:#18181b;border:1px solid rgba(167,139,250,.14);",
      "border-radius:4px 12px 12px 12px;align-self:flex-start;",
      "animation:atz-msg .18s ease;max-width:82%;}",
      ".atz-th-dots{display:flex;gap:4px;flex-shrink:0;}",
      ".atz-th-dots span{width:5px;height:5px;border-radius:50%;",
      "background:var(--atz-c);animation:atz-dot 1.2s infinite;}",
      ".atz-th-dots span:nth-child(2){animation-delay:.2s;}",
      ".atz-th-dots span:nth-child(3){animation-delay:.4s;}",
      "@keyframes atz-dot{0%,60%,100%{transform:translateY(0);opacity:.35}",
      "30%{transform:translateY(-5px);opacity:1}}",
      ".atz-th-text{font-size:12.5px;color:#52525b;font-style:italic;transition:opacity .25s;}",

      /* ── Input area ── */
      "#atz-input-wrap{padding:12px;border-top:1px solid rgba(255,255,255,.06);",
      "display:flex;gap:8px;align-items:flex-end;background:#0b0b0f;flex-shrink:0;}",

      "#atz-input{flex:1;background:#18181b;border:1px solid rgba(255,255,255,.07);",
      "border-radius:10px;padding:9px 13px;font-size:13.5px;color:#f4f4f5;",
      "outline:none;resize:none;max-height:80px;font-family:inherit;",
      "line-height:1.45;transition:border-color .2s,box-shadow .2s;}",
      "#atz-input::placeholder{color:#3f3f46;}",
      "#atz-input:focus{border-color:rgba(124,58,237,.45);",
      "box-shadow:0 0 0 3px rgba(124,58,237,.08);}",

      "#atz-send{width:36px;height:36px;border-radius:9px;flex-shrink:0;",
      "background:linear-gradient(135deg,var(--atz-c),var(--atz-c2));border:none;",
      "cursor:pointer;display:flex;align-items:center;justify-content:center;",
      "transition:opacity .15s,transform .15s,box-shadow .15s;",
      "box-shadow:0 2px 8px rgba(124,58,237,.3);}",
      "#atz-send:hover{opacity:.9;transform:translateY(-1px);",
      "box-shadow:0 4px 14px rgba(124,58,237,.45);}",
      "#atz-send:disabled{opacity:.28;cursor:not-allowed;transform:none;box-shadow:none;}",

      /* ── Lead capture form ── */
      "#atz-lead-form{padding:14px 12px 10px;background:#0b0b0f;",
      "border-top:1px solid rgba(255,255,255,.06);",
      "display:flex;flex-direction:column;gap:8px;flex-shrink:0;}",

      ".atz-lead-input{background:#18181b;border:1px solid rgba(255,255,255,.07);",
      "border-radius:9px;padding:9px 13px;font-size:13px;color:#f4f4f5;",
      "outline:none;font-family:inherit;transition:border-color .2s,box-shadow .2s;",
      "width:100%;box-sizing:border-box;}",
      ".atz-lead-input::placeholder{color:#3f3f46;}",
      ".atz-lead-input:focus{border-color:rgba(124,58,237,.45);",
      "box-shadow:0 0 0 3px rgba(124,58,237,.08);}",

      ".atz-lead-btn{padding:10px;",
      "background:linear-gradient(135deg,var(--atz-c),var(--atz-c2));",
      "color:#fff;border:none;border-radius:9px;font-size:13px;font-weight:500;",
      "cursor:pointer;font-family:inherit;transition:opacity .2s,transform .15s;",
      "box-shadow:0 2px 8px rgba(124,58,237,.28);}",
      ".atz-lead-btn:hover{opacity:.9;transform:translateY(-1px);}",

      "#atz-lead-note{font-size:10.5px;color:#3f3f46;text-align:center;",
      "margin:0;padding:2px 0 0;line-height:1.4;}",

      /* ── Footer ── */
      "#atz-foot{text-align:center;font-size:10.5px;color:#27272a;",
      "padding:5px 0 7px;background:#0b0b0f;",
      "border-top:1px solid rgba(255,255,255,.03);}",
      "#atz-foot a{color:#3f3f46;text-decoration:none;transition:color .15s;}",
      "#atz-foot a:hover{color:#71717a;}"

    ].join("");

    var el = document.createElement("style");
    el.textContent = css;
    document.head.appendChild(el);
  }

  // ── Icons ────────────────────────────────────────────────────────────────────
  var CHAT_ICON  = '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/></svg>';
  var CLOSE_ICON = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
  var CLEAR_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 102.13-9.36L1 10"/></svg>';
  var SEND_ICON  = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>';
  var LOGO_ICON  = '<svg width="18" height="18" viewBox="0 0 100 100" fill="none"><defs><linearGradient id="ag" x1="0" y1="1" x2="1" y2="0"><stop offset="0%" stop-color="#a78bfa"/><stop offset="100%" stop-color="#93c5fd"/></linearGradient></defs><circle cx="50" cy="50" r="44" stroke="url(#ag)" stroke-width="3"/><line x1="50" y1="73" x2="19" y2="31" stroke="url(#ag)" stroke-width="2.8" stroke-linecap="round"/><circle cx="19" cy="31" r="5" fill="url(#ag)"/><line x1="50" y1="73" x2="50" y2="11" stroke="url(#ag)" stroke-width="2.8" stroke-linecap="round"/><circle cx="50" cy="11" r="5" fill="url(#ag)"/><line x1="50" y1="73" x2="81" y2="31" stroke="url(#ag)" stroke-width="2.8" stroke-linecap="round"/><circle cx="81" cy="31" r="5" fill="url(#ag)"/><circle cx="50" cy="73" r="5.5" fill="url(#ag)"/></svg>';

  // ── Build DOM ────────────────────────────────────────────────────────────────
  function buildHTML() {
    var btn = document.createElement("button");
    btn.id = "atz-btn";
    btn.setAttribute("aria-label", "Open chat");
    btn.innerHTML = CHAT_ICON + '<span id="atz-dot"></span><span id="atz-tooltip">Ask ' + botName + '</span>';
    btn.onclick = toggleChat;

    var box = document.createElement("div");
    box.id = "atz-box";
    box.innerHTML =
      '<div id="atz-head">' +
        '<div id="atz-logo">' + LOGO_ICON + '</div>' +
        '<div id="atz-head-info">' +
          '<div id="atz-head-name">' + botName + '</div>' +
          '<div id="atz-head-tagline">' + botTagline + '</div>' +
          '<div id="atz-head-status">Online now</div>' +
        '</div>' +
        '<div id="atz-head-btns">' +
          '<button class="atz-hbtn" id="atz-clear" title="Clear conversation" aria-label="Clear">' + CLEAR_ICON + '</button>' +
          '<button class="atz-hbtn" id="atz-close" title="Close" aria-label="Close">' + CLOSE_ICON + '</button>' +
        '</div>' +
      '</div>' +
      '<div id="atz-msgs"></div>' +
      '<div id="atz-input-wrap">' +
        '<textarea id="atz-input" placeholder="Ask anything…" rows="1" aria-label="Message"></textarea>' +
        '<button id="atz-send" aria-label="Send">' + SEND_ICON + '</button>' +
      '</div>' +
      '<div id="atz-foot">Powered by <a href="https://atlyz.com" target="_blank">Atlyz</a></div>';

    document.body.appendChild(btn);
    document.body.appendChild(box);

    document.getElementById("atz-close").onclick = closeChat;
    document.getElementById("atz-send").onclick   = sendMessage;
    document.getElementById("atz-clear").onclick  = clearChat;

    var inp = document.getElementById("atz-input");
    inp.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
    });
    inp.addEventListener("input", function () {
      this.style.height = "auto";
      this.style.height = Math.min(this.scrollHeight, 80) + "px";
    });
  }

  // ── Open / close ─────────────────────────────────────────────────────────────
  function toggleChat() { isOpen ? closeChat() : openChat(); }

  function openChat() {
    isOpen = true;
    document.getElementById("atz-box").classList.add("atz-open");
    document.getElementById("atz-btn").classList.add("atz-open");
    hideDot();
    if (!sessionId) initSession();
    setTimeout(function () {
      var inp = document.getElementById("atz-input");
      if (inp) inp.focus();
    }, 120);
  }

  function closeChat() {
    isOpen = false;
    document.getElementById("atz-box").classList.remove("atz-open");
    document.getElementById("atz-btn").classList.remove("atz-open");
  }

  function clearChat() {
    clearSession();
    sessionId = null;
    leadMode  = false;
    document.getElementById("atz-msgs").innerHTML = "";
    var wrap = document.getElementById("atz-input-wrap");
    var form = document.getElementById("atz-lead-form");
    if (form) form.remove();
    if (wrap) wrap.style.display = "";
    initSession();
  }

  function showDot() { if (!isOpen) document.getElementById("atz-dot").style.display = "block"; }
  function hideDot() { document.getElementById("atz-dot").style.display = "none"; }

  // ── Messages ─────────────────────────────────────────────────────────────────
  function addMsg(text, fromUser) {
    var msgs = document.getElementById("atz-msgs");
    var el = document.createElement("div");
    el.className = "atz-msg " + (fromUser ? "atz-user" : "atz-bot");
    el.textContent = text;
    msgs.appendChild(el);
    msgs.scrollTop = msgs.scrollHeight;
    saveSession();
  }

  // ── Thinking indicator ────────────────────────────────────────────────────────
  function showThinking() {
    var phrases = getThinkingPhrases();
    var msgs = document.getElementById("atz-msgs");
    var el = document.createElement("div");
    el.id = "atz-thinking-bubble";
    el.innerHTML =
      '<div class="atz-th-dots"><span></span><span></span><span></span></div>' +
      '<span class="atz-th-text" id="atz-th-text">' + phrases[0] + '</span>';
    msgs.appendChild(el);
    msgs.scrollTop = msgs.scrollHeight;

    var status = document.getElementById("atz-head-status");
    if (status) {
      status.textContent = phrases[0];
      status.className = "atz-thinking-status";
    }

    thinkingIdx = 0;
    thinkingTimer = setInterval(function () {
      var ph = getThinkingPhrases();
      thinkingIdx = (thinkingIdx + 1) % ph.length;
      var phrase = ph[thinkingIdx];
      var txt = document.getElementById("atz-th-text");
      if (txt) {
        txt.style.opacity = "0";
        setTimeout(function () {
          if (txt) { txt.textContent = phrase; txt.style.opacity = "1"; }
        }, 200);
      }
      if (status) status.textContent = phrase;
    }, 1800);
  }

  function hideThinking() {
    clearInterval(thinkingTimer);
    var el = document.getElementById("atz-thinking-bubble");
    if (el) el.remove();

    var status = document.getElementById("atz-head-status");
    if (status) {
      status.textContent = "Online now";
      status.className = "";
    }
  }

  // ── Session init ──────────────────────────────────────────────────────────────
  function initSession() {
    var saved = loadSession();
    var savedMsgs = (saved && saved.msgs) ? saved.msgs : [];

    if (savedMsgs.length) {
      savedMsgs.forEach(function (m) {
        var msgs = document.getElementById("atz-msgs");
        var el = document.createElement("div");
        el.className = "atz-msg " + (m.fromUser ? "atz-user" : "atz-bot");
        el.textContent = m.text;
        msgs.appendChild(el);
      });
      document.getElementById("atz-msgs").scrollTop = 99999;
    }

    fetch(SERVER_URL + "/chat/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ business_id: BUSINESS_ID })
    })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      sessionId = d.session_id;

      if (d.business_name) businessName = d.business_name;

      if (d.config) {
        if (d.config.primary_color) applyColor(d.config.primary_color);

        if (d.config.bot_name) {
          botName = d.config.bot_name;
          var nameEl = document.getElementById("atz-head-name");
          if (nameEl) nameEl.textContent = botName;
          var tipEl = document.getElementById("atz-tooltip");
          if (tipEl) tipEl.textContent = "Ask " + botName;
        }

        if (d.config.bot_tagline) {
          botTagline = d.config.bot_tagline;
          var tagEl = document.getElementById("atz-head-tagline");
          if (tagEl) tagEl.textContent = botTagline;
        }

        if (d.config.white_label) {
          var foot = document.getElementById("atz-foot");
          if (foot) foot.style.display = "none";
        }
      }

      if (!savedMsgs.length) {
        if (d.greeting) greeting = d.greeting;
        setTimeout(function () {
          addMsg(greeting, false);
          if (!isOpen) showDot();
        }, 350);
      }
    })
    .catch(function (e) {
      console.error("[Atlyz] Session start failed:", e);
    });
  }

  // ── Send ──────────────────────────────────────────────────────────────────────
  function sendMessage() {
    var inp = document.getElementById("atz-input");
    if (!inp || !sessionId) return;
    var text = inp.value.trim();
    if (!text) return;

    inp.value = "";
    inp.style.height = "auto";
    addMsg(text, true);
    showThinking();

    var sendBtn = document.getElementById("atz-send");
    if (sendBtn) sendBtn.disabled = true;
    inp.disabled = true;

    fetch(SERVER_URL + "/chat/message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, message: text, business_id: BUSINESS_ID })
    })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      hideThinking();
      if (sendBtn) sendBtn.disabled = false;
      inp.disabled = false;
      inp.focus();
      addMsg(d.reply || "Sorry, I had trouble with that.", false);
      if (!isOpen) showDot();
      if (d.action === "collect_lead" && !leadMode) showLeadForm();
    })
    .catch(function () {
      hideThinking();
      if (sendBtn) sendBtn.disabled = false;
      inp.disabled = false;
      addMsg("Connection issue — please try again.", false);
    });
  }

  // ── Lead form ─────────────────────────────────────────────────────────────────
  function showLeadForm() {
    leadMode = true;
    var wrap = document.getElementById("atz-input-wrap");
    if (!wrap) return;

    var form = document.createElement("div");
    form.id = "atz-lead-form";
    form.innerHTML =
      '<input class="atz-lead-input" id="atz-ln" type="text" placeholder="Your name" />' +
      '<input class="atz-lead-input" id="atz-le" type="email" placeholder="Email address" />' +
      '<input class="atz-lead-input" id="atz-lp" type="tel" placeholder="Phone (optional)" />' +
      '<button class="atz-lead-btn" id="atz-lsub">Send — we\'ll be in touch</button>' +
      '<p id="atz-lead-note">We\'ll only use this to follow up on your question.</p>';

    wrap.style.display = "none";
    wrap.parentNode.insertBefore(form, wrap);
    document.getElementById("atz-lsub").onclick = submitLead;
  }

  function submitLead() {
    var name  = (document.getElementById("atz-ln") || {}).value || "";
    var email = (document.getElementById("atz-le") || {}).value || "";
    var phone = (document.getElementById("atz-lp") || {}).value || "";
    if (!name && !email) { alert("Please enter your name or email."); return; }

    var userMsgs = document.querySelectorAll(".atz-user");
    var lastQ = userMsgs.length ? userMsgs[userMsgs.length - 1].textContent : "";

    fetch(SERVER_URL + "/chat/lead", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, name: name, email: email, phone: phone, question: lastQ })
    })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      var form = document.getElementById("atz-lead-form");
      if (form) form.innerHTML = '<p style="text-align:center;color:#52525b;font-size:13px;padding:10px 0;">Got it — we\'ll reach out soon. ✓</p>';
      addMsg(d.message || "Thanks! The owner will contact you shortly.", false);
      clearSession();
    })
    .catch(function () {
      addMsg("Something went wrong. Please try again.", false);
    });
  }

  // ── Init ──────────────────────────────────────────────────────────────────────
  function init() {
    injectCSS();
    buildHTML();
    setTimeout(initSession, 400);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

})();
