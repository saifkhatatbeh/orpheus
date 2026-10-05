/* ORPHEUS front-end — every open tab is a Constellation node. */
"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

/* ---------- identity ---------- */
function deviceKind() {
  const ua = navigator.userAgent;
  if (/iPad|Macintosh.*Mobile/.test(ua) || (navigator.maxTouchPoints > 2 && /Mac/.test(ua))) return "tablet";
  if (/iPhone|Android.*Mobile/.test(ua)) return "phone";
  if (/Android/.test(ua)) return "tablet";
  return "pc";
}
const KIND = deviceKind();
let nodeName = localStorage.getItem("orpheus_name");
if (!nodeName) {
  nodeName = KIND === "pc" ? "EDIFIER DECK (PC)" : KIND === "phone" ? "PHONE" : KIND === "tablet" ? "IPAD" : "NODE";
  localStorage.setItem("orpheus_name", nodeName);
}
/* theme system — cycle through skins, persist */
const THEMES = [
  { id: "", name: "ULTRAMARINE" },
  { id: "void", name: "VOID" },
  { id: "darkroom", name: "DARKROOM" },
  { id: "terminal", name: "TERMINAL" },
  { id: "tangerine", name: "TANGERINE" },
  { id: "riviera", name: "RIVIERA" },
];
function applyTheme(id) {
  if (id) document.body.dataset.theme = id; else delete document.body.dataset.theme;
  localStorage.setItem("orpheus_theme", id);
  const t = THEMES.find((t) => t.id === id) || THEMES[0];
  $("#btn-theme").textContent = "◐ " + t.name;
}
applyTheme(localStorage.getItem("orpheus_theme") || "");
$("#btn-theme").onclick = () => {
  const cur = localStorage.getItem("orpheus_theme") || "";
  const i = THEMES.findIndex((t) => t.id === cur);
  const next = THEMES[(i + 1) % THEMES.length];
  applyTheme(next.id);
  toast("THEME · " + next.name);
};

/* ---------- state ---------- */
let ws, me = null, S = null, nodes = [], clockOffset = 0, clockRtt = 1e9, clockAt = 0;
let userSeeking = false, lastLoadedSrc = "";
let lyricsData = null, lyricsFor = null, lyricsOn = false;
const player = $("#player");

/* ---------- websocket ---------- */
function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = () => { ws.send(JSON.stringify({ type: "hello", name: nodeName, kind: KIND })); clockBurst(); };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "welcome") me = m.you;
    else if (m.type === "pong") {
      const rtt = Date.now() - m.client_ms;
      // keep only low-jitter samples: accept if better RTT than what we
      // have, or our best sample is stale (>90s)
      if (rtt <= clockRtt || Date.now() - clockAt > 90000) {
        clockOffset = m.server_ms + rtt / 2 - Date.now();
        clockRtt = rtt; clockAt = Date.now();
      }
    } else if (m.type === "state" || m.type === "action") {
      S = m.state;
      render(m.type === "action" ? m.action : null);
    } else if (m.type === "nodes") {
      nodes = m.nodes;
      if (S) { S.active_node = m.active_node; S.party = m.party; }
      renderNodes(); render(null);
    }
  };
  ws.onclose = () => setTimeout(connect, 1500);
}
function syncClock() { if (ws?.readyState === 1) ws.send(JSON.stringify({ type: "ping", client_ms: Date.now() })); }
/* clock burst: 5 quick samples at connect, keep the lowest-RTT one */
function clockBurst() { for (let i = 0; i < 5; i++) setTimeout(syncClock, 200 + i * 350); }
setInterval(syncClock, 15000);
function act(action, payload = {}) { if (ws?.readyState === 1) ws.send(JSON.stringify({ type: "action", action, payload })); }

/* ---------- playback ---------- */
function iAmAudible() {
  if (!S) return false;
  if (S.party) return false; // party = snapcast engine owns ALL sound; deck audio muted
  return S.active_node === me?.id;
}
function srcFor(st) {
  if (st.station) return `/api/stations/listen?url=${encodeURIComponent(st.station.url)}`;
  return st.track ? `/api/stream/${st.track.id}` : "";
}
function serverPos() {
  if (!S) return 0;
  return S.playing ? S.position_s + (Date.now() + clockOffset - S.epoch_ms) / 1000 : S.position_s;
}
async function reconcileAudio() {
  if (!S) return;
  const want = iAmAudible() ? srcFor(S) : "";
  if (!want) {
    if (!player.paused) player.pause();
    if (lastLoadedSrc) { player.removeAttribute("src"); player.load(); lastLoadedSrc = ""; }
    return;
  }
  if (want !== lastLoadedSrc) { lastLoadedSrc = want; player.src = want; player.load(); }
  player.volume = S.volume;
  if (S.playing) {
    if (!S.station) {
      const target = serverPos();
      if (Math.abs(player.currentTime - target) > 1.5) { try { player.currentTime = Math.max(0, target); } catch {} }
    }
    if (player.paused) {
      try { await player.play(); armHide(); } catch { armShow(); }
    }
  } else if (!player.paused) player.pause();
}

/* party drift corrector removed — snapcast owns party sync now (chunk
   scheduling against its own clock protocol, far tighter than we can do
   with an HTML <audio> element) */

/* ---- audio arming: browsers block play() without a gesture; give joining
   nodes (phone/iPad in party mode) a big obvious tap target ---- */
function armShow() {
  if ($("#arm-overlay")) return;
  const d = document.createElement("div");
  d.id = "arm-overlay";
  d.style.cssText = "position:fixed;inset:0;z-index:999;display:grid;place-items:center;background:color-mix(in srgb, var(--bg, #0000f2) 88%, transparent);cursor:pointer;backdrop-filter:blur(4px)";
  d.innerHTML = `<div style="text-align:center;font-family:inherit"><div class="display" style="font-size:2rem;letter-spacing:2px">TAP TO JOIN THE SOUND</div><p class="mono dim" style="margin-top:.6rem">this screen becomes a speaker</p></div>`;
  d.onclick = async () => { try { await player.play(); } catch {} armHide(); reconcileAudio(); };
  document.body.appendChild(d);
}
function armHide() { $("#arm-overlay")?.remove(); }

player.addEventListener("ended", () => {
  // only the ACTIVE node advances the queue — in party mode every node
  // fires `ended`; without this gate N nodes = N skips
  if (S?.active_node !== me?.id || !S?.track) return;
  if (S.queue.length) act("next");
  else if (S.radio_seed) refillRadio(true);
});
setInterval(() => {
  // active node is the position source of truth (server ignores the rest)
  if (S?.active_node === me?.id && S?.track && !player.paused) act("position_report", { position_s: player.currentTime, playing: !player.paused });
}, 5000);

async function refillRadio(playNow) {
  if (!S?.radio_seed) return;
  try {
    const r = await fetch(`/api/radio/${S.radio_seed}`).then((r) => r.json());
    if (r.tracks?.length) {
      if (playNow) act("play_track", { track: r.tracks[0], queue: r.tracks.slice(1), radio_seed: S.radio_seed });
      else act("queue_set", { queue: [...S.queue, ...r.tracks] });
    }
  } catch {}
}

/* ---------- render ---------- */
function fmt(s) { if (s == null || isNaN(s)) return "–:––"; s = Math.max(0, Math.floor(s)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; }
function toast(msg) {
  const t = $("#toast"); t.textContent = msg.toUpperCase(); t.classList.add("on");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("on"), 2600);
}
function tickerUpdate() {
  const bits = [];
  if (S?.track) bits.push(`NOW ▸ ${S.track.title} — ${S.track.artist}`);
  if (S?.station) bits.push(`LIVE ▸ ${S.station.name}`);
  bits.push(`${nodes.length} NODE${nodes.length === 1 ? "" : "S"} ONLINE`);
  if (S?.party) bits.push("PARTY MODE ACTIVE");
  if (S?.queue?.length) bits.push(`${S.queue.length} QUEUED`);
  if (S?.vinyl?.title) bits.push(`LAST VINYL ▸ ${S.vinyl.title}`);
  bits.push("ORPHEUS SOUND SYSTEM · LOCAL FIRST · NO ADS · NO SUITS");
  $("#ticker-inner").textContent = bits.join("   ///   ") + "   ///   ";
}

function render(action) {
  if (!S) return;
  renderPartyTune();
  document.body.classList.toggle("playing", S.playing);
  const t = S.track, st = S.station;
  if (st) {
    $("#now-context").textContent = "LIVE RADIO";
    $("#now-title").textContent = st.name;
    $("#now-artist").textContent = [st.tags?.split(",")[0], st.country].filter(Boolean).join(" · ").toUpperCase();
    setArt(st.art);
    $("#art-caption").innerHTML = `<span>SIG LIVE</span><span>${(st.codec || "?")} ${st.bitrate || ""}K</span>`;
  } else if (t) {
    $("#now-context").textContent = (S.radio_seed ? "SONG RADIO ∞ · " : "") + (t.album || "SINGLE").toUpperCase();
    $("#now-title").textContent = t.title;
    $("#now-artist").textContent = (t.artist || "").toUpperCase();
    setArt(t.art);
    $("#art-caption").innerHTML = `<span>TRK ${t.id?.slice(0, 6) || "——"}</span><span>${t.guest ? "GUEST PICK" : ""}</span>`;
  } else {
    $("#now-context").textContent = "STANDBY";
    $("#now-title").innerHTML = "SILENCE,<br/>FOR NOW";
    $("#now-artist").textContent = "search a song · tune the dial · flip to vinyl";
    setArt(null);
    $("#art-caption").innerHTML = `<span>TRK ——</span>`;
  }
  $("#btn-play").innerHTML = S.playing ? "PAUSE&nbsp;❚❚" : "PLAY&nbsp;▶";
  $("#vol").value = S.volume;
  $("#vol-pct").textContent = Math.round(S.volume * 100);
  const active = nodes.find((n) => n.id === S.active_node);
  $("#active-on").textContent = S.party
    ? `▲ PARTY — ${nodes.length} NODES IN SYNC`
    : active ? `▲ SOUNDING ON ${active.name.toUpperCase()}${active.id === me?.id ? " (HERE)" : ""}` : "";
  renderQueue();
  tickerUpdate();
  if (S.vinyl && S.mode === "vinyl") renderVinyl(S.vinyl);
  if (action === "vinyl_result") go("vinyl");
  if (t && lyricsOn && lyricsFor !== t.id) loadLyrics(t);
  if (!t) hideLyrics();
  reconcileAudio();
}
function setArt(url) {
  const img = $("#disc-art");
  if (url) { img.src = url; document.body.classList.add("has-art"); $("#artwash").style.backgroundImage = `url("${url}")`; }
  else { img.removeAttribute("src"); document.body.classList.remove("has-art"); $("#artwash").style.backgroundImage = ""; }
}

/* global art fallback: any dead thumbnail (stale yt3 URL, 404) degrades to a
   clean placeholder glyph instead of the browser's broken-image icon */
document.addEventListener("error", (e) => {
  const img = e.target;
  if (!(img instanceof HTMLImageElement) || img.dataset.ph) return;
  if (!img.getAttribute("src")) return;
  img.dataset.ph = "1";
  img.style.background = "var(--ink-soft, #1a1a2e)";
  img.style.objectFit = "none";
  img.removeAttribute("src");
}, true);

/* ---------- generative field (hermes-style background) ----------
   drifting contour waves + sparse glyph rain in acid, audio-reactive */
const field = $("#field");
const fctx = field.getContext("2d");
let analyser = null, audioCtx = null, fftBuf = null;
function armAnalyser() {
  if (analyser || !window.AudioContext) return;
  try {
    audioCtx = new AudioContext();
    const src = audioCtx.createMediaElementSource(player);
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 128;
    fftBuf = new Uint8Array(analyser.frequencyBinCount);
    src.connect(analyser);
    analyser.connect(audioCtx.destination);
  } catch { analyser = null; }
}
player.addEventListener("play", () => { armAnalyser(); audioCtx?.resume(); }, { once: false });

const GLYPHS = "|/\\<>=+~-·:▏▎▍".split("");
let drops = [];
function sizeField() {
  field.width = innerWidth; field.height = innerHeight;
  drops = Array.from({ length: Math.max(10, innerWidth / 90 | 0) }, () => ({
    x: Math.random() * innerWidth, y: Math.random() * innerHeight,
    v: 0.3 + Math.random() * 0.9, g: GLYPHS[Math.random() * GLYPHS.length | 0],
  }));
}
addEventListener("resize", sizeField); sizeField();
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
let ft = 0;
function drawField() {
  requestAnimationFrame(drawField);
  if (reduceMotion) return;
  ft += 0.004;
  fctx.clearRect(0, 0, field.width, field.height);
  const acid = getComputedStyle(document.body).getPropertyValue("--acid").trim() || "#EDFF45";
  const ink = getComputedStyle(document.body).getPropertyValue("--ink").trim() || "#F5F5F5";
  let energy = 0;
  if (analyser && !player.paused) {
    analyser.getByteFrequencyData(fftBuf);
    energy = fftBuf.reduce((a, b) => a + b, 0) / fftBuf.length / 255;
  }
  /* contour waves across lower half */
  const H = field.height, W = field.width;
  for (let li = 0; li < 4; li++) {
    fctx.beginPath();
    const base = H * (0.62 + li * 0.1);
    for (let x = 0; x <= W; x += 8) {
      const y = base
        + Math.sin(x * 0.006 + ft * (1.5 + li * 0.5) + li * 2) * (14 + li * 8)
        + Math.sin(x * 0.017 - ft * 2.2) * 6
        - energy * 60 * Math.sin(x * 0.01 + ft * 4 + li);
      x === 0 ? fctx.moveTo(x, y) : fctx.lineTo(x, y);
    }
    fctx.strokeStyle = li === 1 ? acid : ink;
    fctx.globalAlpha = li === 1 ? 0.20 + energy * 0.3 : 0.06;
    fctx.lineWidth = 1;
    fctx.stroke();
  }
  /* sparse glyph rain */
  fctx.font = "12px 'Space Mono', monospace";
  drops.forEach((d) => {
    d.y += d.v * (1 + energy * 5);
    if (d.y > H + 20) { d.y = -20; d.x = Math.random() * W; d.g = GLYPHS[Math.random() * GLYPHS.length | 0]; }
    fctx.globalAlpha = 0.10 + energy * 0.22;
    fctx.fillStyle = Math.random() < 0.06 ? acid : ink;
    fctx.fillText(d.g, d.x, d.y);
  });
  fctx.globalAlpha = 1;
}
drawField();
function renderQueue() {
  const q = $("#queue");
  $("#queue-n").textContent = S.queue?.length ? `· ${S.queue.length}` : "";
  q.innerHTML = "";
  (S.queue || []).slice(0, 30).forEach((tr, i) => {
    const c = card(tr, () => {
      act("play_track", { track: tr, queue: S.queue.slice(i + 1), radio_seed: S.radio_seed });
    });
    if (tr.guest) c.querySelector(".c-a").insertAdjacentHTML("beforebegin", `<div class="c-g">GUEST</div>`);
    q.appendChild(c);
  });
  if (!S.queue?.length) q.innerHTML = `<p class="mono dim small">QUEUE EMPTY — ∞ STARTS A SONG RADIO</p>`;
}
function card(tr, onclick) {
  const el = document.createElement("div");
  el.className = "card";
  el.innerHTML = `<img loading="lazy" src="${tr.art || ""}" alt=""/><div class="c-t"></div><div class="c-a"></div>`;
  el.querySelector(".c-t").textContent = tr.title || tr.name || "";
  el.querySelector(".c-a").textContent = tr.artist || "";
  el.onclick = onclick;
  return el;
}

/* ticker + progress */
setInterval(() => {
  if (!S || userSeeking) return;
  const dur = S.track?.duration;
  const pos = iAmAudible() && S.track && !player.paused ? player.currentTime : serverPos();
  $("#t-cur").textContent = S.station ? "LIVE" : fmt(pos);
  $("#t-dur").textContent = S.station ? "∞" : fmt(dur);
  $("#seek").value = dur ? Math.min(100, (pos / dur) * 100) : 0;
  if (lyricsOn && lyricsData?.synced) paintLyrics(pos);
}, 400);

/* ---------- transport ---------- */
$("#btn-play").onclick = () => { player.play().catch(() => {}); (S?.track || S?.station) ? act("toggle_play") : openSearch(); };
$("#btn-next").onclick = () => (S?.queue?.length ? act("next") : refillRadio(true));
$("#btn-prev").onclick = () => act("prev");
$("#vol").oninput = (e) => act("volume", { volume: +e.target.value });
$("#seek").addEventListener("pointerdown", () => (userSeeking = true));
$("#seek").addEventListener("change", (e) => {
  userSeeking = false;
  const dur = S?.track?.duration;
  if (dur) act("seek", { position_s: (+e.target.value / 100) * dur });
});
$("#btn-radio-seed").onclick = () => {
  if (S?.track) { act("play_track", { track: S.track, queue: [], radio_seed: S.track.id }); refillRadio(false); toast("SONG RADIO ∞"); }
};
$("#btn-clear-queue").onclick = () => act("queue_set", { queue: [] });

/* ---------- lyrics ---------- */
$("#btn-lyrics").onclick = () => {
  lyricsOn = !lyricsOn;
  $("#btn-lyrics").classList.toggle("on", lyricsOn);
  if (lyricsOn && S?.track) loadLyrics(S.track); else hideLyrics();
};
async function loadLyrics(t) {
  lyricsFor = t.id;
  $("#lyrics-panel").classList.remove("hidden");
  $("#lyrics-scroll").innerHTML = `<p class="mono">SEARCHING THE ARCHIVE…</p>`;
  const p = new URLSearchParams({ title: t.title || "", artist: t.artist || "", album: t.album || "" });
  if (t.duration) p.set("duration", t.duration);
  lyricsData = await fetch(`/api/lyrics?${p}`).then((r) => r.json()).catch(() => null);
  const box = $("#lyrics-scroll");
  box.innerHTML = "";
  if (lyricsData?.synced) {
    lyricsData.synced.forEach((l, i) => {
      const el = document.createElement("p");
      el.textContent = l.text || "· · ·";
      el.dataset.i = i;
      box.appendChild(el);
    });
  } else if (lyricsData?.plain) {
    lyricsData.plain.split("\n").forEach((line) => {
      const el = document.createElement("p"); el.textContent = line || " "; box.appendChild(el);
    });
  } else box.innerHTML = `<p class="mono">NO LYRICS IN THE ARCHIVE FOR THIS ONE</p>`;
}
function hideLyrics() { $("#lyrics-panel").classList.add("hidden"); lyricsData = null; lyricsFor = null; }
function paintLyrics(pos) {
  const lines = lyricsData.synced;
  let live = -1;
  for (let i = 0; i < lines.length; i++) if (lines[i].t <= pos + 0.2) live = i; else break;
  $$("#lyrics-scroll p").forEach((el, i) => {
    el.classList.toggle("live", i === live);
    el.classList.toggle("past", i < live);
  });
  const el = $(`#lyrics-scroll p[data-i="${live}"]`);
  if (el && lyricsOn) el.scrollIntoView({ block: "center", behavior: "smooth" });
}

/* ---------- nav ---------- */
function go(mode) {
  $$("#modes button").forEach((b) => b.classList.toggle("on", b.dataset.go === mode));
  $$(".view").forEach((v) => v.classList.remove("on"));
  $(`#view-${mode}`).classList.add("on");
  document.body.dataset.mode = mode;
  if (mode === "radio" && !$("#station-shelves").childElementCount) loadStations();
  if (mode === "library") loadLibrary();
  if (mode === "vault") loadVault();
}
$$("#modes button").forEach((b) => (b.onclick = () => go(b.dataset.go)));

/* ---------- search ---------- */
let searchKind = "songs", searchTimer;
function openSearch() { $("#search-overlay").classList.add("on"); $("#q").focus(); }
$("#btn-search").onclick = openSearch;
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") $$(".overlay").forEach((o) => o.classList.remove("on"));
  if (e.key === "/" && !e.target.matches("input,textarea")) { e.preventDefault(); openSearch(); }
  if (e.key === " " && !e.target.matches("input,button,textarea")) { e.preventDefault(); act("toggle_play"); }
});
$$("[data-close]").forEach((b) => (b.onclick = () => b.closest(".overlay").classList.remove("on")));
$$("#search-kinds button").forEach((b) => (b.onclick = () => {
  searchKind = b.dataset.kind;
  $$("#search-kinds button").forEach((x) => x.classList.toggle("on", x === b));
  doSearch();
}));
$("#q").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(doSearch, 350); });
async function doSearch() {
  const q = $("#q").value.trim();
  if (!q) return ($("#results").innerHTML = "");
  const { results } = await fetch(`/api/search?q=${encodeURIComponent(q)}&kind=${searchKind}`).then((r) => r.json());
  const box = $("#results");
  box.innerHTML = "";
  results.forEach((it) => {
    const row = document.createElement("div");
    row.className = "result";
    row.innerHTML = `<img loading="lazy" src="${it.art || ""}"/><div><div class="r-t"></div><div class="r-a"></div></div>`;
    row.querySelector(".r-t").textContent = it.title || it.name;
    row.querySelector(".r-a").textContent = (it.kind === "artist" ? "ARTIST" : (it.artist || it.kind || "")).toUpperCase();
    if (it.id) {
      const add = document.createElement("button");
      add.className = "r-add"; add.textContent = "+"; add.title = "queue";
      add.onclick = (e) => { e.stopPropagation(); act("queue_add", { track: it }); toast("QUEUED"); };
      row.appendChild(add);
      row.onclick = () => { act("play_track", { track: it, radio_seed: null }); closeSearch(); };
    } else if (it.kind === "album") {
      row.onclick = async () => {
        const alb = await fetch(`/api/album/${it.browseId}`).then((r) => r.json());
        if (alb.tracks?.length) { act("play_track", { track: alb.tracks[0], queue: alb.tracks.slice(1), radio_seed: null }); closeSearch(); }
      };
    } else if (it.kind === "artist") {
      row.onclick = async () => {
        const a = await fetch(`/api/artist/${it.browseId}`).then((r) => r.json());
        if (a.songs?.length) { act("play_track", { track: a.songs[0], queue: a.songs.slice(1), radio_seed: a.songs[0].id }); closeSearch(); }
      };
    }
    box.appendChild(row);
  });
}
function closeSearch() { $("#search-overlay").classList.remove("on"); go("deck"); }

/* ---------- shelves + rewind ---------- */
async function loadShelves() {
  try {
    const { shelves } = await fetch("/api/home").then((r) => r.json());
    const box = $("#shelves");
    box.innerHTML = "";
    shelves.slice(0, 4).forEach((sh) => {
      const h = document.createElement("h2");
      h.className = "rule-title mono"; h.textContent = sh.title.toUpperCase();
      const rail = document.createElement("div");
      rail.className = "rail";
      sh.items.filter((i) => i.id).slice(0, 14).forEach((tr) =>
        rail.appendChild(card(tr, () => { act("play_track", { track: tr, radio_seed: tr.id }); refillRadio(false); })));
      if (rail.childElementCount) { box.appendChild(h); box.appendChild(rail); }
    });
  } catch {}
}
async function loadRewind() {
  try {
    const { shelves } = await fetch("/api/rewind").then((r) => r.json());
    const box = $("#rewind-wrap");
    box.innerHTML = "";
    shelves.forEach((sh) => {
      const h = document.createElement("h2");
      h.className = "rule-title mono"; h.textContent = ("REWIND · " + sh.title).toUpperCase();
      const rail = document.createElement("div");
      rail.className = "rail";
      sh.items.forEach((tr) => rail.appendChild(card(tr, () => act("play_track", { track: tr, radio_seed: tr.id }))));
      box.appendChild(h); box.appendChild(rail);
    });
  } catch {}
}

/* ---------- library ---------- */
$("#btn-sources").onclick = () => { $("#sources-overlay").classList.add("on"); srcStatus(); };
$("#lib-back").onclick = () => { $("#lib-detail").classList.add("hidden"); $("#lib-grid").classList.remove("hidden"); };
async function loadLibrary() {
  const { playlists } = await fetch("/api/library").then((r) => r.json());
  const grid = $("#lib-grid");
  grid.innerHTML = playlists.length ? "" : `<p class="mono dim">NO PLAYLISTS YET — CONNECT A SOURCE ↑</p>`;
  playlists.forEach((pl) => {
    const el = document.createElement("div");
    el.className = "pl-card";
    el.innerHTML = `<div class="pl-art"><img loading="lazy" src="${pl.art || ""}"/><span class="pl-src">${pl.source.toUpperCase()}</span></div>
      <div class="pl-n"></div><div class="pl-c">${pl.n} TRACKS</div>`;
    el.querySelector(".pl-n").textContent = pl.name;
    el.onclick = () => openPlaylist(pl.id);
    grid.appendChild(el);
  });
}
let currentPl = null;
async function openPlaylist(pid) {
  currentPl = await fetch(`/api/library/${pid}`).then((r) => r.json());
  $("#lib-grid").classList.add("hidden");
  $("#lib-detail").classList.remove("hidden");
  $("#lib-title").textContent = currentPl.name;
  $("#lib-meta").textContent = `${currentPl.source.toUpperCase()} · ${currentPl.tracks.length} TRACKS`;
  const box = $("#lib-tracks");
  box.innerHTML = "";
  currentPl.tracks.forEach((t, i) => {
    const row = document.createElement("div");
    row.className = "t-row";
    row.innerHTML = `<span class="t-i mono">${String(i + 1).padStart(2, "0")}</span><img loading="lazy" src="${t.art || ""}"/>
      <div><div class="t-t"></div><div class="t-a"></div></div><span class="t-d mono">${fmt(t.duration)}</span>`;
    row.querySelector(".t-t").textContent = t.title;
    row.querySelector(".t-a").textContent = (t.artist || "").toUpperCase();
    row.onclick = () => playFromPlaylist(i);
    box.appendChild(row);
  });
}
async function resolveTrack(t, pid) {
  if (t.id) return t;
  const r = await fetch(`/api/library/${pid}/resolve/${t.pos}`, { method: "POST" });
  if (!r.ok) throw new Error("no match");
  const { id } = await r.json();
  return { ...t, id };
}
async function playFromPlaylist(idx) {
  const pid = currentPl.id;
  toast("MATCHING…");
  try {
    const first = await resolveTrack(currentPl.tracks[idx], pid);
    const rest = currentPl.tracks.slice(idx + 1, idx + 40);
    act("play_track", { track: first, queue: [], radio_seed: null });
    go("deck");
    // resolve the next tracks in background, then set queue
    const resolved = [];
    for (const t of rest) {
      try { resolved.push(await resolveTrack(t, pid)); } catch {}
      if (resolved.length && resolved.length % 5 === 0) act("queue_set", { queue: resolved.slice() });
    }
    act("queue_set", { queue: resolved });
  } catch { toast("NO PLAYABLE MATCH"); }
}
$("#lib-playall").onclick = () => playFromPlaylist(0);

/* sources overlay */
async function srcStatus() {
  const st = await fetch("/api/sources/status").then((r) => r.json());
  for (const k of ["spotify", "apple", "ytmusic"]) $(`#st-${k}`).textContent = st[k] ? "● LINKED" : "○ NOT LINKED";
}
$$("[data-link]").forEach((b) => (b.onclick = async () => {
  const src = b.dataset.link;
  const val = $(`#in-${src}`).value.trim();
  if (!val) return toast("PASTE THE TOKEN FIRST");
  $("#src-status").textContent = "LINKING…";
  const body = src === "apple" ? { media_user_token: val } : { headers: val };
  const r = await fetch(`/api/sources/${src}/link`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  $("#src-status").textContent = r.ok ? `${src.toUpperCase()} LINKED ✓` : (j.detail || "LINK FAILED");
  srcStatus();
}));

/* Spotify — official OAuth PKCE: save Client ID, open consent popup, wait for callback */
$("#sp-login").onclick = async () => {
  const cid = $("#in-spotify").value.trim();
  if (!cid) return toast("PASTE YOUR SPOTIFY CLIENT ID FIRST");
  $("#src-status").textContent = "CONFIGURING…";
  const cfg = await fetch("/api/sources/spotify/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ client_id: cid }) });
  if (!cfg.ok) { const e = await cfg.json().catch(() => ({})); return void ($("#src-status").textContent = e.detail || "BAD CLIENT ID"); }
  const { url } = await fetch("/api/sources/spotify/login").then((r) => r.json());
  $("#src-status").textContent = "AWAITING SPOTIFY CONSENT… (popup)";
  const pop = window.open(url, "orpheus_spotify", "width=520,height=720");
  const onMsg = (ev) => {
    if (ev.data && "orpheus_spotify" in ev.data) {
      window.removeEventListener("message", onMsg);
      $("#src-status").textContent = ev.data.orpheus_spotify ? "SPOTIFY LINKED ✓ — now hit IMPORT" : "LINK FAILED — TRY AGAIN";
      srcStatus();
    }
  };
  window.addEventListener("message", onMsg);
  // fallback: poll status in case popup blockers eat postMessage
  let tries = 0;
  const poll = setInterval(async () => {
    if (++tries > 60 || (pop && pop.closed)) {
      clearInterval(poll);
      const st = await fetch("/api/sources/status").then((r) => r.json());
      if (st.spotify) { $("#src-status").textContent = "SPOTIFY LINKED ✓ — now hit IMPORT"; srcStatus(); }
    }
  }, 2000);
};
$$("[data-import]").forEach((b) => (b.onclick = async () => {
  const src = b.dataset.import;
  $("#src-status").textContent = "IMPORTING… (can take a minute)";
  const r = await fetch(`/api/sources/${src}/import`, { method: "POST" });
  const j = await r.json().catch(() => ({}));
  if (r.ok) {
    const n = (j.imported || []).reduce((a, p) => a + p.tracks, 0);
    $("#src-status").textContent = `IMPORTED ${j.imported.length} PLAYLISTS · ${n} TRACKS ✓`;
    loadLibrary();
  } else $("#src-status").textContent = j.detail || "IMPORT FAILED";
}));

/* ---------- radio ---------- */
async function loadStations() {
  const box = $("#station-shelves");
  box.innerHTML = `<p class="mono dim">TUNING…</p>`;
  const { shelves } = await fetch("/api/stations/featured").then((r) => r.json());
  box.innerHTML = "";
  shelves.forEach((sh) => {
    const h = document.createElement("h2"); h.textContent = sh.tag;
    const grid = document.createElement("div"); grid.className = "st-grid";
    sh.stations.forEach((st) => grid.appendChild(stationRow(st)));
    box.appendChild(h); box.appendChild(grid);
  });
}
function stationRow(st) {
  const el = document.createElement("div");
  el.className = "station" + (S?.station?.uuid === st.uuid ? " live" : "");
  el.innerHTML = `${st.art ? `<img loading="lazy" src="${st.art}" onerror="this.outerHTML='<div class=st-ph>◈</div>'"/>` : `<div class="st-ph">◈</div>`}
    <div style="min-width:0"><div class="st-n"></div><div class="st-m"></div></div>`;
  el.querySelector(".st-n").textContent = st.name.toUpperCase();
  el.querySelector(".st-m").textContent = [st.codec, st.bitrate ? st.bitrate + "K" : "", st.country].filter(Boolean).join(" · ").toUpperCase();
  el.onclick = () => { act("station", { station: st }); toast(`● ${st.name}`); $$(".station.live").forEach((x) => x.classList.remove("live")); el.classList.add("live"); };
  return el;
}
let stTimer;
$("#station-q").addEventListener("input", (e) => {
  clearTimeout(stTimer);
  stTimer = setTimeout(async () => {
    const q = e.target.value.trim();
    if (!q) return loadStations();
    const { stations } = await fetch(`/api/stations/search?q=${encodeURIComponent(q)}`).then((r) => r.json());
    const box = $("#station-shelves");
    box.innerHTML = "<h2>RESULTS</h2>";
    const grid = document.createElement("div"); grid.className = "st-grid";
    stations.forEach((st) => grid.appendChild(stationRow(st)));
    box.appendChild(grid);
  }, 400);
});

/* ---------- vinyl ---------- */
async function listen() {
  const status = $("#vinyl-status"), btn = $("#btn-listen");
  $("#vinyl-result").classList.add("hidden");
  $("#vinyl-idle").classList.remove("hidden");
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const mime = MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : "audio/mp4";
    const rec = new MediaRecorder(stream, { mimeType: mime });
    const chunks = [];
    rec.ondataavailable = (e) => chunks.push(e.data);
    rec.start();
    btn.classList.add("rec"); btn.textContent = "◉ LISTENING…";
    let n = 8;
    status.textContent = `RECORDING ${n}S`;
    const tick = setInterval(() => { status.textContent = `RECORDING ${--n}S`; }, 1000);
    await new Promise((res) => setTimeout(res, 8000));
    clearInterval(tick);
    rec.stop();
    await new Promise((res) => (rec.onstop = res));
    stream.getTracks().forEach((t) => t.stop());
    btn.classList.remove("rec"); btn.textContent = "◉ LISTEN — 8 SEC";
    status.textContent = "CONSULTING THE ORACLE…";
    const fd = new FormData();
    fd.append("audio", new Blob(chunks, { type: mime }), mime.includes("webm") ? "clip.webm" : "clip.mp4");
    const r = await fetch("/api/vinyl/recognize", { method: "POST", body: fd }).then((r) => r.json());
    status.textContent = r.match ? "" : "NO MATCH — GET CLOSER TO THE SPEAKER";
  } catch (err) {
    btn.classList.remove("rec"); btn.textContent = "◉ LISTEN — 8 SEC";
    status.textContent = err.name === "NotAllowedError" ? "MIC PERMISSION DENIED" : `MIC ERROR: ${err.message}`;
  }
}
$("#btn-listen").onclick = listen;
$("#btn-listen-again").onclick = listen;
function renderVinyl(v) {
  $("#vinyl-idle").classList.add("hidden");
  $("#vinyl-result").classList.remove("hidden");
  $("#vinyl-art").src = v.art || "";
  $("#vinyl-title").textContent = v.title || "?";
  $("#vinyl-artist").textContent = (v.artist || "").toUpperCase();
  $("#vinyl-album").textContent = v.album ? `FROM “${v.album.toUpperCase()}”` : "";
  $("#vinyl-extra").textContent = [v.year, v.label].filter(Boolean).join(" · ").toUpperCase();
}

/* ---------- vault ---------- */
async function loadVault() {
  const d = await fetch("/api/vault").then((r) => r.json());
  const gb = (d.bytes / 1e9).toFixed(2);
  const hrs = (d.seconds / 3600).toFixed(1);
  $("#vault-stats").textContent = `${d.count} TRACKS ARCHIVED · ${hrs} HOURS · ${gb} GB · YOURS FOREVER`;
  const box = $("#vault-list");
  box.innerHTML = d.tracks.length ? "" : `<p class="mono dim" style="padding:1rem 0">PLAY SOMETHING — IT LANDS HERE AUTOMATICALLY</p>`;
  d.tracks.forEach((t, i) => {
    const row = document.createElement("div");
    row.className = "t-row";
    row.innerHTML = `<span class="t-i mono">${String(i + 1).padStart(3, "0")}</span><img loading="lazy" src="${t.art || ""}"/>
      <div><div class="t-t"></div><div class="t-a"></div></div><span class="vaulted mono">● VAULTED</span><span class="t-d mono">${fmt(t.duration)}</span>`;
    row.querySelector(".t-t").textContent = t.title;
    row.querySelector(".t-a").textContent = (t.artist || "").toUpperCase();
    row.onclick = () => { act("play_track", { track: t, radio_seed: null }); go("deck"); };
    box.appendChild(row);
  });
}

/* ---------- nodes ---------- */
$("#btn-nodes").onclick = () => { $("#nodes-overlay").classList.add("on"); loadAlexa(); };
const KIND_LABEL = { pc: "PC", phone: "PHONE", tablet: "TABLET", dlna: "WIFI", spotify: "FRAME", other: "NODE" };
function renderNodes() {
  $("#node-count").textContent = nodes.length;
  const list = $("#node-list");
  list.innerHTML = "";
  nodes.forEach((n) => {
    const row = document.createElement("div");
    row.className = "node-row" + (S?.active_node === n.id && !S?.party ? " active" : "");
    row.innerHTML = `<span class="n-kind">${KIND_LABEL[n.kind] || "NODE"}</span><span></span>`;
    row.children[1].textContent = n.name.toUpperCase() + (n.id === me?.id ? " · YOU" : "");
    if (S?.active_node === n.id && !S?.party) row.insertAdjacentHTML("beforeend", `<span class="n-badge">◉ SOUNDING</span>`);
    row.onclick = () => { act("set_active", { node_id: n.id }); toast(`SOUND → ${n.name}`); };
    list.appendChild(row);
  });
  if ($("#party-toggle")) $("#party-toggle").checked = !!S?.party;
  renderPartyTune();
  tickerUpdate();
}
$("#party-toggle").onchange = (e) => act("party", { on: e.target.checked });

/* ---------- party engine (snapcast) : THE SKY ---------- */
/* Every speaker is a star. Size = volume. Color = role (band):
   FULL = whole song · THUMP = bass · HEART = vocals/mids · AIR = highs.
   Drag stars to draw your constellation (saved per device). Tap = tune. */
let snapPoll = null, skyClients = [], skyDrag = null, cardFor = null;
const BAND_META = {
  FULL:  { color: "#F5F5F5", label: "FULL", desc: "the whole song" },
  THUMP: { color: "#FF5C39", label: "THUMP", desc: "bass only — subwoofer duty" },
  HEART: { color: "#EDFF45", label: "HEART", desc: "vocals & instruments" },
  AIR:   { color: "#5CD6FF", label: "AIR", desc: "sparkle & highs" },
};
const skyPos = JSON.parse(localStorage.getItem("orpheus-sky") || "{}");

function renderPartyTune() {
  const box = $("#party-tune");
  if (!box) return;
  box.classList.toggle("hidden", !S?.party);
  if (S?.party && !snapPoll) { refreshSnap(); snapPoll = setInterval(refreshSnap, 4000); }
  if (!S?.party && snapPoll) { clearInterval(snapPoll); snapPoll = null; closeStarCard(); }
}

async function refreshSnap() {
  const st = await fetch("/api/party/status").then((r) => r.json()).catch(() => ({ up: false, clients: [] }));
  $("#party-status").textContent = !st.up
    ? "ENGINE DOWN — run: docker compose -f party/docker-compose.yml up -d"
    : `ENGINE ${st.feeding ? "LIVE — FEEDING THE SKY" : "UP · IDLE (press play)"} · ${st.clients.filter((c) => c.connected).length} STARS LIT`;
  skyClients = st.clients || [];
  if (!skyDrag) drawSky();   // don't fight a drag in progress
}

function starXY(c, i, n) {
  if (skyPos[c.id]) return skyPos[c.id];
  // first sighting: place on a loose arc so the sky never starts as a blob
  const a = (i / Math.max(n, 1)) * Math.PI * 1.4 + 0.9;
  return { x: 320 + Math.cos(a) * (170 + (i % 3) * 42), y: 210 + Math.sin(a) * (120 + (i % 2) * 38) };
}

function drawSky() {
  const svg = $("#sky");
  if (!svg) return;
  const N = skyClients.length;
  const pts = skyClients.map((c, i) => ({ c, ...starXY(c, i, N) }));
  let h = `<defs>
    <filter id="glow" x="-80%" y="-80%" width="260%" height="260%">
      <feGaussianBlur stdDeviation="4.5" result="b"/>
      <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
  </defs>`;
  // backdrop dust (deterministic pseudo-random so it doesn't flicker)
  for (let i = 0; i < 44; i++) {
    const r = Math.sin(i * 127.1) * 0.5 + 0.5, q = Math.sin(i * 311.7) * 0.5 + 0.5;
    h += `<circle class="dust" cx="${(r * 640).toFixed(0)}" cy="${(q * 420).toFixed(0)}" r="${(0.6 + q).toFixed(1)}" style="animation-delay:${(r * 5).toFixed(1)}s"/>`;
  }
  // constellation lines: chain stars by proximity (greedy nearest walk)
  if (pts.length > 1) {
    const left = [...pts]; let cur = left.shift();
    while (left.length) {
      left.sort((a, b) => (a.x - cur.x) ** 2 + (a.y - cur.y) ** 2 - ((b.x - cur.x) ** 2 + (b.y - cur.y) ** 2));
      const nxt = left.shift();
      h += `<line class="cline" x1="${cur.x}" y1="${cur.y}" x2="${nxt.x}" y2="${nxt.y}"/>`;
      cur = nxt;
    }
  }
  const playing = S?.playing && S?.party;
  pts.forEach(({ c, x, y }, i) => {
    const meta = BAND_META[c.band] || BAND_META.FULL;
    const r = 7 + (c.volume / 100) * 15;                       // size = volume
    const dim = !c.connected || c.muted;
    h += `<g class="star${dim ? " dead" : ""}${playing && !dim ? " beat" : ""}" data-cid="${c.id}"
            transform="translate(${x},${y})" style="animation-delay:${(i * 0.7) % 3}s">
      <circle class="halo" r="${(r * 2.1).toFixed(1)}" fill="${meta.color}"/>
      <circle class="core" r="${r.toFixed(1)}" fill="${meta.color}" filter="url(#glow)"/>
      <path class="spike" d="M0 ${-r - 7} L0 ${r + 7} M${-r - 7} 0 L${r + 7} 0" stroke="${meta.color}"/>
      <text class="star-name" y="${r + 16}">${(c.name || c.id).toUpperCase().slice(0, 18)}</text>
      <text class="star-band" y="${r + 27}" fill="${meta.color}">${meta.label}${dim ? " · OFFLINE" : ""}</text>
    </g>`;
  });
  if (!pts.length) h += `<text class="sky-empty" x="320" y="200">AN EMPTY SKY — ADD SPEAKERS BELOW</text>`;
  svg.innerHTML = h;
  svg.querySelectorAll("g.star").forEach((g) => {
    g.addEventListener("pointerdown", (e) => startStarDrag(e, g));
  });
}

/* drag to reposition · tap (no movement) to open the tuner card */
function startStarDrag(e, g) {
  e.preventDefault();
  const cid = g.dataset.cid, svg = $("#sky");
  const toSky = (ev) => {
    const m = svg.getScreenCTM().inverse(), p = new DOMPoint(ev.clientX, ev.clientY).matrixTransform(m);
    return { x: Math.max(20, Math.min(620, p.x)), y: Math.max(20, Math.min(380, p.y)) };
  };
  const start = toSky(e);
  skyDrag = { cid, moved: false };
  const move = (ev) => {
    const p = toSky(ev);
    if (Math.hypot(p.x - start.x, p.y - start.y) > 6) skyDrag.moved = true;
    if (skyDrag.moved) {
      skyPos[cid] = p;
      g.setAttribute("transform", `translate(${p.x},${p.y})`);
    }
  };
  const up = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    const wasTap = !skyDrag.moved;
    skyDrag = null;
    if (wasTap) openStarCard(cid);
    else { localStorage.setItem("orpheus-sky", JSON.stringify(skyPos)); drawSky(); }
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
}

function closeStarCard() { cardFor = null; $("#star-card")?.classList.add("hidden"); }

function openStarCard(cid) {
  const c = skyClients.find((x) => x.id === cid);
  if (!c) return;
  cardFor = cid;
  const meta = BAND_META[c.band] || BAND_META.FULL;
  const card = $("#star-card");
  card.innerHTML = `
    <div class="sc-head mono">${(c.name || c.id).toUpperCase().slice(0, 22)}
      <button class="chip tiny" id="sc-close">✕</button></div>
    <div class="mono dim small">${c.connected ? "shining" : "offline"} · role: ${meta.desc}</div>
    <label class="mono small">BRIGHTNESS (VOLUME) <span id="sc-vol-val">${c.volume}</span>
      <input type="range" id="sc-vol" min="0" max="100" value="${c.volume}"/></label>
    <div class="mono small" style="margin-top:.45rem">ROLE — what this star sings</div>
    <div class="sc-bands">${Object.entries(BAND_META).map(([b, m]) =>
      `<button class="chip sc-band${b === c.band ? " on" : ""}" data-band="${b}"
        style="--bandc:${m.color}">${m.label}<span>${m.desc}</span></button>`).join("")}</div>
    <label class="mono small" style="margin-top:.45rem">TIMING TRIM <span id="sc-lat-val">${c.latency_ms}</span>ms
      <input type="range" id="sc-lat" min="-500" max="500" step="10" value="${c.latency_ms}"/></label>
    <div class="mono dim small">if this speaker sounds late, go negative (bluetooth ≈ −150)</div>`;
  card.classList.remove("hidden");
  $("#sc-close").onclick = closeStarCard;
  const vol = $("#sc-vol");
  vol.oninput = (ev) => { $("#sc-vol-val").textContent = ev.target.value; };
  vol.onchange = async (ev) => {
    await fetch(`/api/party/client/${encodeURIComponent(cid)}/volume`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ percent: +ev.target.value }) });
    refreshSnap();
  };
  const lat = $("#sc-lat");
  lat.oninput = (ev) => { $("#sc-lat-val").textContent = ev.target.value; };
  lat.onchange = (ev) => fetch(`/api/party/client/${encodeURIComponent(cid)}/latency`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ms: +ev.target.value }) });
  card.querySelectorAll(".sc-band").forEach((b) => {
    b.onclick = async () => {
      b.textContent = "…";
      const r = await fetch(`/api/party/client/${encodeURIComponent(cid)}/band`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ band: b.dataset.band }) });
      if (!r.ok) alert((await r.json().catch(() => ({}))).detail || "failed");
      await refreshSnap();
      openStarCard(cid); // re-render card with new role
    };
  });
}
$("#btn-party-join").onclick = () => {
  // snapweb: the synced browser player bundled with snapserver
  window.open(`http://${location.hostname}:1780/`, "_blank");
};

/* ---- PC outputs: BT speakers / any device on the server PC ---- */
async function refreshOutputs() {
  const box = $("#party-outputs");
  box.innerHTML = `<div class="mono dim small">scanning…</div>`;
  let devs;
  try {
    devs = (await fetch("/api/party/outputs").then((r) => r.json())).devices;
  } catch { box.innerHTML = `<div class="mono dim small">scan failed</div>`; return; }
  box.innerHTML = "";
  devs.filter((d) => !d.skip_hint).forEach((d) => {
    const row = document.createElement("div");
    row.className = "node-row";
    row.innerHTML = `<span class="n-kind">${d.joined ? "◉" : "○"}</span>
      <span class="sc-name"></span>
      <button class="chip${d.joined ? "" : " acid"}">${d.joined ? "LEAVE" : "JOIN"}</button>`;
    row.querySelector(".sc-name").textContent = d.name.toUpperCase();
    row.querySelector("button").onclick = async () => {
      const url = d.joined ? "/api/party/outputs/leave" : "/api/party/outputs/join";
      row.querySelector("button").textContent = "…";
      const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                   body: JSON.stringify({ idx: d.idx, name: d.name }) });
      if (!r.ok) { const e = await r.json().catch(() => ({})); alert(e.detail || "failed"); }
      await refreshOutputs(); await refreshSnap();
    };
    box.appendChild(row);
  });
  if (!box.children.length) box.innerHTML = `<div class="mono dim small">no output devices found</div>`;
}
$("#btn-outputs-scan").onclick = refreshOutputs;
$("#btn-frame-scan").onclick = async () => {
  $("#frame-hint").textContent = "ASKING SPOTIFY FOR CONNECT DEVICES…";
  const r = await fetch("/api/frame/devices");
  const j = await r.json().catch(() => ({}));
  if (r.ok && j.devices?.length) {
    $("#frame-hint").textContent = `FOUND: ${j.devices.map((d) => `${d.name} (${d.type})`).join(", ")} — now in the node list ↑ set it active and press play`;
  } else if (r.status === 428) {
    $("#frame-hint").textContent = "LINK SPOTIFY FIRST — Library → Connect Sources → sp_dc";
  } else {
    $("#frame-hint").textContent = j.detail || "no connect devices visible — is the Frame awake? (play to it once from the Spotify app)";
  }
};
$("#btn-dlna-scan").onclick = async () => {
  $("#dlna-hint").textContent = "SCANNING WIFI (SSDP + MDNS)…";
  const d = await fetch("/api/dlna/scan").then((r) => r.json()).catch(() => ({ renderers: [], mdns_seen: [] }));
  if (d.renderers.length) {
    $("#dlna-hint").textContent = `FOUND: ${d.renderers.map((r) => r.name).join(", ")} — now in the node list ↑`;
  } else if (d.mdns_seen?.length) {
    $("#dlna-hint").textContent = `SEEN ON WIFI: ${d.mdns_seen.map((m) => `${m.name} (${m.ip} · ${m.services.join("/")})`).join(", ")} — but no open DLNA port. try ADD BY IP below, or enable "IP Remote"/network standby on the Frame`;
  } else {
    $("#dlna-hint").textContent = "nothing found — Frame must be ON (not standby) and on this wifi. try ADD BY IP";
  }
};
$("#btn-dlna-add").onclick = async () => {
  const ip = $("#dlna-ip").value.trim();
  if (!ip) return toast("TYPE THE FRAME'S IP");
  $("#dlna-hint").textContent = `PROBING ${ip}…`;
  const r = await fetch("/api/dlna/add", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ip }) });
  const j = await r.json().catch(() => ({}));
  $("#dlna-hint").textContent = r.ok ? `ADDED: ${j.renderers.map((x) => x.name).join(", ")} ↑` : (j.detail || "probe failed");
};

/* Alexa */
async function loadAlexa() {
  const st = await fetch("/api/alexa/status").then((r) => r.json());
  $("#alexa-state").textContent = st.linked ? "· LINKED" : "· NOT LINKED (README)";
  if (st.linked) {
    try {
      const { devices } = await fetch("/api/alexa/devices").then((r) => r.json());
      $("#alexa-devices").textContent = devices.map((d) => d.name).join(" · ") || "no devices";
    } catch { $("#alexa-devices").textContent = "cookie expired — re-link"; }
  }
}
$("#btn-alexa-send").onclick = async () => {
  const text = $("#alexa-text").value.trim();
  if (!text) return;
  const r = await fetch("/api/alexa/command", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) });
  toast(r.ok ? `ALEXA ← “${text}”` : "ALEXA NOT LINKED");
  if (r.ok) $("#alexa-text").value = "";
};

/* guest */
$("#btn-guest").onclick = async () => {
  $("#guest-overlay").classList.add("on");
  const d = await fetch("/api/guest/qr").then((r) => r.json());
  $("#guest-qr").innerHTML = d.svg;
  $("#guest-url").textContent = d.url;
};

/* ---------- boot ---------- */
connect();
loadShelves();
loadRewind();
