/* Immich Organizer - web UI. No framework, no build step. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const TOKEN_KEY = "immich-organizer-token";

  // The token arrives in the URL. Stash it and scrub the URL so it does not
  // linger in history, screenshots, or a shared home-screen link.
  function resolveToken() {
    const fromUrl = new URLSearchParams(location.search).get("t");
    if (fromUrl) {
      try { localStorage.setItem(TOKEN_KEY, fromUrl); } catch { /* private mode */ }
      history.replaceState(null, "", location.pathname);
      return fromUrl;
    }
    try { return localStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; }
  }

  let token = resolveToken();

  function askForToken(message) {
    $("token-card").classList.remove("is-hidden");
    $("status").textContent = message;
    $("status").classList.add("bad");
    $("token-input").focus();
  }

  function saveToken() {
    let value = $("token-input").value.trim();
    // Accept the whole link as well as the bare key.
    const match = value.match(/[?&]t=([^&#\s]+)/);
    if (match) value = decodeURIComponent(match[1]);
    if (!value) return;
    try { localStorage.setItem(TOKEN_KEY, value); } catch { /* private mode: session only */ }
    token = value;
    $("token-card").classList.add("is-hidden");
    $("token-input").value = "";
    boot();
  }
  const state = {
    assets: [],
    selected: new Set(),
    albums: [],
    sessionSkip: new Set(),   // photos filed during this visit
    lastClicked: -1,
    rules: [],
    defaults: {},
    people: [],               // [{id, name}] named people
    peopleByLabel: new Map(), // label shown in the picker -> person
    editing: -1,              // index of the rule being edited, -1 = new
    editingBase: {},
  };

  // ------------------------------------------------------------- primitives

  async function api(path, options = {}) {
    const res = await fetch(path, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        "X-Organizer-Token": token,
        ...(options.headers || {}),
      },
    });
    let payload = null;
    try { payload = await res.json(); } catch { /* non-JSON error page */ }
    if (!res.ok) {
      const detail = (payload && payload.error) || `HTTP ${res.status}`;
      if (res.status === 401) {
        askForToken("Access key missing or wrong — enter it below.");
        throw new Error("Access key missing or wrong — enter it at the top of the page.");
      }
      const failure = new Error(detail);
      failure.status = res.status;               // lets a caller treat 503 (models not ready) differently
      throw failure;
    }
    return payload;
  }

  let toastTimer;
  function toast(message, bad = false) {
    const el = $("toast");
    el.textContent = message;
    el.classList.toggle("bad", bad);
    el.classList.remove("is-hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("is-hidden"), bad ? 7000 : 4000);
  }

  function busy(on, text = "Working…") {
    $("veil-text").textContent = text;
    $("veil").classList.toggle("is-hidden", !on);
  }

  const thumbUrl = (id) => `/thumb/${encodeURIComponent(id)}?t=${encodeURIComponent(token)}`;
  const splitTerms = (value) => value.split(",").map((s) => s.trim()).filter(Boolean);
  const plural = (n, word) => `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;

  // A segmented control: returns a getter/setter for its value.
  function segmented(id, onChange) {
    const root = $(id);
    let value = root.querySelector(".seg-btn.is-active").dataset.value;
    const set = (v) => {
      value = v;
      root.querySelectorAll(".seg-btn").forEach((b) => b.classList.toggle("is-active", b.dataset.value === v));
      if (onChange) onChange(v);
    };
    root.querySelectorAll(".seg-btn").forEach((btn) => {
      btn.addEventListener("click", () => set(btn.dataset.value));
    });
    return { get: () => value, set };
  }

  // A list of removable chips fed from a text input (album names).
  function chipList(inputId, buttonId, chipsId, onChange) {
    let items = [];
    const render = () => {
      const box = $(chipsId);
      box.textContent = "";
      items.forEach((name) => {
        const chip = document.createElement("span");
        chip.className = "chip";
        chip.textContent = name;
        const x = document.createElement("button");
        x.type = "button";
        x.setAttribute("aria-label", `Stop skipping ${name}`);
        x.textContent = "×";
        x.addEventListener("click", () => { items = items.filter((i) => i !== name); render(); });
        chip.appendChild(x);
        box.appendChild(chip);
      });
      if (onChange) onChange(items);
    };
    const add = () => {
      const input = $(inputId);
      const name = input.value.trim();
      if (!name) return;
      if (!state.albums.some((a) => a.name === name)) {
        toast(`No album called “${name}”. Pick one from the list.`, true);
        return;
      }
      if (!items.includes(name)) items.push(name);
      input.value = "";
      render();
    };
    $(buttonId).addEventListener("click", add);
    $(inputId).addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); add(); } });
    return { get: () => items.slice(), set: (list) => { items = [...new Set(list || [])]; render(); } };
  }

  // Chosen people, shown as chips with their face. Values are person ids.
  function peoplePicker(inputId, buttonId, chipsId, onChange) {
    let ids = [];
    const nameOf = (id) => (state.people.find((p) => p.id === id) || {}).name || "Unknown person";
    const render = () => {
      const box = $(chipsId);
      box.textContent = "";
      ids.forEach((id) => {
        const chip = document.createElement("span");
        chip.className = "chip person";
        const face = document.createElement("img");
        face.alt = "";
        face.loading = "lazy";
        face.src = `/thumb/person/${encodeURIComponent(id)}?t=${encodeURIComponent(token)}`;
        const label = document.createElement("span");
        label.textContent = nameOf(id);
        const x = document.createElement("button");
        x.type = "button";
        x.setAttribute("aria-label", `Remove ${nameOf(id)}`);
        x.textContent = "×";
        x.addEventListener("click", () => { ids = ids.filter((i) => i !== id); render(); });
        chip.append(face, label, x);
        box.appendChild(chip);
      });
      if (onChange) onChange(ids);
    };
    const add = () => {
      const input = $(inputId);
      const typed = input.value.trim();
      if (!typed) return;
      const person = state.peopleByLabel.get(typed)
        || state.people.find((p) => p.name.toLowerCase() === typed.toLowerCase());
      if (!person) {
        toast(`No named person “${typed}”. Pick one from the list (only named people can be searched).`, true);
        return;
      }
      if (!ids.includes(person.id)) ids.push(person.id);
      input.value = "";
      render();
    };
    $(buttonId).addEventListener("click", add);
    $(inputId).addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); add(); } });
    return { get: () => ids.slice(), set: (list) => { ids = [...new Set(list || [])]; render(); }, render };
  }

  // ------------------------------------------------------- suggestion lists
  // Phone browsers (Chrome on Android and the browsers built on its WebView) don't open
  // <datalist> suggestions as a drop-down -- most show nothing at all. On touch screens every
  // input with a list (albums, people, tags) gets its own drop-down instead; computers keep the
  // browser's. Picking an entry fills the box, and where there is an "Add" button next to it,
  // adds it right away.
  (function touchSuggestions() {
    const touch = window.matchMedia("(pointer: coarse)").matches || !("HTMLDataListElement" in window);
    if (!touch) return;
    const MAX = 80;
    const pop = document.createElement("div");
    pop.className = "suggest is-hidden";
    pop.setAttribute("role", "listbox");
    document.body.appendChild(pop);
    let current = null;

    const entries = (input) => {
      const list = document.getElementById(input.dataset.list);
      return list ? [...list.options].map((o) => ({ value: o.value, note: o.label && o.label !== o.value ? o.label : "" })) : [];
    };
    function place(input) {
      const r = input.getBoundingClientRect();
      const vv = window.visualViewport;
      const viewTop = vv ? vv.offsetTop : 0;
      const viewBottom = vv ? vv.offsetTop + vv.height : window.innerHeight;   // above the on-screen keyboard
      const below = viewBottom - r.bottom - 8, above = r.top - viewTop - 8;
      const up = below < 180 && above > below;
      pop.style.left = `${Math.max(8, r.left)}px`;
      pop.style.width = `${r.width}px`;
      pop.style.maxHeight = `${Math.max(120, Math.min(320, up ? above : below))}px`;
      if (up) { pop.style.top = ""; pop.style.bottom = `${window.innerHeight - r.top + 4}px`; }
      else { pop.style.bottom = ""; pop.style.top = `${r.bottom + 4}px`; }
    }
    function hide() { pop.classList.add("is-hidden"); current = null; }
    function choose(input, value) {
      input.value = value;
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
      hide();
      const add = input.closest(".chip-add") && input.closest(".chip-add").querySelector("button");
      if (add) add.click();
    }
    function show(input) {
      const q = input.value.trim().toLowerCase();
      let list = entries(input);
      if (q) {
        list = list.filter((o) => o.value.toLowerCase().includes(q));
        list.sort((a, b) => Number(b.value.toLowerCase().startsWith(q)) - Number(a.value.toLowerCase().startsWith(q)));
      }
      if (!list.length || (list.length === 1 && list[0].value.toLowerCase() === q)) return hide();
      current = input;
      pop.textContent = "";
      list.slice(0, MAX).forEach((o) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "suggest-item";
        b.setAttribute("role", "option");
        const v = document.createElement("span");
        v.textContent = o.value;
        b.appendChild(v);
        if (o.note) { const n = document.createElement("small"); n.textContent = o.note; b.appendChild(n); }
        b.addEventListener("mousedown", (e) => e.preventDefault());     // keep the keyboard up while tapping
        b.addEventListener("click", () => choose(input, o.value));
        pop.appendChild(b);
      });
      if (list.length > MAX) {
        const more = document.createElement("p");
        more.className = "suggest-more";
        more.textContent = `${(list.length - MAX).toLocaleString()} more — keep typing to narrow it down`;
        pop.appendChild(more);
      }
      pop.scrollTop = 0;
      place(input);
      pop.classList.remove("is-hidden");
    }
    document.querySelectorAll("input[list]").forEach((input) => {
      input.dataset.list = input.getAttribute("list");
      input.removeAttribute("list");                  // no half-working native list underneath
      input.setAttribute("autocomplete", "off");
      input.addEventListener("focus", () => show(input));
      input.addEventListener("click", () => { if (current !== input) show(input); });
      input.addEventListener("input", () => show(input));
      input.addEventListener("blur", () => setTimeout(() => { if (current === input && document.activeElement !== input) hide(); }, 200));
      input.addEventListener("keydown", (e) => { if (e.key === "Escape" || e.key === "Enter") hide(); });
    });
    const replace = () => { if (current) place(current); };
    window.addEventListener("resize", replace);
    if (window.visualViewport) window.visualViewport.addEventListener("resize", replace);
    document.addEventListener("scroll", (e) => { if (current && e.target !== pop) place(current); }, true);
  })();

  // ------------------------------------------------------------------ viewer

  function openButton() {
    const b = document.createElement("span");
    b.className = "open-btn";
    b.setAttribute("role", "button");
    b.setAttribute("aria-label", "View full size");
    b.title = "View full size";
    b.textContent = "⤢";
    return b;
  }

  const viewer = { list: [], index: 0, hooks: null, open: false, details: false, full: false };
  const mediaUrl = (id, kind, extra = "") => `/media/${encodeURIComponent(id)}?kind=${kind}&t=${encodeURIComponent(token)}${extra}`;
  const previewUrl = (id, size = "preview") => `/thumb/${encodeURIComponent(id)}?size=${size}&t=${encodeURIComponent(token)}`;
  const isAnimated = (item) => /\.(gif|webp|apng)$/i.test(item.name || "");

  const searchViewerHooks = {
    isSelected: (item) => state.selected.has(item.id),
    toggle: (item, index) => {
      if (state.selected.has(item.id)) state.selected.delete(item.id); else state.selected.add(item.id);
      const tile = $("results").children[index];
      if (tile) setTile(tile, state.selected.has(item.id));
      updateCounts();
    },
  };
  const albumViewerHooks = {
    isSelected: (item) => albumsState.selected.has(item.id),
    toggle: (item, index) => {
      if (albumsState.selected.has(item.id)) albumsState.selected.delete(item.id); else albumsState.selected.add(item.id);
      const tile = $("album-grid").children[index];
      if (tile) tile.setAttribute("aria-pressed", albumsState.selected.has(item.id) ? "true" : "false");
      updateAlbumSelection();
    },
  };

  function openViewer(list, index, hooks) {
    if (!list.length) return;
    viewer.list = list; viewer.index = Math.max(0, Math.min(index, list.length - 1)); viewer.hooks = hooks;
    viewer.full = false;
    if (!viewer.open) {
      viewer.open = true;
      $("viewer").classList.remove("is-hidden");
      document.body.classList.add("viewer-open");
      try { history.pushState({ viewer: true }, ""); } catch { /* ignore */ }
    }
    showViewerItem();
  }

  function closeViewer(fromHistory = false) {
    if (!viewer.open) return;
    viewer.open = false;
    $("v-stage").textContent = "";                // stops a playing video
    $("viewer").classList.add("is-hidden");
    document.body.classList.remove("viewer-open");
    if (!fromHistory && history.state && history.state.viewer) history.back();
  }

  function showViewerItem() {
    const item = viewer.list[viewer.index];
    const stage = $("v-stage");
    stage.textContent = "";
    $("v-count").textContent = `${viewer.index + 1} / ${viewer.list.length}`;
    $("v-name").textContent = `${item.name || ""}${item.date || item.taken ? ` · ${(item.date || item.taken).slice(0, 10)}` : ""}`
      + (item.score != null ? ` · score ${Number(item.score).toFixed(3)}` : "");
    $("v-prev").disabled = viewer.index === 0;
    $("v-next").disabled = viewer.index >= viewer.list.length - 1;
    const selected = viewer.hooks && viewer.hooks.isSelected(item);
    $("v-select").classList.toggle("is-hidden", !viewer.hooks);
    $("v-select").textContent = selected ? "✓ Selected" : "Select";
    $("v-select").classList.toggle("on", !!selected);
    $("v-full").classList.toggle("is-hidden", item.type === "VIDEO" || isAnimated(item));
    $("v-full").classList.toggle("on", viewer.full);
    $("v-download").href = mediaUrl(item.id, "original", `&download=1&name=${encodeURIComponent(item.name || item.id)}`);
    $("viewer").classList.toggle("has-video", item.type === "VIDEO");
    if (item.type === "VIDEO") {
      stage.appendChild(buildVideoPlayer(item));
    } else {
      const img = document.createElement("img");
      img.alt = item.name || "photo";
      img.decoding = "async";
      img.src = isAnimated(item) ? mediaUrl(item.id, "original") : previewUrl(item.id, viewer.full ? "fullsize" : "preview");
      img.addEventListener("dblclick", () => { viewer.full = !viewer.full; showViewerItem(); });
      stage.appendChild(img);
    }
    // warm up the neighbours so swiping feels instant
    [viewer.index - 1, viewer.index + 1].forEach((i) => {
      const n = viewer.list[i];
      if (n && n.type !== "VIDEO" && !isAnimated(n)) { const im = new Image(); im.src = previewUrl(n.id); }
    });
    if (viewer.details) loadViewerDetails(); else $("v-details").classList.add("is-hidden");
  }

  async function loadViewerDetails() {
    const item = viewer.list[viewer.index];
    const box = $("v-details");
    box.classList.remove("is-hidden");
    box.textContent = "Loading…";
    try {
      const a = await api(`/api/asset/${encodeURIComponent(item.id)}`);
      if (viewer.list[viewer.index] !== item) return;
      box.textContent = "";
      const line = (label, value) => {
        if (!value) return;
        const b = document.createElement("b"); b.textContent = `${label}: `;
        box.append(b, document.createTextNode(`${value}\n`));
      };
      line("File", a.name);
      line("Taken", a.taken ? new Date(a.taken).toLocaleString() : "");
      line("Size", [a.width && a.height ? `${a.width}×${a.height}` : "", a.size ? `${(a.size / 1048576).toFixed(1)} MB` : "",
        typeof a.duration === "number" && a.duration > 0 ? fmtTime(a.duration) : ""].filter(Boolean).join(" · "));
      if (a.description) { box.append(document.createTextNode("\n")); line("Description", ""); box.append(document.createTextNode(a.description)); }
    } catch (err) { box.textContent = err.message; }
  }

  function viewerStep(delta) {
    const i = viewer.index + delta;
    if (i < 0 || i >= viewer.list.length) return;
    viewer.index = i; viewer.full = false;
    showViewerItem();
  }

  $("v-close").addEventListener("click", () => closeViewer());
  $("v-prev").addEventListener("click", () => viewerStep(-1));
  $("v-next").addEventListener("click", () => viewerStep(1));
  $("v-full").addEventListener("click", () => { viewer.full = !viewer.full; showViewerItem(); });
  $("v-info").addEventListener("click", () => {
    viewer.details = !viewer.details;
    $("v-info").classList.toggle("on", viewer.details);
    if (viewer.details) loadViewerDetails(); else $("v-details").classList.add("is-hidden");
  });
  $("v-select").addEventListener("click", () => {
    if (!viewer.hooks) return;
    viewer.hooks.toggle(viewer.list[viewer.index], viewer.index);
    const on = viewer.hooks.isSelected(viewer.list[viewer.index]);
    $("v-select").textContent = on ? "✓ Selected" : "Select";
    $("v-select").classList.toggle("on", on);
  });
  window.addEventListener("popstate", () => { if (viewer.open) closeViewer(true); });
  document.addEventListener("keydown", (e) => {
    if (!viewer.open) return;
    const video = $("v-stage").querySelector("video");
    if (video && !e.ctrlKey && !e.metaKey && !e.altKey) {
      const k = e.key.toLowerCase();
      const act = { " ": () => playerToggle(video), k: () => playerToggle(video), j: () => playerSeek(video, -10),
        l: () => playerSeek(video, 10), m: () => { video.muted = !video.muted; }, f: () => playerFullscreen(video),
        ",": () => playerSeek(video, -5), ".": () => playerSeek(video, 5) }[k];
      if (act && !(e.target && /INPUT|SELECT|TEXTAREA/.test(e.target.tagName))) { act(); e.preventDefault(); return; }
    }
    if (e.key === "Escape") closeViewer();
    else if (e.key === "ArrowLeft") viewerStep(-1);
    else if (e.key === "ArrowRight") viewerStep(1);
    else if (e.key === "i") $("v-info").click();
    else if (e.key === "s") $("v-select").click();
    else return;
    e.preventDefault();
  });
  (() => {   // swipe left/right to move, down to close (not while scrubbing a video)
    let x0 = null, y0 = 0;
    const stage = $("v-stage");
    stage.addEventListener("touchstart", (e) => {
      if (e.touches.length !== 1 || e.target.closest(".vplayer")) { x0 = null; return; }   // the video player has its own gestures
      x0 = e.touches[0].clientX; y0 = e.touches[0].clientY;
    }, { passive: true });
    stage.addEventListener("touchend", (e) => {
      if (x0 === null) return;
      const dx = e.changedTouches[0].clientX - x0, dy = e.changedTouches[0].clientY - y0;
      x0 = null;
      if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5) viewerStep(dx < 0 ? 1 : -1);
      else if (dy > 110 && Math.abs(dy) > Math.abs(dx) * 1.5) closeViewer();
    }, { passive: true });
  })();

  // ------------------------------------------------------------- video player
  // Own controls instead of the browser's, so the time bar sits higher and gestures work:
  //   tap = show/hide controls · double-tap left/right = -10 s / +10 s (keeps adding while you tap)
  //   double-tap middle = play/pause · hold = 2× speed while held · swipe left/right = previous/next item
  //   swipe up = next item · swipe down = close. Mouse: click = play/pause, double-click = full screen.

  const fmtTime = (t) => {
    if (!isFinite(t)) return "0:00";
    t = Math.max(0, Math.floor(t));
    const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), sec = String(t % 60).padStart(2, "0");
    return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
  };
  function playerToggle(v) { if (v.paused) v.play().catch(() => {}); else v.pause(); }
  function playerSeek(v, delta) {
    const d = isFinite(v.duration) ? v.duration : Infinity;
    v.currentTime = Math.max(0, Math.min(d - 0.05, v.currentTime + delta));
  }
  function playerFullscreen(v) {
    const root = $("viewer");
    if (document.fullscreenElement) { document.exitFullscreen().catch(() => {}); return; }
    if (root.requestFullscreen) root.requestFullscreen().catch(() => { if (v.webkitEnterFullscreen) v.webkitEnterFullscreen(); });
    else if (v.webkitEnterFullscreen) v.webkitEnterFullscreen();       // iPhone: the system player
  }

  function buildVideoPlayer(item) {
    const wrap = document.createElement("div");
    wrap.className = "vplayer";
    const v = document.createElement("video");
    v.playsInline = true; v.autoplay = true; v.preload = "metadata";
    v.poster = previewUrl(item.id);
    v.src = mediaUrl(item.id, "video");
    wrap.appendChild(v);

    const el = (tag, cls, text) => { const x = document.createElement(tag); if (cls) x.className = cls; if (text != null) x.textContent = text; return x; };
    const big = el("button", "vbig", "▶"); big.type = "button"; big.setAttribute("aria-label", "Play");
    const ripL = el("div", "vripple left"); const ripR = el("div", "vripple right");
    const badge = el("div", "vbadge is-hidden", "2× ▸▸");
    const ctl = el("div", "vctl");
    const bar = el("div", "vbar");
    const cur = el("span", "vtime", "0:00");
    const seek = document.createElement("input");
    seek.type = "range"; seek.className = "vseek"; seek.min = "0"; seek.max = "1000"; seek.step = "1"; seek.value = "0";
    seek.setAttribute("aria-label", "Position");
    const dur = el("span", "vtime", "0:00");
    bar.append(cur, seek, dur);
    const row = el("div", "vrow");
    const btn = (label, aria, fn) => { const b = el("button", "vbtn2", label); b.type = "button"; b.setAttribute("aria-label", aria); b.title = aria; b.addEventListener("click", (e) => { e.stopPropagation(); fn(); poke(); }); return b; };
    const back = btn("⟲ 10", "Back 10 seconds (J)", () => playerSeek(v, -10));
    const play = btn("❚❚", "Play / pause (K)", () => playerToggle(v));
    const fwd = btn("10 ⟳", "Forward 10 seconds (L)", () => playerSeek(v, 10));
    const speeds = [1, 1.25, 1.5, 2, 0.5];
    const speed = btn("1×", "Speed", () => { const i = (speeds.indexOf(v.playbackRate) + 1) % speeds.length; v.playbackRate = speeds[i]; });
    const loop = btn("⟳ off", "Repeat", () => { v.loop = !v.loop; });
    const mute = btn("🔊", "Mute (M)", () => { v.muted = !v.muted; });
    const full = btn("⛶", "Full screen (F)", () => playerFullscreen(v));
    const spacer = el("span", "vspacer");
    row.append(back, play, fwd, spacer, speed, loop, mute, full);
    ctl.append(bar, row);
    wrap.append(big, ripL, ripR, badge, ctl);

    // --- state -> UI
    let dragging = false;
    const sync = () => {
      const d = v.duration;
      if (!dragging && isFinite(d) && d > 0) seek.value = String(Math.round((v.currentTime / d) * 1000));
      cur.textContent = fmtTime(v.currentTime);
      dur.textContent = fmtTime(d);
      const pct = isFinite(d) && d > 0 ? (v.currentTime / d) * 100 : 0;
      seek.style.setProperty("--p", `${pct}%`);
    };
    v.addEventListener("timeupdate", sync);
    v.addEventListener("loadedmetadata", sync);
    v.addEventListener("durationchange", sync);
    const playState = () => {
      play.textContent = v.paused ? "▶" : "❚❚";
      big.classList.toggle("is-hidden", !v.paused);
      if (v.paused) showCtl(true); else poke();
    };
    v.addEventListener("play", playState);
    v.addEventListener("pause", playState);
    v.addEventListener("ratechange", () => { speed.textContent = `${v.playbackRate}×`; });
    v.addEventListener("volumechange", () => { mute.textContent = v.muted ? "🔇" : "🔊"; });
    v.addEventListener("error", () => {
      wrap.textContent = "";
      const m = el("div", "viewer-msg", "This video can't be played here (Immich may still be preparing it). Use ⤓ to download the original.");
      wrap.appendChild(m);
    }, { once: true });
    const loopSync = () => { loop.textContent = v.loop ? "⟳ on" : "⟳ off"; loop.classList.toggle("on", v.loop); };
    loop.addEventListener("click", loopSync);

    seek.addEventListener("input", () => {
      dragging = true;
      const d = v.duration;
      if (isFinite(d)) { cur.textContent = fmtTime((seek.value / 1000) * d); seek.style.setProperty("--p", `${seek.value / 10}%`); }
      poke();
    });
    seek.addEventListener("change", () => {
      const d = v.duration;
      if (isFinite(d)) v.currentTime = (seek.value / 1000) * d;
      dragging = false;
    });
    big.addEventListener("click", (e) => { e.stopPropagation(); v.play().catch(() => {}); });

    // --- controls auto-hide
    let hideTimer = null;
    function showCtl(on) {
      ctl.classList.toggle("vhide", !on);
      clearTimeout(hideTimer);
    }
    function poke() {
      showCtl(true);
      hideTimer = setTimeout(() => { if (!v.paused && !dragging) ctl.classList.add("vhide"); }, 2800);
    }
    ["pointerdown", "pointerup", "click", "touchstart"].forEach((t) => ctl.addEventListener(t, (e) => e.stopPropagation()));

    // --- gestures on the picture
    let start = null, lastTap = null, tapTimer = null, holdTimer = null, held = false, streak = 0, streakTimer = null;
    let lastPointer = "mouse";   // phones also fire "dblclick" for a double-tap; only a real mouse may use it
    const zone = (x) => { const r = wrap.getBoundingClientRect(); const f = (x - r.left) / r.width; return f < 0.35 ? "left" : f > 0.65 ? "right" : "mid"; };
    const flash = (side, text) => {
      const r = side === "left" ? ripL : ripR;
      r.textContent = text; r.classList.remove("show"); void r.offsetWidth; r.classList.add("show");
    };
    wrap.addEventListener("pointerdown", (e) => {
      if (e.button > 0) return;
      lastPointer = e.pointerType || "mouse";
      start = { x: e.clientX, y: e.clientY, t: Date.now(), type: e.pointerType };
      held = false;
      clearTimeout(holdTimer);
      if (e.pointerType !== "mouse") {
        holdTimer = setTimeout(() => {
          if (!start || v.paused) return;
          held = true; v.dataset.rate = String(v.playbackRate); v.playbackRate = 2; badge.classList.remove("is-hidden");
        }, 450);
      }
    });
    const endHold = () => {
      clearTimeout(holdTimer);
      if (held) { v.playbackRate = Number(v.dataset.rate || 1); badge.classList.add("is-hidden"); }
    };
    wrap.addEventListener("pointercancel", () => { endHold(); start = null; });
    wrap.addEventListener("pointerup", (e) => {
      if (!start) return;
      const s = start; start = null;
      const wasHeld = held; endHold();
      if (wasHeld) return;
      const dx = e.clientX - s.x, dy = e.clientY - s.y, dt = Date.now() - s.t;
      // swipes (touch or pen)
      if (s.type !== "mouse" && dt < 700) {
        if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.4) { viewerStep(dx < 0 ? 1 : -1); return; }
        if (dy < -80 && Math.abs(dy) > Math.abs(dx) * 1.4) { viewerStep(1); return; }
        if (dy > 110 && Math.abs(dy) > Math.abs(dx) * 1.4) { closeViewer(); return; }
      }
      if (Math.abs(dx) > 12 || Math.abs(dy) > 12) return;
      if (s.type === "mouse") { playerToggle(v); poke(); return; }
      // taps: single = toggle controls, double = seek / play-pause
      const z = zone(e.clientX);
      const now = Date.now();
      const quick = lastTap && now - lastTap.t < 320 && lastTap.z === z;
      const continuing = streak && lastTap && lastTap.z === z && z !== "mid" && now - lastTap.t < 700;
      if (!continuing) streak = 0;
      if (quick || continuing) {
        clearTimeout(tapTimer);
        lastTap = { t: now, z };
        if (z === "mid") { playerToggle(v); lastTap = null; return; }
        streak += 10;
        playerSeek(v, z === "left" ? -10 : 10);
        flash(z, `${z === "left" ? "−" : "+"}${streak} s`);
        clearTimeout(streakTimer);
        streakTimer = setTimeout(() => { streak = 0; }, 700);
        return;
      }
      lastTap = { t: now, z };
      clearTimeout(tapTimer);
      tapTimer = setTimeout(() => {
        if (ctl.classList.contains("vhide")) poke(); else if (!v.paused) ctl.classList.add("vhide");
      }, 330);
    });
    wrap.addEventListener("dblclick", (e) => {
      e.preventDefault();
      if (lastPointer === "mouse" && !e.target.closest(".vctl")) playerFullscreen(v);
    });
    wrap.addEventListener("contextmenu", (e) => e.preventDefault());   // long-press menu would stop the 2× hold

    poke();
    return wrap;
  }

  // ------------------------------------------------------------------ chrome

  function showTab(name) {
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("is-active", t.dataset.panel === name));
    document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("is-active", p.id === `panel-${name}`));
    if (name === "history") loadHistory();
    if (name === "faces" && !facesState.loaded) loadFaces();
    if (name === "albums" && !albumsState.loaded) loadAlbumList();
    if (name === "themes") loadThemes();
    if (name === "splus") startSearchPlus(); else stopSearchPlus();
    if (name === "tagger") startTagger(); else stopTagger();
  }
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => showTab(tab.dataset.panel));
  });

  let searchEngineReady = false;     // don't save the choice while the page is still starting
  const searchEngine = segmented("engine", (v) => {
    $("engine-hint").textContent = v === "searchplus"
      ? "Scores with the Search+ index (the bigger PE-Core model), with the filters below. Only photos already in "
        + "the Search+ index are found; “must also / must not match” words count from a score of 0.15."
      : "Immich's own smart search (SigLIP2).";
    if (searchEngineReady) savePref("searchEngine", v);
  });
  const searchMode = segmented("mode", (v) => {
    $("field-query").classList.toggle("is-hidden", v !== "query");
    $("field-like").classList.toggle("is-hidden", v !== "like");
  });

  const fileMode = segmented("file-mode", (v) => {
    $("file-verb").textContent = v === "move" ? "Move" : "Add";
    $("file-mode-hint").textContent = v === "move"
      ? "The photos are added here and taken out of every other album they are in. Nothing is deleted, and you can undo it from History."
      : "The photos stay in any albums they are already in, and are also added here.";
  });

  const skipAlbums = chipList("skip-input", "skip-add", "skip-chips", renderSessionChip);
  const searchPeopleMatch = segmented("people-match");
  const searchPeople = peoplePicker("people-input", "people-add", "people-chips",
    (ids) => $("people-match").classList.toggle("is-hidden", ids.length < 2));

  function renderSessionChip() {
    const box = $("skip-chips");
    const old = box.querySelector(".chip.session");
    if (old) old.remove();
    if (!state.sessionSkip.size) return;
    const chip = document.createElement("span");
    chip.className = "chip session";
    chip.textContent = `${plural(state.sessionSkip.size, "photo")} filed this session`;
    const x = document.createElement("button");
    x.type = "button";
    x.setAttribute("aria-label", "Stop skipping photos filed this session");
    x.textContent = "×";
    x.addEventListener("click", () => { state.sessionSkip.clear(); renderSessionChip(); });
    chip.appendChild(x);
    box.appendChild(chip);
  }

  // ------------------------------------------------------------------ search

  function setTile(tile, on) {
    tile.setAttribute("aria-pressed", on ? "true" : "false");
  }

  function renderResults() {
    const grid = $("results");
    grid.textContent = "";
    const frag = document.createDocumentFragment();
    state.assets.forEach((asset, index) => {
      const tile = document.createElement("button");
      tile.className = "tile";
      tile.type = "button";
      tile.dataset.index = String(index);
      setTile(tile, state.selected.has(asset.id));
      tile.title = `${asset.name || asset.id}${asset.date ? ` — ${asset.date}` : ""}`;

      const img = document.createElement("img");
      img.loading = "lazy";
      img.decoding = "async";
      img.alt = asset.name || "photo";
      img.src = thumbUrl(asset.id);

      const mark = document.createElement("span");
      mark.className = "mark";
      mark.textContent = "✓";

      const rank = document.createElement("span");
      rank.className = "rank";
      rank.textContent = `#${index + 1}`;

      tile.append(img, mark, rank, openButton());
      frag.appendChild(tile);
    });
    grid.appendChild(frag);
    updateCounts();
  }

  // One delegated handler instead of thousands of listeners.
  $("results").addEventListener("click", (e) => {
    const tile = e.target.closest(".tile");
    if (!tile) return;
    if (e.target.closest(".open-btn")) return openViewer(state.assets, Number(tile.dataset.index), searchViewerHooks);
    const index = Number(tile.dataset.index);
    const asset = state.assets[index];
    const turnOn = !state.selected.has(asset.id);
    if (e.shiftKey && state.lastClicked >= 0) {
      const [a, b] = [Math.min(state.lastClicked, index), Math.max(state.lastClicked, index)];
      const tiles = $("results").children;
      for (let i = a; i <= b; i++) {
        const id = state.assets[i].id;
        if (turnOn) state.selected.add(id); else state.selected.delete(id);
        setTile(tiles[i], turnOn);
      }
    } else {
      if (turnOn) state.selected.add(asset.id); else state.selected.delete(asset.id);
      setTile(tile, turnOn);
    }
    state.lastClicked = index;
    updateCounts();
  });

  function updateCounts() {
    $("results-count").textContent =
      `${plural(state.assets.length, "result")} · ${state.selected.size.toLocaleString()} selected`;
    $("file-count").textContent = state.selected.size.toLocaleString();
    $("file").disabled = state.selected.size === 0;
  }

  function currentSearch() {
    const body = {
      engine: searchEngine.get(),
      limit: Number($("limit").value) || 200,
      filters: {},
      refine: { all_of: splitTerms($("allOf").value), none_of: splitTerms($("noneOf").value) },
      excludeAlbums: skipAlbums.get(),
      people: searchPeople.get(),
      peopleMatch: searchPeopleMatch.get(),
    };
    if (searchMode.get() === "query") body.query = $("query").value.trim();
    else body.like = $("like").value.trim();
    if ($("type").value) body.filters.type = $("type").value;
    if ($("takenAfter").value) body.filters.taken_after = $("takenAfter").value;
    if ($("takenBefore").value) body.filters.taken_before = $("takenBefore").value;
    if ($("unfiled").checked) body.filters.only_unfiled = true;
    return body;
  }

  async function runSearch() {
    const body = currentSearch();
    if (searchMode.get() === "query" && !body.query && !body.people.length) {
      return toast("Describe what you are looking for, or pick people.", true);
    }
    if (searchMode.get() === "like" && !body.like) return toast("Paste an asset ID to match against.", true);
    if (body.limit > 10000) return toast("Max results can be at most 10,000.", true);
    body.skipIds = [...state.sessionSkip];

    busy(true, body.engine === "searchplus" && body.query
      ? "Searching with the Search+ model… (the first search loads the model: up to ~30 s)"
      : body.people.length > 1 && body.peopleMatch === "any" && (body.query || body.like)
      ? "Searching for photos with any of these people… (can take up to ~15 s)"
      : `Searching your library for up to ${body.limit.toLocaleString()}…`);
    try {
      const data = await api("/api/search", { method: "POST", body: JSON.stringify(body) });
      state.assets = data.assets || [];
      // Everything starts selected; deselecting the tail is faster than
      // picking winners out of a long list.
      state.selected = new Set(state.assets.map((a) => a.id));
      state.lastClicked = -1;
      renderResults();
      $("results-card").classList.remove("is-hidden");
      $("file-card").classList.toggle("is-hidden", state.assets.length === 0);
      let msg = state.assets.length ? `Found ${plural(state.assets.length, "photo")}` : "No matches";
      if (data.total && data.total > state.assets.length) msg += ` (of ${data.total.toLocaleString()} — raise Max results for more)`;
      if (data.excluded) msg += ` (skipped ${data.excluded.toLocaleString()} from your skip list)`;
      toast(msg + ".");
      if (data.unknownAlbums && data.unknownAlbums.length) {
        toast(`No album named ${data.unknownAlbums.join(", ")} — nothing skipped for it.`, true);
      }
    } catch (err) {
      toast(err.message, true);
    } finally {
      busy(false);
    }
  }

  async function fileSelected() {
    const album = $("album").value.trim();
    if (!album) return toast("Name the album to put them in.", true);
    const assetIds = state.assets.map((a) => a.id).filter((id) => state.selected.has(id));
    if (!assetIds.length) return toast("Nothing selected.", true);
    const move = fileMode.get() === "move";
    const exists = state.albums.some((a) => a.name.toLowerCase() === album.toLowerCase());

    if (move && !confirm(
      `Move ${assetIds.length.toLocaleString()} photo(s) to “${album}”?\n\n` +
      "They will be taken out of every other album they are in. Nothing is deleted, " +
      "and you can undo this from the History tab."
    )) return;

    busy(true, `${move ? "Moving" : "Adding"} ${assetIds.length.toLocaleString()} to ${album}…`);
    try {
      const data = await api("/api/file", {
        method: "POST",
        body: JSON.stringify({
          assetIds, album, move,
          createAlbum: true,
          archive: $("archive").checked,
          favorite: $("favorite").checked,
        }),
      });
      const bits = [`${move ? "Moved" : "Added"} ${(data.added + (move ? data.duplicates : 0)).toLocaleString()} to “${data.album}”`];
      if (data.created || !exists) bits.push("(new album)");
      if (!move && data.duplicates) bits.push(`· ${data.duplicates} were already there`);
      if (move && data.removedTotal) {
        bits.push(`· taken out of ${plural(Object.keys(data.removedFrom).length, "other album")}`);
      }
      toast(bits.join(" "));
      if (data.failures && data.failures.length) {
        toast(`${data.failures.length} problem(s): ${data.failures[0]}`, true);
      }

      // Filed photos leave the grid, and optionally future searches.
      const filed = new Set(assetIds);
      if ($("skip-filed").checked) {
        filed.forEach((id) => state.sessionSkip.add(id));
        renderSessionChip();
      }
      state.assets = state.assets.filter((a) => !filed.has(a.id));
      filed.forEach((id) => state.selected.delete(id));
      renderResults();
      $("file-card").classList.toggle("is-hidden", state.assets.length === 0);
      await loadAlbums();
    } catch (err) {
      toast(err.message, true);
    } finally {
      busy(false);
    }
  }

  // ------------------------------------------------------------------- rules

  const ruleMode = segmented("r-mode", (v) => {
    $("r-field-query").classList.toggle("is-hidden", v !== "query");
    $("r-field-like").classList.toggle("is-hidden", v !== "like");
  });
  const ruleSkip = chipList("r-skip-input", "r-skip-add", "r-skip-chips");
  const rulePeopleMatch = segmented("r-people-match");
  const rulePeople = peoplePicker("r-people-input", "r-people-add", "r-people-chips",
    (ids) => $("r-people-match").classList.toggle("is-hidden", ids.length < 2));

  function renderRules(data) {
    const list = $("rules-list");
    list.textContent = "";
    state.rules = data.rules || [];
    state.defaults = data.defaults || {};
    $("rules-path").textContent = data.loaded
      ? `Saved in ${data.path}`
      : (data.error ? `Could not read the rules file: ${data.error}` : "No rules file loaded.");

    const anyEnabled = state.rules.some((r) => r.enabled);
    $("preview-rules").disabled = !anyEnabled;
    $("apply-rules").disabled = !anyEnabled;
    if (!state.rules.length) {
      const p = document.createElement("p");
      p.className = "hint";
      p.textContent = "No rules yet. Press “+ New rule”, or build a search on the Search tab and press “Save as rule…”.";
      list.appendChild(p);
      return;
    }

    state.rules.forEach((rule, index) => {
      const row = document.createElement("div");
      row.className = "rule-row" + (rule.enabled ? "" : " is-off");
      const left = document.createElement("div");
      const name = document.createElement("strong");
      name.textContent = rule.name + (rule.enabled ? "" : " (off)");
      const detail = document.createElement("small");
      let text = `${rule.match} → album “${rule.album}”, up to ${rule.limit.toLocaleString()}`;
      if (rule.people && rule.people.length) text += ` · people: ${rule.people.join(", ")}`;
      if (rule.excludeAlbums && rule.excludeAlbums.length) text += `, skipping ${rule.excludeAlbums.join(", ")}`;
      detail.textContent = text;
      left.append(name, detail);

      const actions = document.createElement("div");
      actions.className = "row-actions";
      const mk = (label, fn, cls = "btn btn-quiet") => {
        const b = document.createElement("button");
        b.className = cls; b.type = "button"; b.textContent = label;
        b.addEventListener("click", fn);
        actions.appendChild(b);
        return b;
      };
      mk("Preview", () => runRules(false, [rule.name])).disabled = !rule.enabled;
      mk("Apply", () => runRules(true, [rule.name])).disabled = !rule.enabled;
      mk("Edit", () => openEditor(index));
      mk("Delete", () => deleteRule(index), "btn btn-quiet danger-text");
      row.append(left, actions);
      list.appendChild(row);
    });
  }

  function openEditor(index, prefill = null) {
    state.editing = index;
    const raw = prefill || (index >= 0 ? state.rules[index].raw : {}) || {};
    state.editingBase = JSON.parse(JSON.stringify(raw));
    const filters = raw.filters || {};
    const refine = raw.refine || {};
    const actions = raw.actions || {};
    $("rule-editor-title").textContent = index >= 0 ? `Edit rule “${state.rules[index].name}”` : "New rule";
    $("r-name").value = raw.name || "";
    $("r-album").value = raw.album || "";
    ruleMode.set(raw.like_asset ? "like" : "query");
    $("r-query").value = raw.query || "";
    $("r-like").value = raw.like_asset || "";
    $("r-limit").value = raw.limit || state.defaults.limit || 200;
    $("r-type").value = (filters.type || "").toUpperCase();
    $("r-takenAfter").value = /^\d{4}-\d{2}-\d{2}/.test(filters.taken_after || "") ? filters.taken_after.slice(0, 10) : "";
    $("r-takenBefore").value = /^\d{4}-\d{2}-\d{2}/.test(filters.taken_before || "") ? filters.taken_before.slice(0, 10) : "";
    $("r-unfiled").checked = Boolean(filters.only_unfiled);
    ruleSkip.set(raw.exclude_albums || []);
    rulePeople.set(Array.isArray(filters.person_ids) ? filters.person_ids : (filters.person_ids ? [filters.person_ids] : []));
    rulePeopleMatch.set(raw.people_match === "any" ? "any" : "all");
    $("r-allOf").value = (refine.all_of || []).join(", ");
    $("r-noneOf").value = (refine.none_of || []).join(", ");
    $("r-archive").checked = Boolean(actions.archive);
    $("r-favorite").checked = Boolean(actions.favorite);
    $("r-enabled").checked = raw.enabled !== false;
    $("rule-editor").classList.remove("is-hidden");
    $("rule-editor").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function ruleFromEditor() {
    // Start from the rule as it was, so settings the form doesn't show survive.
    const raw = JSON.parse(JSON.stringify(state.editingBase || {}));
    raw.name = $("r-name").value.trim();
    raw.album = $("r-album").value.trim();
    delete raw.query; delete raw.like_asset;
    if (ruleMode.get() === "query") { if ($("r-query").value.trim()) raw.query = $("r-query").value.trim(); }
    else if ($("r-like").value.trim()) raw.like_asset = $("r-like").value.trim();
    raw.limit = Number($("r-limit").value) || 200;

    const filters = { ...(raw.filters || {}) };
    const setOrDrop = (key, value) => { if (value) filters[key] = value; else delete filters[key]; };
    setOrDrop("type", $("r-type").value);
    setOrDrop("taken_after", $("r-takenAfter").value);
    setOrDrop("taken_before", $("r-takenBefore").value);
    setOrDrop("only_unfiled", $("r-unfiled").checked || null);
    const who = rulePeople.get();
    setOrDrop("person_ids", who.length ? who : null);
    if (who.length > 1 && rulePeopleMatch.get() === "any") raw.people_match = "any"; else delete raw.people_match;
    if (Object.keys(filters).length) raw.filters = filters; else delete raw.filters;

    const skip = ruleSkip.get();
    if (skip.length) raw.exclude_albums = skip; else delete raw.exclude_albums;

    const refine = { ...(raw.refine || {}) };
    const allOf = splitTerms($("r-allOf").value);
    const noneOf = splitTerms($("r-noneOf").value);
    if (allOf.length) refine.all_of = allOf; else delete refine.all_of;
    if (noneOf.length) refine.none_of = noneOf; else delete refine.none_of;
    if (refine.all_of || refine.none_of) raw.refine = refine; else delete raw.refine;

    const actions = { ...(raw.actions || {}) };
    if ($("r-archive").checked) actions.archive = true; else delete actions.archive;
    if ($("r-favorite").checked) actions.favorite = true; else delete actions.favorite;
    if (Object.keys(actions).length) raw.actions = actions; else delete raw.actions;

    if ($("r-enabled").checked) delete raw.enabled; else raw.enabled = false;
    return raw;
  }

  async function saveRules(rawRules, message) {
    busy(true, "Saving rules…");
    try {
      const data = await api("/api/rules/save", { method: "POST", body: JSON.stringify({ rules: rawRules }) });
      renderRules(data);
      toast(message);
      return true;
    } catch (err) {
      toast(err.message, true);
      return false;
    } finally {
      busy(false);
    }
  }

  async function saveEditor() {
    const raw = ruleFromEditor();
    if (!raw.name) return toast("Give the rule a name.", true);
    if (!raw.album) return toast("Say which album the photos go into.", true);
    if (!raw.query && !raw.like_asset && !(raw.filters && raw.filters.person_ids)) {
      return toast("Describe the photos, paste a reference photo ID, or pick people.", true);
    }
    const all = state.rules.map((r) => r.raw);
    if (state.editing >= 0) all[state.editing] = raw; else all.push(raw);
    if (await saveRules(all, `Rule “${raw.name}” saved.`)) {
      $("rule-editor").classList.add("is-hidden");
    }
  }

  async function deleteRule(index) {
    const rule = state.rules[index];
    if (!confirm(`Delete the rule “${rule.name}”? This only deletes the saved search; no photos or albums change.`)) return;
    const all = state.rules.map((r) => r.raw).filter((_, i) => i !== index);
    await saveRules(all, `Rule “${rule.name}” deleted.`);
  }

  function saveSearchAsRule() {   // "Save as smart album…"
    const s = currentSearch();
    if (!s.query && !s.like && !s.people.length) return toast("Set up a search first.", true);
    const who = s.people.map((id) => (state.people.find((p) => p.id === id) || {}).name).filter(Boolean);
    const name = $("album").value.trim() || (s.query || who.join(" & ") || "Similar photos").slice(0, 40);
    const prefill = {
      source: s.query ? "text" : s.like ? "like" : "none",
      description: s.query || "", like: s.like || "",
      name, album: $("album").value.trim() || name,
      mode: "top", limit: s.limit, engine: s.engine || "immich", cutoff: s.engine === "searchplus" ? 0.17 : 0.1,
      media: s.filters.type || "", people: s.people, people_match: s.peopleMatch,
      taken_after: s.filters.taken_after || "", taken_before: s.filters.taken_before || "",
      exclude_albums: s.excludeAlbums, only_unfiled: !!s.filters.only_unfiled,
      all_of: s.refine.all_of, none_of: s.refine.none_of,
      enabled: false,
    };
    showTab("themes");
    openThemeEditor(null, prefill);
    toast("Saved as “the best N” like your search. Switch to “Every photo above a score” and preview for stricter results, then press Save.");
  }


  function renderPlan(data) {
    const body = $("plan-body");
    body.textContent = "";
    $("plan-card").classList.remove("is-hidden");

    const head = document.createElement("p");
    head.className = "hint";
    head.textContent = data.applied
      ? `Applied — ${data.added} photo(s) added.${data.createdAlbums?.length ? ` Created: ${data.createdAlbums.join(", ")}.` : ""} You can undo this from History.`
      : `Preview only, nothing changed: ${data.totalToAdd} photo(s) would be added.${data.newAlbums?.length ? ` New albums: ${data.newAlbums.join(", ")}.` : ""}`;
    body.appendChild(head);

    (data.entries || []).forEach((entry) => {
      const div = document.createElement("div");
      div.className = "plan-entry";
      const title = document.createElement("div");
      if (entry.error) {
        title.className = "err";
        title.textContent = `${entry.rule}: ${entry.error}`;
      } else {
        const n = document.createElement("span");
        n.className = "n";
        n.textContent = `${entry.rule} → ${entry.album}: ${entry.toAdd}`;
        title.appendChild(n);
        const extra = document.createElement("small");
        extra.textContent = ` to add${entry.alreadyThere ? `, ${entry.alreadyThere} already there` : ""}`
          + `${entry.albumExists ? "" : " (new album)"}`;
        title.appendChild(extra);
      }
      div.appendChild(title);

      if (entry.assets && entry.assets.length) {
        const strip = document.createElement("div");
        strip.className = "thumbs";
        entry.assets.slice(0, 40).forEach((asset) => {
          const img = document.createElement("img");
          img.loading = "lazy";
          img.alt = asset.name || "photo";
          img.src = thumbUrl(asset.id);
          strip.appendChild(img);
        });
        div.appendChild(strip);
      }
      body.appendChild(div);
    });
    $("plan-card").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  async function runRules(apply, only = null) {
    const what = only ? `the rule “${only[0]}”` : "every enabled rule";
    if (apply && !confirm(`Apply ${what}? This adds photos to albums for real (undo is in History).`)) return;
    busy(true, apply ? "Applying…" : "Building preview…");
    try {
      const data = await api(apply ? "/api/apply" : "/api/plan", {
        method: "POST",
        body: JSON.stringify(only ? { only } : {}),
      });
      renderPlan(data);
      toast(apply ? `Applied: ${data.added} added.` : `Preview ready: ${data.totalToAdd} would be added.`);
      if (apply) loadAlbums();
    } catch (err) {
      toast(err.message, true);
    } finally {
      busy(false);
    }
  }

  // ----------------------------------------------------------------- history

  async function loadHistory() {
    const list = $("history-list");
    try {
      const data = await api("/api/history");
      list.textContent = "";
      if (!data.runs.length) {
        const p = document.createElement("p");
        p.className = "hint";
        p.textContent = "Nothing yet.";
        list.appendChild(p);
        return;
      }
      data.runs.forEach((run) => {
        const row = document.createElement("div");
        row.className = "rule-row" + (run.undone ? " is-off" : "");
        const left = document.createElement("div");
        const title = document.createElement("strong");
        const when = run.timestamp ? new Date(run.timestamp).toLocaleString() : "";
        title.textContent = `${run.note || "change"}${run.undone ? " (undone)" : ""}`;
        const detail = document.createElement("small");
        const parts = Object.entries(run.added).map(([a, n]) => `+${n} to ${a}`);
        const removed = Object.entries(run.removed || {});
        if (removed.length) {
          const total = removed.reduce((s, [, n]) => s + n, 0);
          parts.push(`taken out of ${removed.length} album(s) (${total} photo entries)`);
        }
        if (run.deleted && run.deleted.length) parts.push(`deleted album(s): ${run.deleted.join(", ")}`);
        if (run.renamed && run.renamed.length) parts.push(`renamed (was ${run.renamed.join(", ")})`);
        detail.textContent = `${when} · ${parts.join(", ") || "no photos"}`;
        left.append(title, detail);
        const btn = document.createElement("button");
        btn.className = "btn btn-quiet";
        btn.type = "button";
        btn.textContent = run.undone ? "Undone" : "Undo";
        btn.disabled = run.undone;
        btn.addEventListener("click", () => undoRun(run));
        row.append(left, btn);
        list.appendChild(row);
      });
    } catch (err) {
      toast(err.message, true);
    }
  }

  async function undoRun(run) {
    if (!confirm(`Undo “${run.note || "this change"}”? The photos it added are taken back out` +
      (Object.keys(run.removed || {}).length ? ", photos it took out are put back" : "") +
      (run.deleted && run.deleted.length ? ", and deleted albums are re-created" : "") +
      (run.renamed && run.renamed.length ? ", and the old name is restored" : "") + "."
    )) return;
    busy(true, "Undoing…");
    try {
      const data = await api("/api/undo", { method: "POST", body: JSON.stringify({ runId: run.runId }) });
      toast(`Undone: removed ${data.removed}${data.restored ? `, put ${data.restored} back` : ""}.`);
      if (data.failures && data.failures.length) toast(`${data.failures.length} problem(s): ${data.failures[0]}`, true);
      await Promise.all([loadHistory(), loadAlbums()]);
    } catch (err) {
      toast(err.message, true);
    } finally {
      busy(false);
    }
  }

  // ------------------------------------------------------------------ albums

  const albumsState = { loaded: false, albums: [], picked: new Set(), current: null, items: [], selected: new Set(), last: -1 };

  function albumMatches(a) {
    const q = $("albums-find").value.trim().toLowerCase();
    if (q && !a.name.toLowerCase().includes(q)) return false;
    if ($("albums-small").checked && a.count > Number($("albums-small-n").value || 0)) return false;
    return true;
  }

  function renderAlbumList() {
    const sort = $("albums-sort").value;
    const list = albumsState.albums.filter(albumMatches).sort((a, b) => {
      if (sort === "small") return a.count - b.count || a.name.localeCompare(b.name);
      if (sort === "big") return b.count - a.count || a.name.localeCompare(b.name);
      if (sort === "updated") return (b.updatedAt || "").localeCompare(a.updatedAt || "");
      return a.name.localeCompare(b.name, undefined, { sensitivity: "base", numeric: true });
    });
    const box = $("albums-rows");
    box.textContent = "";
    const frag = document.createDocumentFragment();
    list.forEach((a) => {
      const row = document.createElement("div");
      row.className = "album-row";
      const check = document.createElement("input");
      check.type = "checkbox";
      check.checked = albumsState.picked.has(a.id);
      check.setAttribute("aria-label", `Select ${a.name}`);
      check.addEventListener("change", () => {
        if (check.checked) albumsState.picked.add(a.id); else albumsState.picked.delete(a.id);
        updateAlbumBulk();
      });
      const open = document.createElement("button");
      open.type = "button";
      open.className = "album-open";
      const img = document.createElement("img");
      img.loading = "lazy"; img.alt = "";
      if (a.thumb) img.src = thumbUrl(a.thumb);
      const text = document.createElement("span");
      const name = document.createElement("strong");
      name.textContent = a.name || "(untitled)";
      const meta = document.createElement("small");
      meta.textContent = `${a.count.toLocaleString()} item${a.count === 1 ? "" : "s"}${a.shared ? " · shared" : ""}`;
      text.append(name, meta);
      open.append(img, text);
      open.addEventListener("click", () => openAlbum(a.id));
      row.append(check, open);
      frag.appendChild(row);
    });
    box.appendChild(frag);
    const total = albumsState.albums.length;
    $("albums-summary").textContent = `Showing ${list.length.toLocaleString()} of ${total.toLocaleString()} albums · `
      + `${albumsState.albums.filter((a) => a.count <= 3).length} have 3 items or fewer.`;
    updateAlbumBulk();
  }

  function updateAlbumBulk() {
    const n = albumsState.picked.size;
    $("albums-bulk").classList.toggle("is-hidden", n === 0);
    $("albums-bulk-count").textContent = `${n} selected`;
  }

  async function loadAlbumList() {
    busy(true, "Loading albums…");
    try {
      const data = await api("/api/albums/list");
      albumsState.albums = data.albums || [];
      albumsState.loaded = true;
      const known = new Set(albumsState.albums.map((a) => a.id));
      albumsState.picked.forEach((id) => { if (!known.has(id)) albumsState.picked.delete(id); });
      renderAlbumList();
    } catch (err) { toast(err.message, true); } finally { busy(false); }
  }

  function albumById(id) { return albumsState.albums.find((a) => a.id === id); }

  async function openAlbum(id, keepSort = false) {
    const album = albumById(id);
    if (!album) return;
    albumsState.current = album;
    albumsState.selected.clear();
    albumsState.last = -1;
    if (!keepSort) {
      $("album-sort").value = prefs.albumSort || "taken_desc"; $("album-q").value = "";
      albumsState.quiet = true; albumMedia.set(""); albumsState.quiet = false;
    }
    $("albums-list-view").classList.add("is-hidden");
    $("album-view").classList.remove("is-hidden");
    $("album-title").textContent = album.name;
    $("album-meta").textContent = album.description || "";
    busy(true, `Opening ${album.name}…`);
    try {
      const sort = $("album-sort").value;
      $("album-q-field").classList.toggle("is-hidden", sort !== "relevance");
      const params = new URLSearchParams({ sort, type: albumMedia.get() });
      if (sort === "relevance") params.set("q", $("album-q").value.trim());
      const data = await api(`/api/albums/${encodeURIComponent(id)}/items?${params}`);
      albumsState.items = data.items || [];
      if (data.sort === "relevance") {
        $("album-q").value = data.query || "";
        if (data.unscored) toast(`${data.unscored} item(s) have no smart-search score yet — listed last.`);
      }
      if (data.addedAvailable === false) toast("Date added isn't available (no database access) — sorted by date taken.", true);
      renderAlbumItems();
    } catch (err) { toast(err.message, true); } finally { busy(false); }
    window.scrollTo(0, 0);
  }

  function renderAlbumItems() {
    const grid = $("album-grid");
    grid.textContent = "";
    const frag = document.createDocumentFragment();
    albumsState.items.forEach((item, index) => {
      const tile = document.createElement("button");
      tile.type = "button"; tile.className = "tile pick-tile"; tile.dataset.index = String(index);
      tile.setAttribute("aria-pressed", albumsState.selected.has(item.id) ? "true" : "false");
      tile.title = `${item.name || ""} · taken ${(item.taken || "").slice(0, 10)}${item.added ? ` · added ${item.added.slice(0, 16)}` : ""}`;
      const img = document.createElement("img");
      img.loading = "lazy"; img.decoding = "async"; img.alt = item.name || "photo"; img.src = thumbUrl(item.id);
      const mark = document.createElement("span"); mark.className = "mark"; mark.textContent = "✓";
      tile.append(img, mark);
      const label = [];
      if (item.score != null) label.push(item.score.toFixed(3));
      if (item.type === "VIDEO") label.push("▶");
      if (label.length) { const v = document.createElement("span"); v.className = "rank"; v.textContent = label.join(" "); tile.appendChild(v); }
      tile.appendChild(openButton());
      frag.appendChild(tile);
    });
    grid.appendChild(frag);
    updateAlbumSelection();
  }

  function updateAlbumSelection() {
    const n = albumsState.selected.size;
    $("album-count").textContent = `${albumsState.items.length.toLocaleString()} items · ${n.toLocaleString()} selected`;
    document.querySelectorAll("#album-actions .sel-n").forEach((el) => { el.textContent = n.toLocaleString(); });
    ["album-move", "album-copy", "album-remove"].forEach((id) => { $(id).disabled = n === 0; });
  }

  $("album-grid").addEventListener("click", (e) => {
    const tile = e.target.closest(".tile");
    if (!tile) return;
    if (e.target.closest(".open-btn")) return openViewer(albumsState.items, Number(tile.dataset.index), albumViewerHooks);
    const index = Number(tile.dataset.index);
    const id = albumsState.items[index].id;
    const on = !albumsState.selected.has(id);
    const tiles = $("album-grid").children;
    const range = e.shiftKey && albumsState.last >= 0
      ? [Math.min(albumsState.last, index), Math.max(albumsState.last, index)] : [index, index];
    for (let i = range[0]; i <= range[1]; i++) {
      const itemId = albumsState.items[i].id;
      if (on) albumsState.selected.add(itemId); else albumsState.selected.delete(itemId);
      tiles[i].setAttribute("aria-pressed", on ? "true" : "false");
    }
    albumsState.last = index;
    updateAlbumSelection();
  });

  async function albumCall(action, body, message) {
    busy(true, message);
    try {
      const data = await api(`/api/albums/${action}`, { method: "POST", body: JSON.stringify(body) });
      if (data.failures && data.failures.length) toast(`${data.failures.length} problem(s): ${data.failures[0]}`, true);
      return data;
    } catch (err) { toast(err.message, true); return null; } finally { busy(false); }
  }

  async function refreshAfterChange(reopen) {
    albumsState.loaded = false;
    await loadAlbumList();
    await loadAlbums();              // the album-name suggestions used everywhere
    if (reopen && albumById(reopen)) await openAlbum(reopen, true);
    else if (albumsState.current) closeAlbum();
  }

  function closeAlbum() {
    albumsState.current = null;
    $("album-view").classList.add("is-hidden");
    $("albums-list-view").classList.remove("is-hidden");
  }

  async function transferSelected(move) {
    const target = $("album-target").value.trim();
    if (!target) return toast("Type or pick the target album.", true);
    const assetIds = albumsState.items.filter((i) => albumsState.selected.has(i.id)).map((i) => i.id);
    const src = albumsState.current;
    const data = await albumCall("transfer", { sourceId: src.id, assetIds, target, move },
      `${move ? "Moving" : "Copying"} ${assetIds.length} to ${target}…`);
    if (!data) return;
    toast(`${move ? "Moved" : "Copied"} ${data.added + data.alreadyThere} to “${data.target}”${data.created ? " (new album)" : ""}.`);
    await refreshAfterChange(src.id);
  }

  function askTarget(prompt_) {
    const value = window.prompt(prompt_);
    return value ? value.trim() : "";
  }

  async function mergeAlbums(ids) {
    const names = ids.map((id) => (albumById(id) || {}).name).filter(Boolean);
    const target = askTarget(`Merge ${names.length} album(s) into which album? (existing name, or a new one)\n\n${names.slice(0, 12).join("\n")}${names.length > 12 ? "\n…" : ""}`);
    if (!target) return;
    const deleteSources = confirm(`After copying their photos into “${target}”, delete the ${names.length} emptied album(s)?\n\nOK = delete them (photos are kept in “${target}”)\nCancel = keep them too`);
    const targetAlbum = albumsState.albums.find((a) => a.name.toLowerCase() === target.toLowerCase());
    let replaceTarget = false;
    if (targetAlbum && targetAlbum.count > 0) {
      replaceTarget = confirm(`“${target}” already has ${targetAlbum.count} item(s).\n\nOK = overwrite: it ends up with ONLY the merged photos\nCancel = keep its current photos and add the new ones`);
    }
    const data = await albumCall("merge", { sourceIds: ids, target, deleteSources, replaceTarget }, `Merging into ${target}…`);
    if (!data) return;
    toast(`Merged into “${data.target}”: +${data.added}${data.deletedAlbums.length ? `, deleted ${data.deletedAlbums.length} album(s)` : ""}${data.removedFromTarget ? `, replaced ${data.removedFromTarget}` : ""}.`);
    albumsState.picked.clear();
    // From the list: stay on the list. From inside an album: stay in it, or
    // follow its photos to the target if the album itself was merged away.
    const cur = albumsState.current;
    await refreshAfterChange(cur ? (data.deletedAlbums.includes(cur.name) ? data.targetId : cur.id) : null);
  }

  async function deleteAlbums(ids) {
    const names = ids.map((id) => (albumById(id) || {}).name).filter(Boolean);
    if (!confirm(`Delete ${names.length} album(s)? The photos stay in your library; only the album(s) go. You can undo from History.\n\n${names.slice(0, 15).join("\n")}${names.length > 15 ? "\n…" : ""}`)) return;
    const data = await albumCall("delete", { albumIds: ids }, "Deleting…");
    if (!data) return;
    toast(`Deleted ${data.deleted.length} album(s).`);
    albumsState.picked.clear();
    closeAlbum();
    await refreshAfterChange(null);
  }

  $("albums-find").addEventListener("input", renderAlbumList);
  $("albums-sort").addEventListener("change", () => { savePref("albumsSort", $("albums-sort").value); renderAlbumList(); });
  $("albums-small").addEventListener("change", renderAlbumList);
  $("albums-small-n").addEventListener("input", renderAlbumList);
  $("albums-clear").addEventListener("click", () => { albumsState.picked.clear(); renderAlbumList(); });
  $("albums-merge").addEventListener("click", () => mergeAlbums([...albumsState.picked]));
  $("albums-delete").addEventListener("click", () => deleteAlbums([...albumsState.picked]));
  $("albums-new").addEventListener("click", async () => {
    const name = askTarget("Name of the new album:");
    if (!name) return;
    const data = await albumCall("create", { name }, "Creating…");
    if (data) { toast(`Created “${data.name}”.`); await refreshAfterChange(data.id); }
  });
  $("album-back").addEventListener("click", closeAlbum);
  $("album-sort").addEventListener("change", () => {
    if ($("album-sort").value !== "relevance") savePref("albumSort", $("album-sort").value);
    if (albumsState.current) openAlbum(albumsState.current.id, true);
  });
  const albumMedia = segmented("album-media", () => {
    if (!albumsState.quiet && albumsState.current) openAlbum(albumsState.current.id, true);
  });
  $("album-q-go").addEventListener("click", () => albumsState.current && openAlbum(albumsState.current.id, true));
  $("album-q").addEventListener("keydown", (e) => { if (e.key === "Enter" && albumsState.current) openAlbum(albumsState.current.id, true); });
  $("album-grid").addEventListener("dblclick", (e) => {
    const tile = e.target.closest(".tile");
    if (tile) openViewer(albumsState.items, Number(tile.dataset.index), albumViewerHooks);
  });
  $("results").addEventListener("dblclick", (e) => {
    const tile = e.target.closest(".tile");
    if (tile) openViewer(state.assets, Number(tile.dataset.index), searchViewerHooks);
  });
  $("album-all").addEventListener("click", () => { albumsState.selected = new Set(albumsState.items.map((i) => i.id)); renderAlbumItems(); });
  $("album-none").addEventListener("click", () => { albumsState.selected.clear(); renderAlbumItems(); });
  $("album-move").addEventListener("click", () => transferSelected(true));
  $("album-copy").addEventListener("click", () => transferSelected(false));
  $("album-remove").addEventListener("click", async () => {
    const assetIds = albumsState.items.filter((i) => albumsState.selected.has(i.id)).map((i) => i.id);
    if (!confirm(`Remove ${assetIds.length} item(s) from “${albumsState.current.name}”? They stay in your library.`)) return;
    const data = await albumCall("remove", { albumId: albumsState.current.id, assetIds }, "Removing…");
    if (data) { toast(`Removed ${data.removed}.`); await refreshAfterChange(albumsState.current.id); }
  });
  $("album-rename").addEventListener("click", async () => {
    const name = window.prompt("New name:", albumsState.current.name);
    if (!name || name.trim() === albumsState.current.name) return;
    const data = await albumCall("rename", { albumId: albumsState.current.id, name: name.trim() }, "Renaming…");
    if (data) { toast(`Renamed to “${data.name}”.`); await refreshAfterChange(albumsState.current.id); }
  });
  $("album-merge").addEventListener("click", () => mergeAlbums([albumsState.current.id]));
  $("album-delete").addEventListener("click", () => deleteAlbums([albumsState.current.id]));

  // ----------------------------------------------------------------- Search+
  // Experimental second index made with a bigger CLIP-style model (see searchplus.py).

  const spState = { data: null, timer: null, assets: [], immich: [], selected: new Set(), lastClicked: -1, failKey: "" };
  const spMode = segmented("sp-mode", (v) => {
    $("sp-field-text").classList.toggle("is-hidden", v !== "text");
    $("sp-field-like").classList.toggle("is-hidden", v !== "like");
  });
  const isUuid = (v) => /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(v);
  function spLikeThumb() {
    const id = $("sp-like").value.trim();
    const img = $("sp-like-thumb");
    img.classList.toggle("is-hidden", !isUuid(id));
    if (isUuid(id)) img.src = thumbUrl(id);
  }
  $("sp-like").addEventListener("input", spLikeThumb);

  function startSearchPlus() {
    loadSearchPlus();
    clearInterval(spState.timer);
    spState.timer = setInterval(() => { if (!document.hidden) loadSearchPlus(); }, 5000);
  }
  function stopSearchPlus() { clearInterval(spState.timer); spState.timer = null; }
  async function loadSearchPlus() {
    try { renderSearchPlus(await api("/api/searchplus")); }
    catch (err) { $("sp-state").textContent = err.message; }
  }
  const fmtMinutes = (m) => (m < 90 ? `${Math.max(1, Math.round(m))} min` : `${(m / 60).toFixed(1)} h`);

  function renderSearchPlus(data) {
    spState.data = data;
    const c = data.counts, ix = data.indexer, sv = data.service, cfg = data.settings;
    let text, cls = "";
    if (ix.state === "running") { text = `Indexing · ${ix.ratePerMin ? `${Math.round(ix.ratePerMin).toLocaleString()} per minute` : "warming up"}`; cls = "ok"; }
    else if (ix.state === "starting") text = ix.detail ? ix.detail.charAt(0).toUpperCase() + ix.detail.slice(1) : "Starting…";
    else if (ix.state === "done") { text = "Up to date"; cls = "ok"; }
    else if (ix.state === "error") { text = `Stopped — ${ix.detail}`; cls = "warn"; }
    else text = c.indexed ? "Paused" : "Not built yet";
    const stateEl = $("sp-state");
    stateEl.className = `d-state ${cls}`;
    stateEl.textContent = text;
    const sub = document.createElement("small");
    if (sv.status === "ok") {
      const left = sv.idleExitMinutes ? Math.max(0, sv.idleExitMinutes - (sv.idleSeconds || 0) / 60) : null;
      sub.textContent = "Model loaded on the GPU" + (left != null && (sv.idleSeconds || 0) > 60 ? ` · unloads in ~${Math.ceil(left)} min if unused` : "");
    } else if (sv.status === "loading" || (sv.container === "running" && !sv.status)) sub.textContent = "Model loading…";
    else if (sv.status === "error") sub.textContent = `Model failed to load: ${sv.error}`;
    else if (sv.container === "missing") sub.textContent = "Model server not installed yet";
    else sub.textContent = "Model not loaded — the GPU memory is free";
    stateEl.appendChild(sub);

    const pct = c.assets ? (100 * c.indexed) / c.assets : 0;
    $("sp-meter").style.width = `${pct.toFixed(1)}%`;
    let prog = `${c.indexed.toLocaleString()} of ${c.assets.toLocaleString()} photos and videos indexed (${pct.toFixed(pct > 99 || pct < 1 ? 1 : 0)}%)`;
    if (ix.etaMinutes && c.pending) prog += ` · about ${fmtMinutes(ix.etaMinutes)} left`;
    if (!c.assets) prog = "The library list is read when indexing starts.";
    $("sp-progress").textContent = prog;
    const stats = [[c.pending, "still to do"], [c.videosIndexed, `of ${c.videos.toLocaleString()} videos`],
      [c.frames, "pictures in the index"], [c.failed, "could not index"], [`${c.sizeMB} MB`, "index size"]];
    if (c.cleared) stats.push([c.cleared, "skipped (cleared)"]);
    const box = $("sp-stats"); box.textContent = "";
    stats.forEach(([n, label]) => {
      const d = document.createElement("div"); d.className = "stat";
      const b = document.createElement("b"); b.textContent = typeof n === "number" ? n.toLocaleString() : n;
      const sp = document.createElement("span"); sp.textContent = label;
      d.append(b, sp); box.appendChild(d);
    });

    const toggle = $("sp-toggle");
    toggle.textContent = cfg.indexing ? "Pause" : (c.indexed ? "Resume indexing" : "Build index");
    toggle.classList.toggle("btn-primary", !cfg.indexing);
    $("sp-unload").disabled = sv.container !== "running" && !cfg.indexing;
    if (document.activeElement !== $("sp-frames")) {
      $("sp-frames").value = cfg.video_frames;
      $("sp-frames-v").textContent = String(cfg.video_frames);
    }
    $("sp-keep").checked = !!cfg.keep_updated;

    const fails = data.failures || [];
    $("sp-fail-box").classList.toggle("is-hidden", !c.failed);
    $("sp-fail-sum").textContent = `Could not index ${plural(c.failed, "item")}`;
    $("sp-fail-hint").textContent = c.retrying
      ? `${c.retrying.toLocaleString()} of them will be tried again by themselves (up to 3 times, 15 minutes apart). Newest first:`
      : "Newest first:";
    const key = fails.map((f) => `${f.id}:${f.error}`).join("|");
    if (key !== spState.failKey) {
      spState.failKey = key;
      const fb = $("sp-failures"); fb.textContent = "";
      fails.forEach((f) => {
        const row = document.createElement("div"); row.className = "desc-row";
        const img = document.createElement("img"); img.loading = "lazy"; img.alt = ""; img.src = thumbUrl(f.id);
        img.onerror = () => { img.style.visibility = "hidden"; };
        const d = document.createElement("div");
        const p = document.createElement("p"); p.textContent = f.error;
        d.appendChild(p); row.append(img, d); fb.appendChild(row);
      });
    }
  }

  function fillSpGrid(grid, list, selectable, other) {
    grid.textContent = "";
    const frag = document.createDocumentFragment();
    list.forEach((a, i) => {
      const tile = document.createElement("button");
      tile.className = "tile"; tile.type = "button"; tile.dataset.index = String(i);
      tile.setAttribute("aria-pressed", !selectable || spState.selected.has(a.id) ? "true" : "false");
      if (other && other.has(a.id)) tile.classList.add("sp-common");
      tile.title = `${a.name || a.id}${a.date ? ` — ${a.date}` : ""}${a.score != null ? ` · score ${a.score}` : ""}`;
      const img = document.createElement("img");
      img.loading = "lazy"; img.decoding = "async"; img.alt = a.name || "photo"; img.src = thumbUrl(a.id);
      tile.appendChild(img);
      if (selectable) { const mark = document.createElement("span"); mark.className = "mark"; mark.textContent = "✓"; tile.appendChild(mark); }
      const rank = document.createElement("span"); rank.className = "rank";
      rank.textContent = `#${i + 1}${a.type === "VIDEO" ? " ▶" : ""}`;
      tile.append(rank, openButton());
      frag.appendChild(tile);
    });
    grid.appendChild(frag);
  }

  function updateSpCounts() {
    $("sp-count").textContent = `${plural(spState.assets.length, "result")} · ${spState.selected.size.toLocaleString()} selected`;
    $("sp-file-count").textContent = spState.selected.size.toLocaleString();
    $("sp-file").disabled = spState.selected.size === 0;
    $("sp-file-card").classList.toggle("is-hidden", !spState.assets.length);
  }

  function renderSpResults(data) {
    $("sp-results-card").classList.remove("is-hidden");
    const two = !!data.immich;
    $("sp-cols").classList.toggle("two", two);
    $("sp-col-immich").classList.toggle("is-hidden", !two);
    $("sp-head-mine").classList.toggle("is-hidden", !two);
    const mine = new Set(spState.assets.map((a) => a.id));
    const theirs = new Set(spState.immich.map((a) => a.id));
    if (two) {
      $("sp-head-mine").textContent = `Search+ · PE-Core G/14 · ${plural(spState.assets.length, "result")}`;
      $("sp-head-immich").textContent = `Immich · ${data.immich.model} · ${data.immich.overlap} of them in both (marked “both”)`;
    }
    fillSpGrid($("sp-grid"), spState.assets, true, two ? theirs : null);
    if (two) fillSpGrid($("sp-grid-immich"), spState.immich, false, mine);
    const c = data.counts;
    let note = `Took ${(data.tookMs / 1000).toFixed(1)} s. Tap a photo to (un)select it; ⤢ opens it. Videos score by their best-matching frame.`;
    if (c && (c.pending || 0) + (c.retrying || 0) > 0) note = `Searched the ${c.indexed.toLocaleString()} of ${c.assets.toLocaleString()} items indexed so far — the index is still being built. ` + note;
    if (c && !c.indexed) note = "The index is empty — press “Build index” below first.";
    $("sp-note").textContent = note;
    updateSpCounts();
  }

  async function runSearchPlus(likeId = null) {
    if (likeId) { spMode.set("like"); $("sp-like").value = likeId; spLikeThumb(); }
    const mode = spMode.get();
    const body = {
      media: $("sp-type").value || null, limit: Math.min(Number($("sp-limit").value) || 200, 1000),
      after: $("sp-after").value || null, before: $("sp-before").value || null, compare: $("sp-compare").checked,
    };
    if (mode === "text") {
      body.text = $("sp-text").value.trim();
      if (!body.text) return toast("Type what you are looking for.", true);
    } else {
      body.like = $("sp-like").value.trim();
      if (!isUuid(body.like)) return toast("Pick a photo: open any photo in the panel and press “Similar”, or paste its ID.", true);
    }
    const loaded = spState.data && spState.data.service.status === "ok";
    busy(true, mode === "text" && !loaded ? "Loading the Search+ model, then searching… (about half a minute)" : "Searching…");
    try {
      const data = await api("/api/searchplus/search", { method: "POST", body: JSON.stringify(body) });
      spState.assets = data.assets || [];
      spState.immich = data.immich ? data.immich.assets : [];
      spState.selected = new Set(spState.assets.map((a) => a.id));
      spState.lastClicked = -1;
      renderSpResults(data);
      loadSearchPlus();
    } catch (err) {
      toast(err.message, true);
    } finally {
      busy(false);
    }
  }

  const spViewerHooks = {
    isSelected: (item) => spState.selected.has(item.id),
    toggle: (item, index) => {
      if (spState.selected.has(item.id)) spState.selected.delete(item.id); else spState.selected.add(item.id);
      const tile = $("sp-grid").children[index];
      if (tile) tile.setAttribute("aria-pressed", spState.selected.has(item.id) ? "true" : "false");
      updateSpCounts();
    },
  };
  $("sp-grid").addEventListener("click", (e) => {
    const tile = e.target.closest(".tile");
    if (!tile) return;
    const index = Number(tile.dataset.index);
    if (e.target.closest(".open-btn")) return openViewer(spState.assets, index, spViewerHooks);
    const on = !spState.selected.has(spState.assets[index].id);
    const [a, b] = e.shiftKey && spState.lastClicked >= 0
      ? [Math.min(spState.lastClicked, index), Math.max(spState.lastClicked, index)] : [index, index];
    for (let i = a; i <= b; i++) {
      const id = spState.assets[i].id;
      if (on) spState.selected.add(id); else spState.selected.delete(id);
      $("sp-grid").children[i].setAttribute("aria-pressed", on ? "true" : "false");
    }
    spState.lastClicked = index;
    updateSpCounts();
  });
  $("sp-grid-immich").addEventListener("click", (e) => {
    const tile = e.target.closest(".tile");
    if (tile) openViewer(spState.immich, Number(tile.dataset.index), null);
  });
  $("sp-all").addEventListener("click", () => {
    spState.selected = new Set(spState.assets.map((a) => a.id));
    [...$("sp-grid").children].forEach((t) => t.setAttribute("aria-pressed", "true"));
    updateSpCounts();
  });
  $("sp-none").addEventListener("click", () => {
    spState.selected.clear();
    [...$("sp-grid").children].forEach((t) => t.setAttribute("aria-pressed", "false"));
    updateSpCounts();
  });
  $("sp-run").addEventListener("click", () => runSearchPlus());
  $("sp-text").addEventListener("keydown", (e) => { if (e.key === "Enter") runSearchPlus(); });
  $("sp-like").addEventListener("keydown", (e) => { if (e.key === "Enter") runSearchPlus(); });
  $("v-similar").addEventListener("click", () => {
    const item = viewer.list[viewer.index];
    if (!item) return;
    closeViewer();
    showTab("splus");
    runSearchPlus(item.id);
  });

  $("sp-file").addEventListener("click", async () => {
    const album = $("sp-album").value.trim();
    if (!album) return toast("Name the album to put them in.", true);
    const assetIds = spState.assets.map((a) => a.id).filter((id) => spState.selected.has(id));
    if (!assetIds.length) return toast("Nothing selected.", true);
    busy(true, `Adding ${assetIds.length.toLocaleString()} to ${album}…`);
    try {
      const data = await api("/api/file", { method: "POST", body: JSON.stringify({ assetIds, album, move: false, createAlbum: true }) });
      toast(`Added ${data.added.toLocaleString()} to “${data.album}”${data.duplicates ? ` · ${data.duplicates} were already there` : ""}.`);
      await loadAlbums();
    } catch (err) { toast(err.message, true); } finally { busy(false); }
  });

  async function searchPlusCall(path, body, message) {
    try {
      const data = await api(`/api/searchplus/${path}`, { method: "POST", body: JSON.stringify(body) });
      renderSearchPlus(data);
      if (message) toast(message);
      return data;
    } catch (err) { toast(err.message, true); loadSearchPlus(); return null; }
  }
  $("sp-toggle").addEventListener("click", () => {
    const on = spState.data && spState.data.settings.indexing;
    searchPlusCall("index", { action: on ? "pause" : "start" },
      on ? "Paused. The model unloads by itself after 20 quiet minutes (or press Stop & free GPU)."
        : "Indexing started — it resumes by itself after a restart.");
  });
  $("sp-unload").addEventListener("click", async () => {
    busy(true, "Stopping and freeing the GPU…");
    try {
      const data = await searchPlusCall("unload", {});
      if (data) toast(data.stopped ? "Stopped; the Search+ model is unloaded." : "Stopped. The model was not loaded.");
    } finally { busy(false); }
  });
  $("sp-frames").addEventListener("input", () => { $("sp-frames-v").textContent = $("sp-frames").value; });
  $("sp-frames").addEventListener("change", () => searchPlusCall("settings", { changes: { video_frames: Number($("sp-frames").value) } }));
  $("sp-keep").addEventListener("change", () => searchPlusCall("settings", { changes: { keep_updated: $("sp-keep").checked } }));
  $("sp-retry").addEventListener("click", () => searchPlusCall("index", { action: "retry" }, "They will be tried again."));
  $("sp-clear").addEventListener("click", () => searchPlusCall("index", { action: "clear" },
    "Cleared — those items are skipped (Try again brings them back)."));
  $("sp-reset").addEventListener("click", () => {
    if (!confirm("Delete the Search+ index and start again from nothing? (Immich is not touched.)")) return;
    searchPlusCall("index", { action: "reset", confirm: "reset" }, "Index deleted. Press Build index to start again.");
  });

  // -------------------------------------------------------------- AI Tagger
  // Two image taggers plus a vision model write tags and a description into the Immich description
  // (see aitagger.py and docs/AI-TAGGER.md). Same shape as Search+: status polled every 5 s while the tab shows.

  const tagState = {
    data: null, timer: null, failKey: "",
    base: {},              // form values as last loaded or saved: tells which keys the person changed
    seen: {},              // the server value each form field was last filled from
    rules: [],             // the rule rows being edited
    list: { items: [], total: 0, page: 1, size: 60, tags: [], tag: "", q: "", outdated: false, allTags: false, seq: 0, loaded: false, version: null },
    selected: new Set(),   // asset ids ticked in the list (kept across pages)
    qTimer: null,
  };
  const tgEl = (tag, cls, text) => {
    const x = document.createElement(tag);
    if (cls) x.className = cls;
    if (text != null) x.textContent = text;
    return x;
  };
  const tgSame = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const tgScore = (n) => (Number.isFinite(Number(n)) ? Number(n).toFixed(2) : "");
  const TG_UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;

  // Every setting the form edits. `kind` says how to read and write it.
  const TG_FIELDS = {
    instructions:   { id: "tg-instructions", kind: "text" },
    vocabulary:     { id: "tg-vocabulary", kind: "text" },
    blocked:        { id: "tg-blocked", kind: "list" },
    language:       { id: "tg-language", kind: "line" },
    describe:       { id: "tg-describe", kind: "bool" },
    use_wd:         { id: "tg-use-wd", kind: "bool" },
    use_ram:        { id: "tg-use-ram", kind: "bool" },
    character_tags: { id: "tg-character", kind: "bool" },
    rating_tag:     { id: "tg-rating", kind: "bool" },
    write_tags:     { id: "tg-write-tags", kind: "bool" },
    wd_strictness:  { id: "tg-wd", kind: "float" },
    ram_strictness: { id: "tg-ram", kind: "float" },
    max_tags:       { id: "tg-max", kind: "int" },
    video_frames:   { kind: "frames" },
    batch_size:     { id: "tg-batch", kind: "int" },
    vlm_parallel:   { id: "tg-parallel", kind: "int" },
    vram_gb:        { id: "tg-vram", kind: "int" },
  };
  const TG_LABELS = {
    instructions: "Instructions", vocabulary: "Vocabulary", blocked: "Blocked tags", language: "Language",
    describe: "Write a description", use_wd: "WD tagger", use_ram: "RAM++ tagger", character_tags: "Character names",
    rating_tag: "Rating tag", write_tags: "Immich tags", wd_strictness: "WD strictness", ram_strictness: "RAM++ strictness",
    max_tags: "Most tags per photo", rules: "Rules", video_frames: "Captures per video", batch_size: "Assets per round",
    vlm_parallel: "Parallel descriptions", vram_gb: "GPU memory",
  };
  // Which settings change what is written, and the lightest reprocess mode that applies them.
  const TG_MODE_OF = {
    wd_strictness: "retag", ram_strictness: "retag", rules: "retag", blocked: "retag", max_tags: "retag",
    write_tags: "retag", character_tags: "retag", rating_tag: "retag",
    instructions: "describe", vocabulary: "describe", language: "describe", describe: "describe",
    video_frames: "full", use_wd: "full", use_ram: "full",
  };
  const TG_MODES = [
    ["retag", "Re-tag", "quick, re-applies the stored results, no GPU needed"],
    ["describe", "Re-describe", "asks the description model again"],
    ["full", "Full re-process", "runs the taggers and the description model again, slowest"],
  ];
  const TG_GROUPS = {
    how: ["instructions", "vocabulary", "blocked", "language", "describe", "use_wd", "use_ram", "character_tags",
      "rating_tag", "write_tags", "wd_strictness", "ram_strictness", "max_tags"],
    rules: ["rules"],
    speed: ["video_frames", "batch_size", "vlm_parallel", "vram_gb"],
  };
  const TG_LIMIT_IDS = { wd_strictness: "tg-wd", ram_strictness: "tg-ram", max_tags: "tg-max", batch_size: "tg-batch",
    vlm_parallel: "tg-parallel", vram_gb: "tg-vram" };
  const TG_RULE_KEYS = ["if_all", "if_any", "unless", "add", "remove"];

  const tgFrames = segmented("tg-frames");
  const FRAMES_NOTE = "Pictures taken along the clip and tagged together. Photos always use one.";

  function tgRead(key) {
    if (key === "rules") return tgCleanRules(tagState.rules);
    const f = TG_FIELDS[key];
    if (f.kind === "frames") return Number(tgFrames.get());
    const el = $(f.id);
    switch (f.kind) {
      case "bool": return el.checked;
      case "list": return splitTerms(el.value);
      case "float": return Number(el.value);
      case "int": return el.value.trim() === "" ? NaN : Number(el.value);
      case "line": return el.value.trim();
      default: return el.value;
    }
  }

  function tgWrite(key, value) {
    if (key === "rules") {
      tagState.rules = tgCleanRules(value);
      tgRenderRules();
      return;
    }
    const f = TG_FIELDS[key];
    if (f.kind === "frames") {
      tgFrames.set(String(value));
      $("tg-frames-note").textContent = [2, 6].includes(Number(value)) ? FRAMES_NOTE : `Set to ${value} at the moment. ${FRAMES_NOTE}`;
      return;
    }
    const el = $(f.id);
    if (f.kind === "bool") el.checked = !!value;
    else {
      const text = f.kind === "list" ? (Array.isArray(value) ? value : []).join(", ") : String(value == null ? "" : value);
      if (el.value !== text) el.value = text;
    }
    tgShowSliders();
  }

  function tgShowSliders() {
    $("tg-wd-v").textContent = tgScore($("tg-wd").value);
    $("tg-ram-v").textContent = tgScore($("tg-ram").value);
    $("tg-vram-v").textContent = `${$("tg-vram").value} GB`;
  }
  ["tg-wd", "tg-ram", "tg-vram"].forEach((id) => $(id).addEventListener("input", tgShowSliders));

  // The ranges come from the server (`limits`), never from here. Set them before filling in values.
  function tgApplyLimits(limits) {
    Object.entries(TG_LIMIT_IDS).forEach(([key, id]) => {
      const range = limits && limits[key];
      if (!range) return;
      const el = $(id);
      el.min = String(range[0]);
      el.max = String(range[1]);
      if (el.type !== "number") return;
      const label = el.closest("label");
      const small = label.querySelector("small") || label.appendChild(document.createElement("small"));
      if (small.dataset.base == null) small.dataset.base = small.textContent;
      small.textContent = `${range[0]} to ${range[1]}.${small.dataset.base ? ` ${small.dataset.base}` : ""}`;
    });
  }

  // Fill the form from the server's settings, but never over something the person is in the middle of editing.
  function tgSync(settings) {
    [...Object.keys(TG_FIELDS), "rules"].forEach((key) => {
      if (!(key in settings)) return;
      const first = !(key in tagState.base);
      const changedOnServer = !tgSame(settings[key], tagState.seen[key]);
      if (first || (changedOnServer && tgSame(tgRead(key), tagState.base[key]))) {
        tgWrite(key, settings[key]);
        tagState.base[key] = tgRead(key);
        tagState.seen[key] = settings[key];
      }
    });
    $("tg-keep").checked = !!settings.keep_updated;
  }

  // ------------------------------------------------------------------ status

  function startTagger() {
    loadTagger();
    clearInterval(tagState.timer);
    tagState.timer = setInterval(() => { if (!document.hidden) loadTagger(); }, 5000);
    if (!tagState.list.loaded) tgLoadAssets();
  }
  function stopTagger() { clearInterval(tagState.timer); tagState.timer = null; }
  async function loadTagger() {
    try { renderTagger(await api("/api/aitagger")); }
    catch (err) { $("tg-state").textContent = err.message; }
  }

  function tagError(err) {
    let message = err.message || "Something went wrong.";
    if (err.status === 503) {
      message += `${/[.!?]$/.test(message) ? "" : "."} The models may still be loading — try again in a minute.`;
    }
    toast(message, true);
  }

  async function tagCall(path, body, message) {
    try {
      const data = await api(`/api/aitagger/${path}`, { method: "POST", body: JSON.stringify(body) });
      if (data && data.settings) renderTagger(data);
      if (message) toast(typeof message === "function" ? message(data) : message);
      return data;
    } catch (err) { tagError(err); loadTagger(); return null; }
  }

  function tgServiceWord(s) {
    s = s || {};
    if (s.status === "ok") return "ready";
    if (s.status === "loading" || (s.container === "running" && !s.status)) return "loading…";
    if (s.status === "error") return `failed${s.error ? ` (${s.error})` : ""}`;
    if (s.container === "missing") return "not installed yet";
    return "not loaded";
  }

  function renderTagger(data) {
    tagState.data = data;
    const c = data.counts || {}, ix = data.indexer || {}, sv = data.service || {}, cfg = data.settings || {};
    const tg = sv.tagger || {}, vl = sv.vlm || {}, gpu = sv.gpu || {}, models = data.models || {};
    const processed = c.processed || 0, total = c.assets || 0;

    let text, cls = "";
    if (ix.state === "running") { text = `Tagging · ${ix.ratePerMin ? `${Math.round(ix.ratePerMin).toLocaleString()} per minute` : "warming up"}`; cls = "ok"; }
    else if (ix.state === "starting") text = ix.detail ? ix.detail.charAt(0).toUpperCase() + ix.detail.slice(1) : "Starting…";
    else if (ix.state === "done") { text = "Up to date"; cls = "ok"; }
    else if (ix.state === "error") { text = `Stopped — ${ix.error || ix.detail || "see the log"}`; cls = "warn"; }
    else text = processed ? "Paused" : "Not started yet";
    const stateEl = $("tg-state");
    stateEl.className = `d-state ${cls}`;
    stateEl.textContent = text;
    if (ix.detail && (ix.state === "running" || (ix.state === "done"))) {
      const sub = document.createElement("small");
      sub.textContent = ix.detail;
      stateEl.appendChild(sub);
    }

    const pct = total ? (100 * processed) / total : 0;
    $("tg-meter").style.width = `${pct.toFixed(1)}%`;
    let prog = `${processed.toLocaleString()} of ${total.toLocaleString()} photos and videos tagged (${pct.toFixed(pct > 99 || pct < 1 ? 1 : 0)}%)`;
    if (ix.etaMinutes && c.pending) prog += ` · about ${fmtMinutes(ix.etaMinutes)} left`;
    if (!total) prog = "The library list is read when tagging starts.";
    $("tg-progress").textContent = prog;

    const stats = [[processed, "tagged"], [c.pending || 0, "still to do"], [c.queued || 0, "queued to redo"],
      [c.outdated || 0, "older settings"], [c.failed || 0, "could not tag"]];
    if (c.excluded) stats.push([c.excluded, "left alone"]);
    const box = $("tg-stats"); box.textContent = "";
    stats.forEach(([n, label]) => {
      const d = tgEl("div", "stat");
      d.append(tgEl("b", "", Number(n).toLocaleString()), tgEl("span", "", label));
      box.appendChild(d);
    });

    const lines = [`Taggers: ${tgServiceWord(tg)} · Description model: ${tgServiceWord(vl)}`
      + (gpu.totalGb ? ` · GPU ${gpu.usedGb != null ? gpu.usedGb : "?"} of ${gpu.totalGb} GB in use` : "")];
    if (sv.searchplusRunning) lines.push("Search+ is using the GPU right now; it is stopped when the tagger starts.");
    const names = [models.wd, models.ram, models.vlm].filter(Boolean);
    if (names.length) lines.push(`Models: ${names.join(" · ")}`);
    $("tg-service").textContent = lines.join("\n");

    const toggle = $("tg-toggle");
    toggle.textContent = cfg.indexing ? "Pause" : (processed ? "Resume tagging" : "Start tagging");
    toggle.classList.toggle("btn-primary", !cfg.indexing);
    $("tg-load").disabled = tg.status === "ok" && vl.status === "ok";
    $("tg-unload").disabled = ![tg, vl].some((s) => s.container === "running") && !cfg.indexing;

    const fails = data.failures || [];
    $("tg-fail-box").classList.toggle("is-hidden", !c.failed && !fails.length);
    $("tg-fail-sum").textContent = `Could not tag ${plural(c.failed || fails.length, "item")}`;
    const key = fails.map((f) => `${f.id}:${f.error}`).join("|");
    if (key !== tagState.failKey) {
      tagState.failKey = key;
      const fb = $("tg-failures"); fb.textContent = "";
      fails.forEach((f) => {
        const row = tgEl("div", "desc-row");
        const img = document.createElement("img");
        img.loading = "lazy"; img.alt = ""; img.src = thumbUrl(f.id);
        img.onerror = () => { img.style.visibility = "hidden"; };
        const d = document.createElement("div");
        d.appendChild(tgEl("p", "", f.error || "Failed"));
        d.appendChild(tgEl("small", "", `${f.name || f.id}${f.attempts ? ` · tried ${f.attempts}×` : ""}`));
        row.append(img, d);
        fb.appendChild(row);
      });
    }

    $("tg-outdated-row").classList.toggle("is-hidden", !c.outdated);
    $("tg-outdated-text").textContent = `${plural(c.outdated || 0, "tagged asset")} still ${c.outdated === 1 ? "has" : "have"} tags from older settings.`;

    tgApplyLimits(data.limits);
    tgSync(cfg);
    if (tagState.list.loaded && tagState.list.version !== data.settingsVersion) tgRenderAssets();
  }

  // --------------------------------------------------------------- a dialog
  // One modal for the choices below. `build(body)` fills it and returns a function that reads the answer;
  // the promise gives that answer, or null if the person cancelled.
  function tgModal(title, confirmLabel, build, danger = false) {
    return new Promise((resolve) => {
      const dlg = $("tg-dialog"), body = $("tg-dialog-body"), ok = $("tg-dialog-ok");
      $("tg-dialog-title").textContent = title;
      body.textContent = "";
      const read = build(body);
      ok.textContent = confirmLabel;
      ok.classList.toggle("btn-danger", danger);
      ok.classList.toggle("btn-primary", !danger);
      let answer = null;
      ok.onclick = () => { answer = read(); dlg.close(); };
      $("tg-dialog-cancel").onclick = () => dlg.close();
      dlg.addEventListener("close", () => resolve(answer), { once: true });
      dlg.showModal();
    });
  }

  // A mode picker with a one-line explanation of the chosen mode underneath. Returns the <select>.
  function tgModeSelect(box, selected, suggested) {
    const select = document.createElement("select");
    TG_MODES.forEach(([value, label]) => {
      const o = document.createElement("option");
      o.value = value;
      o.textContent = `${label}${value === suggested ? " (recommended)" : ""}`;
      select.appendChild(o);
    });
    select.value = selected;
    const note = tgEl("small", "hint", "");
    const showNote = () => { note.textContent = (TG_MODES.find((m) => m[0] === select.value) || [])[2] || ""; };
    select.addEventListener("change", showNote);
    showNote();
    box.append(select, note);
    return select;
  }

  const tgSuggestMode = (keys) => keys.reduce((best, k) => {
    const rank = { retag: 1, describe: 2, full: 3 };
    return rank[TG_MODE_OF[k]] > rank[best] ? TG_MODE_OF[k] : best;
  }, "retag");

  // "New assets only" or "also update the N already-tagged assets" (and how). Gives "none" or a mode.
  function tgAskReprocess(keys, n) {
    const suggested = tgSuggestMode(keys);
    return tgModal("Apply to assets already tagged?", "Save", (box) => {
      box.appendChild(tgEl("p", "hint", `You changed: ${keys.map((k) => TG_LABELS[k] || k).join(", ")}.`));
      const option = (value, label, note, checked) => {
        const row = tgEl("label", "check");
        const input = document.createElement("input");
        input.type = "radio"; input.name = "tg-choice"; input.value = value; input.checked = checked;
        const span = document.createElement("span");
        span.append(label);
        if (note) span.appendChild(tgEl("small", "", note));
        row.append(input, span);
        box.appendChild(row);
        return input;
      };
      const only = option("none", "New assets only", "Already-tagged ones keep their tags and are marked as made with older settings. You can update them later.", true);
      const also = option("also", `Also update the ${plural(n, "already-tagged asset")}`, "", false);
      const select = tgModeSelect(box, suggested, suggested);
      select.disabled = true;
      const sync = () => { select.disabled = !also.checked; };
      only.addEventListener("change", sync);
      also.addEventListener("change", sync);
      return () => (also.checked ? select.value : "none");
    });
  }

  // ------------------------------------------------------------- rules editor

  const tgBlankRule = () => ({ if_all: [], if_any: [], unless: [], add: [], remove: [] });
  function tgCleanRules(list) {
    return (Array.isArray(list) ? list : []).map((r) => {
      const out = {};
      TG_RULE_KEYS.forEach((k) => {
        out[k] = (r && Array.isArray(r[k]) ? r[k] : []).map((t) => String(t).trim()).filter(Boolean);
      });
      return out;
    });
  }

  const TG_RULE_FIELDS = [
    ["if_all", "IF all of", "girl, beach"],
    ["if_any", "IF any of", "dog, cat"],
    ["unless", "UNLESS", "night"],
    ["add", "THEN add", "summer"],
    ["remove", "THEN remove", "indoors"],
  ];

  function tgRenderRules() {
    const box = $("tg-rules");
    box.textContent = "";
    if (!tagState.rules.length) box.appendChild(tgEl("p", "hint", "No rules yet."));
    tagState.rules.forEach((rule, i) => {
      const row = tgEl("div", "tg-rule");
      const head = tgEl("div", "tg-rule-head");
      head.appendChild(tgEl("span", "", `Rule ${i + 1}`));
      const del = tgEl("button", "btn btn-quiet danger-text", "Delete");
      del.type = "button";
      del.addEventListener("click", () => { tagState.rules.splice(i, 1); tgRenderRules(); });
      head.appendChild(del);
      row.appendChild(head);
      const field = (key) => {
        const [, label, example] = TG_RULE_FIELDS.find((f) => f[0] === key);
        const wrap = tgEl("label", "field");
        wrap.appendChild(tgEl("span", "", label));
        const input = document.createElement("input");
        input.type = "text"; input.autocomplete = "off"; input.spellcheck = false;
        input.placeholder = example;
        input.value = rule[key].join(", ");
        input.addEventListener("input", () => { rule[key] = splitTerms(input.value); row.classList.remove("is-bad"); });
        wrap.appendChild(input);
        return wrap;
      };
      const pair = (a, b) => { const g = tgEl("div", "grid-2"); g.append(field(a), field(b)); return g; };
      row.append(pair("if_all", "if_any"), field("unless"), pair("add", "remove"));
      box.appendChild(row);
    });
  }

  // Rows left completely empty are ignored; a half-filled one stops the save.
  function tgReadRules() {
    const rows = [...document.querySelectorAll("#tg-rules .tg-rule")];
    rows.forEach((r) => r.classList.remove("is-bad"));
    const out = [];
    tgCleanRules(tagState.rules).forEach((rule, i) => {
      if (TG_RULE_KEYS.every((k) => !rule[k].length)) return;
      if (!rule.if_all.length && !rule.if_any.length) {
        if (rows[i]) { rows[i].classList.add("is-bad"); rows[i].scrollIntoView({ block: "center" }); }
        throw new Error(`Rule ${i + 1} needs a condition: fill in IF all of or IF any of.`);
      }
      if (!rule.add.length && !rule.remove.length) {
        if (rows[i]) { rows[i].classList.add("is-bad"); rows[i].scrollIntoView({ block: "center" }); }
        throw new Error(`Rule ${i + 1} needs an action: fill in THEN add or THEN remove.`);
      }
      out.push(rule);
    });
    return out;
  }

  $("tg-rule-add").addEventListener("click", () => {
    tagState.rules.push(tgBlankRule());
    tgRenderRules();
    const inputs = document.querySelectorAll("#tg-rules .tg-rule:last-child input");
    if (inputs.length) inputs[0].focus();
  });

  // -------------------------------------------------------------------- save

  // Which of `keys` the person changed since the form was filled, checked against the server's ranges.
  function tgCollect(keys) {
    const limits = (tagState.data && tagState.data.limits) || {};
    const changes = {};
    keys.forEach((key) => {
      const value = key === "rules" ? tgReadRules() : tgRead(key);
      const kind = (TG_FIELDS[key] || {}).kind;
      const label = TG_LABELS[key] || key;
      if (kind === "int" || kind === "frames") {
        if (!Number.isInteger(value)) throw new Error(`${label} must be a whole number.`);
      } else if (kind === "float" && !Number.isFinite(value)) {
        throw new Error(`${label} must be a number.`);
      }
      if (kind === "line" && !value) throw new Error(`${label} can't be empty.`);
      const range = limits[key];
      if (range && typeof value === "number" && (value < range[0] || value > range[1])) {
        throw new Error(`${label} must be between ${range[0]} and ${range[1]}.`);
      }
      if (!tgSame(value, tagState.base[key])) changes[key] = value;
    });
    return changes;
  }

  async function tgSave(keys) {
    if (!tagState.data) return toast("Still loading — try again in a moment.", true);
    let changes;
    try { changes = tgCollect(keys); } catch (err) { return toast(err.message, true); }
    const names = Object.keys(changes);
    if (!names.length) return toast("Nothing changed.");
    const content = names.filter((k) => k in TG_MODE_OF);
    const done = (tagState.data.counts || {}).processed || 0;
    let reprocess = "none";
    if (content.length && done > 0) {
      const choice = await tgAskReprocess(content, done);
      if (choice === null) return;
      reprocess = choice;
    }
    busy(true, "Saving…");
    try {
      const body = { changes, reprocess };
      if (reprocess !== "none") body.scope = "all";
      const data = await api("/api/aitagger/settings", { method: "POST", body: JSON.stringify(body) });
      names.forEach((k) => { tagState.base[k] = changes[k]; tagState.seen[k] = (data.settings || {})[k]; });
      if (names.includes("rules")) { tagState.rules = tgCleanRules(changes.rules); tgRenderRules(); }
      renderTagger(data);
      if (reprocess !== "none") {
        toast(`Saved. ${plural(data.queued || 0, "asset")} queued to be redone${data.settings && data.settings.indexing ? "." : " — press Start tagging to begin."}`);
      } else if (content.length) {
        toast("Saved. New photos and videos use it; the ones already tagged keep their tags for now.");
      } else toast("Saved.");
      if (reprocess !== "none" || content.length) tgLoadAssets();
    } catch (err) { tagError(err); } finally { busy(false); }
  }
  $("tg-save-how").addEventListener("click", () => tgSave(TG_GROUPS.how));
  $("tg-save-rules").addEventListener("click", () => tgSave(TG_GROUPS.rules));
  $("tg-save-speed").addEventListener("click", () => tgSave(TG_GROUPS.speed));
  $("tg-keep").addEventListener("change", () => tagCall("settings", { changes: { keep_updated: $("tg-keep").checked } }));

  // ----------------------------------------------------------------- buttons
  $("tg-load").addEventListener("click", async () => {
    busy(true, "Starting the models… the first start downloads them and can take several minutes.");
    try { await tagCall("load", {}, "The models are starting. Loading can take a few minutes the first time."); }
    finally { busy(false); }
  });
  $("tg-toggle").addEventListener("click", () => {
    const on = tagState.data && tagState.data.settings.indexing;
    tagCall("index", { action: on ? "pause" : "start" },
      on ? "Paused. The models unload by themselves after a quiet spell (or press Stop & free GPU)."
        : "Tagging started — it picks up by itself after a restart. Search+ is paused meanwhile.");
  });
  $("tg-unload").addEventListener("click", async () => {
    busy(true, "Stopping and freeing the GPU…");
    try { await tagCall("unload", {}, "Stopped; the models are unloaded and the GPU is free."); }
    finally { busy(false); }
  });
  $("tg-retry").addEventListener("click", () => tagCall("index", { action: "retry" }, "They will be tried again."));
  $("tg-clear").addEventListener("click", () => tagCall("index", { action: "clear" }, "Cleared — those items are skipped (Try again brings them back)."));

  // -------------------------------------------------------------------- test

  const tgTestId = () => { const m = $("tg-test-id").value.match(TG_UUID); return m ? m[0].toLowerCase() : ""; };
  function tgTestThumb() {
    const id = tgTestId();
    const img = $("tg-test-thumb");
    img.classList.toggle("is-hidden", !id);
    if (id) img.src = thumbUrl(id);
    $("tg-test-name").textContent = "";
  }
  $("tg-test-id").addEventListener("input", tgTestThumb);
  $("tg-test-id").addEventListener("keydown", (e) => { if (e.key === "Enter") tgRunTest("preview"); });
  $("tg-test-thumb").addEventListener("error", () => $("tg-test-thumb").classList.add("is-hidden"));

  async function tgSample(type) {
    try {
      const s = await api(`/api/aitagger/sample?type=${type}`);
      $("tg-test-id").value = s.id;
      tgTestThumb();
      $("tg-test-name").textContent = `${s.name || s.id} · ${s.type === "VIDEO" ? "video" : "photo"}`;
    } catch (err) { toast(err.message, true); }
  }
  $("tg-rand-photo").addEventListener("click", () => tgSample("IMAGE"));
  $("tg-rand-video").addEventListener("click", () => tgSample("VIDEO"));
  $("tg-preview").addEventListener("click", () => tgRunTest("preview"));

  // "preview" tags one asset and writes nothing; "apply" does the same and writes it.
  async function tgRunTest(path) {
    const id = tgTestId();
    if (!id) return toast("Pick a photo or video: paste its ID, or press Random photo / Random video.", true);
    const d = tagState.data, sv = (d && d.service) || {};
    const ready = sv.tagger && sv.tagger.status === "ok" && (!d.settings.describe || (sv.vlm && sv.vlm.status === "ok"));
    busy(true, ready ? (path === "apply" ? "Tagging and writing…" : "Tagging…")
      : "Loading the models (can take a few minutes the first time), then tagging…");
    try {
      const p = await api(`/api/aitagger/${path}`, { method: "POST", body: JSON.stringify({ id }) });
      renderPreview(p);
      if (path === "apply") {
        toast(p.written ? "Written to the description." : "Nothing needed writing.");
        loadTagger();
        tgLoadAssets();
      } else loadTagger();
    } catch (err) { tagError(err); } finally { busy(false); }
  }

  function tgChips(parent, list, cls = "") {
    const row = tgEl("div", "tg-tags");
    const sorted = list.slice().sort((a, b) => (b.score || 0) - (a.score || 0));
    sorted.slice(0, 60).forEach((t) => {
      const chip = tgEl("span", `tg-tag ${cls}`.trim(), t.tag);
      if (t.score != null && t.source !== "vlm" && t.source !== "rule") chip.appendChild(tgEl("small", "", tgScore(t.score)));
      if (t.source) chip.appendChild(tgEl("span", "badge", t.source));
      row.appendChild(chip);
    });
    if (sorted.length > 60) row.appendChild(tgEl("small", "hint", `+${sorted.length - 60} weaker ones not shown`));
    parent.appendChild(row);
  }

  function renderPreview(p) {
    const out = $("tg-test-out");
    out.textContent = "";
    out.classList.remove("is-hidden");
    const heading = (text) => out.appendChild(tgEl("h3", "tg-h", text));
    const frames = (p.frames || []).filter((f) => typeof f === "string" && f.startsWith("data:image/"));

    heading(`${p.name || p.id} · ${p.type === "VIDEO" ? "video" : "photo"} · ${plural(p.captures || frames.length, "capture")}`);
    if (frames.length) {
      const strip = tgEl("div", "tg-frames");
      frames.forEach((src, i) => {
        const img = document.createElement("img");
        img.src = src; img.alt = `Capture ${i + 1}`;
        strip.appendChild(img);
      });
      out.appendChild(strip);
    }

    const m = p.models || {};
    if (m.wd) { heading(`Illustration / people tagger (WD) · ${m.wd.length}`); tgChips(out, m.wd); }
    if (m.ram) { heading(`Everyday objects tagger (RAM++) · ${m.ram.length}`); tgChips(out, m.ram); }
    if (m.rating) {
      const ratings = Object.entries(m.rating).sort((a, b) => b[1] - a[1]);
      if (ratings.length) {
        heading("Rating");
        const line = tgEl("p", "hint", "");
        ratings.forEach(([name, score], i) => {
          if (i) line.append(" · ");
          const part = tgEl(i === 0 ? "b" : "span", "", `${name} ${tgScore(score)}`);
          line.appendChild(part);
        });
        out.appendChild(line);
      }
    }

    const v = p.vlm;
    heading("Description model");
    if (!v || (!v.description && !(v.add_tags || []).length && !(v.remove_tags || []).length)) {
      out.appendChild(tgEl("p", "hint", (v && v.note) || "It was not used for this one."));
    } else {
      if (v.description) out.appendChild(tgEl("p", "", v.description));
      if ((v.add_tags || []).length) {
        out.appendChild(tgEl("small", "hint", "Added"));
        tgChips(out, v.add_tags.map((tag) => ({ tag: `+ ${tag}` })), "add");
      }
      if ((v.remove_tags || []).length) {
        out.appendChild(tgEl("small", "hint", "Removed"));
        tgChips(out, v.remove_tags.map((tag) => ({ tag })), "remove");
      }
      if (v.note) out.appendChild(tgEl("p", "hint", v.note));
    }

    heading("Rules that fired");
    const fired = p.rules || [];
    if (!fired.length) out.appendChild(tgEl("p", "hint", "None."));
    fired.forEach((r) => {
      const bits = [];
      if ((r.added || []).length) bits.push(`added ${r.added.join(", ")}`);
      if ((r.removed || []).length) bits.push(`removed ${r.removed.join(", ")}`);
      out.appendChild(tgEl("p", "hint", `Rule ${Number(r.rule) + 1}: ${bits.join(" · ") || "no change"}`));
    });

    heading(`Final tags · ${(p.tags || []).length}`);
    tgChips(out, p.tags || [], "final");

    heading("Description to write");
    out.appendChild(tgEl("p", "", p.description || "(none)"));

    const ba = tgEl("div", "tg-ba");
    [["Before", p.currentDescription], ["After", p.newDescription]].forEach(([label, text]) => {
      const col = tgEl("div", "");
      col.appendChild(tgEl("small", "hint", label));
      col.appendChild(tgEl("pre", "tg-block", text || "(empty)"));
      ba.appendChild(col);
    });
    out.appendChild(ba);

    const write = tgEl("button", `btn ${p.written ? "" : "btn-primary"}`.trim(), p.written ? "Written ✓" : "Write this");
    write.type = "button";
    write.disabled = !!p.written;
    write.style.marginTop = "14px";
    write.addEventListener("click", () => tgRunTest("apply"));
    out.appendChild(write);
  }

  // ---------------------------------------------------------- tagged assets

  const tgViewerHooks = {
    isSelected: (item) => tagState.selected.has(item.id),
    toggle: (item, index) => {
      if (tagState.selected.has(item.id)) tagState.selected.delete(item.id); else tagState.selected.add(item.id);
      const row = $("tg-list").children[index];
      const box = row && row.querySelector("input[type=checkbox]");
      if (box) box.checked = tagState.selected.has(item.id);
      tgUpdateSelection();
    },
  };

  function tgUpdateSelection() {
    const n = tagState.selected.size;
    $("tg-bulk").classList.toggle("is-hidden", !n);
    $("tg-bulk-count").textContent = `${n.toLocaleString()} selected`;
  }

  async function tgLoadAssets(page) {
    const L = tagState.list;
    if (page) L.page = page;
    const seq = ++L.seq;
    const params = new URLSearchParams({ page: String(L.page), size: String(L.size) });
    if (L.tag) params.set("tag", L.tag);
    if (L.q) params.set("q", L.q);
    if (L.outdated) params.set("outdated", "1");
    try {
      const data = await api(`/api/aitagger/assets?${params}`);
      if (seq !== L.seq) return;
      L.items = data.items || [];
      L.total = data.total || 0;
      L.page = data.page || L.page;
      L.tags = data.tags || [];
      L.loaded = true;
      tgRenderAssets();
    } catch (err) {
      if (seq === L.seq) $("tg-count").textContent = err.message;
    }
  }

  function tgRenderAssets() {
    const L = tagState.list;
    const current = tagState.data ? tagState.data.settingsVersion : null;
    L.version = current;

    const top = $("tg-top-tags");
    top.textContent = "";
    const shown = L.allTags ? L.tags : L.tags.slice(0, 18);
    const tagChip = (name, count) => {
      const b = tgEl("button", `tg-tag${L.tag === name ? " is-on" : ""}`, name);
      b.type = "button";
      if (count != null) b.appendChild(tgEl("small", "", count.toLocaleString()));
      b.addEventListener("click", () => { L.tag = L.tag === name ? "" : name; tgLoadAssets(1); });
      return b;
    };
    if (L.tag && !shown.some((t) => t.tag === L.tag)) top.appendChild(tagChip(L.tag, null));
    shown.forEach((t) => top.appendChild(tagChip(t.tag, t.count)));
    if (L.tags.length > 18) {
      const more = tgEl("button", "tg-tag", L.allTags ? "Fewer tags" : `All ${L.tags.length} tags`);
      more.type = "button";
      more.addEventListener("click", () => { L.allTags = !L.allTags; tgRenderAssets(); });
      top.appendChild(more);
    }

    $("tg-count").textContent = plural(L.total, "asset");
    const list = $("tg-list");
    list.textContent = "";
    if (!L.items.length) {
      list.appendChild(tgEl("p", "hint", L.loaded && (L.tag || L.q || L.outdated)
        ? "Nothing matches these filters." : "Nothing has been tagged yet."));
    }
    const frag = document.createDocumentFragment();
    L.items.forEach((a, i) => {
      const row = tgEl("div", "desc-row tg-item");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = tagState.selected.has(a.id);
      box.setAttribute("aria-label", `Select ${a.name || a.id}`);
      box.addEventListener("change", () => {
        if (box.checked) tagState.selected.add(a.id); else tagState.selected.delete(a.id);
        tgUpdateSelection();
      });
      const thumb = tgEl("button", "tg-thumb");
      thumb.type = "button";
      thumb.setAttribute("aria-label", `View ${a.name || "photo"} full size`);
      const img = document.createElement("img");
      img.loading = "lazy"; img.decoding = "async"; img.alt = ""; img.src = thumbUrl(a.id);
      img.onerror = () => { img.style.visibility = "hidden"; };
      thumb.appendChild(img);
      if (a.type === "VIDEO") thumb.appendChild(tgEl("span", "tg-vid", "▶"));
      thumb.addEventListener("click", () => openViewer(L.items, i, tgViewerHooks));

      const text = tgEl("div", "tg-text");
      const title = tgEl("p", "");
      title.appendChild(tgEl("b", "", a.name || a.id));
      if (current != null && a.settingsVersion != null && a.settingsVersion < current) {
        const badge = tgEl("span", "badge", "older settings");
        badge.title = "Tagged before your latest settings change";
        title.appendChild(badge);
      }
      text.appendChild(title);
      const date = (a.taken || "").slice(0, 10);
      if (date) text.appendChild(tgEl("small", "", date));
      if ((a.tags || []).length) text.appendChild(tgEl("small", "tg-tagline", a.tags.join(", ")));
      if (a.description) text.appendChild(tgEl("p", "tg-desc", a.description));
      text.addEventListener("click", () => row.classList.toggle("is-open"));
      row.append(box, thumb, text);
      frag.appendChild(row);
    });
    list.appendChild(frag);

    const pages = Math.max(1, Math.ceil(L.total / L.size));
    $("tg-pager").classList.toggle("is-hidden", pages <= 1);
    $("tg-page").textContent = `Page ${L.page} of ${pages}`;
    $("tg-prev").disabled = L.page <= 1;
    $("tg-next").disabled = L.page >= pages;
    tgUpdateSelection();
  }

  const tgGoPage = (page) => { tgLoadAssets(page); $("tg-assets").scrollIntoView({ block: "start" }); };
  $("tg-prev").addEventListener("click", () => tgGoPage(tagState.list.page - 1));
  $("tg-next").addEventListener("click", () => tgGoPage(tagState.list.page + 1));
  $("tg-refresh").addEventListener("click", () => { loadTagger(); tgLoadAssets(); });
  const tgSearch = () => { clearTimeout(tagState.qTimer); tagState.list.q = $("tg-q").value.trim(); tgLoadAssets(1); };
  $("tg-q-go").addEventListener("click", tgSearch);
  $("tg-q").addEventListener("keydown", (e) => { if (e.key === "Enter") tgSearch(); });
  $("tg-q").addEventListener("input", () => { clearTimeout(tagState.qTimer); tagState.qTimer = setTimeout(tgSearch, 500); });
  $("tg-outdated").addEventListener("change", () => { tagState.list.outdated = $("tg-outdated").checked; tgLoadAssets(1); });
  $("tg-sel-page").addEventListener("click", () => {
    tagState.list.items.forEach((a) => tagState.selected.add(a.id));
    [...$("tg-list").querySelectorAll("input[type=checkbox]")].forEach((b) => { b.checked = true; });
    tgUpdateSelection();
  });
  $("tg-sel-none").addEventListener("click", () => {
    tagState.selected.clear();
    [...$("tg-list").querySelectorAll("input[type=checkbox]")].forEach((b) => { b.checked = false; });
    tgUpdateSelection();
  });

  async function tgReprocess(mode) {
    const ids = [...tagState.selected];
    if (!ids.length) return;
    const data = await tagCall("reprocess", { scope: "ids", ids, mode }, (d) =>
      `${plural((d && d.queued) != null ? d.queued : ids.length, "asset")} queued${d && d.settings && d.settings.indexing ? "." : " — press Start tagging to begin."}`);
    if (!data) return;
    tagState.selected.clear();
    tgLoadAssets();
  }
  $("tg-act-retag").addEventListener("click", () => tgReprocess("retag"));
  $("tg-act-describe").addEventListener("click", () => tgReprocess("describe"));
  $("tg-act-full").addEventListener("click", () => tgReprocess("full"));

  $("tg-act-remove").addEventListener("click", async () => {
    const ids = [...tagState.selected];
    if (!ids.length) return;
    const exclude = await tgModal(`Remove the AI text from ${plural(ids.length, "asset")}?`, "Remove AI text", (box) => {
      box.appendChild(tgEl("p", "hint", "The [AI Tagger] block is taken out of their descriptions and the results are forgotten. "
        + "Anything you wrote yourself stays."));
      const row = tgEl("label", "check");
      const input = document.createElement("input");
      input.type = "checkbox";
      row.append(input, tgEl("span", "", "And don't tag them again"));
      box.appendChild(row);
      return () => input.checked;
    }, true);
    if (exclude === null) return;
    busy(true, "Removing…");
    try {
      const r = await api("/api/aitagger/remove", { method: "POST", body: JSON.stringify({ ids, exclude }) });
      toast(`Removed the AI text from ${plural(r.removed != null ? r.removed : ids.length, "asset")}${exclude ? `; ${r.excluded != null ? r.excluded : ids.length} will not be tagged again` : ""}.`);
      tagState.selected.clear();
      loadTagger();
      tgLoadAssets();
    } catch (err) { tagError(err); } finally { busy(false); }
  });

  // Every tagged asset made with older settings, in one go.
  $("tg-update-outdated").addEventListener("click", async () => {
    const n = (tagState.data && tagState.data.counts.outdated) || 0;
    const mode = await tgModal(`Update ${plural(n, "asset")}?`, "Update", (box) => {
      box.appendChild(tgEl("p", "hint", "These were tagged before your latest settings change. Re-tag is enough for strictness, "
        + "rules, blocked tags and the like; pick Re-describe after changing the instructions, and a full re-process after "
        + "changing the captures or the taggers."));
      const select = tgModeSelect(box, "retag", "retag");
      return () => select.value;
    });
    if (mode === null) return;
    const data = await tagCall("reprocess", { scope: "outdated", mode }, (d) =>
      `${plural((d && d.queued) != null ? d.queued : n, "asset")} queued${d && d.settings && d.settings.indexing ? "." : " — press Start tagging to begin."}`);
    if (data) tgLoadAssets();
  });

  // ------------------------------------------------------------ smart albums
  // (named "themes" in the code and API; rules were merged into them)

  const themesState = { themes: [], editing: null, preview: null };
  const tSource = segmented("t-source", (v) => {
    $("t-description-field").classList.toggle("is-hidden", v !== "text");
    $("t-like-field").classList.toggle("is-hidden", v !== "like");
    if (v === "none") { tMode.set("top"); $("t-filters").open = true; }
  });
  const tMode = segmented("t-mode", (v) => {
    $("t-cutoff-field").classList.toggle("is-hidden", v !== "cutoff");
    $("t-limit-field").classList.toggle("is-hidden", v !== "top");
    renderThemePreview();
  });
  const ENGINE_DEFAULT_CUTOFF = { immich: 0.1, searchplus: 0.17 };
  const tEngine = segmented("t-engine", (v) => {
    $("t-engine-hint").textContent = v === "searchplus"
      ? "Scores with the Search+ index (the bigger model). Its scores sit higher — around 0.17 is a typical cut-off — "
        + "so preview before saving. Only photos already in the Search+ index can be added; it keeps itself up to date."
      : "Scores with Immich's own smart-search model (the same one as the Search tab).";
    const other = v === "searchplus" ? "immich" : "searchplus";
    if (Math.abs(Number($("t-cutoff").value) - ENGINE_DEFAULT_CUTOFF[other]) < 1e-9 && tSource.get() === "text")
      $("t-cutoff").value = ENGINE_DEFAULT_CUTOFF[v].toFixed(3);
    themesState.preview = null; $("t-grid").textContent = ""; $("t-count").textContent = "";
  });
  const tPeopleMatch = segmented("t-people-match");
  const tPeople = peoplePicker("t-people-input", "t-people-add", "t-people-chips",
    (ids) => $("t-people-match").classList.toggle("is-hidden", ids.length < 2));
  const tSkip = chipList("t-skip-input", "t-skip-add", "t-skip-chips");

  async function loadThemes() {
    try {
      const data = await api("/api/themes");
      themesState.themes = data.themes || [];
      const sch = data.schedule || {};
      $("themes-schedule").textContent = sch.active
        ? `Smart albums with “Run every hour” run automatically while WSL is running${sch.next ? ` · next run ${sch.next}` : ""}.`
        : "The hourly schedule is not active — smart albums only run when you press Run.";
      renderThemes();
    } catch (err) { toast(err.message, true); }
  }

  function themeSummary(t) {
    const what = t.source === "like" ? `like photo ${String(t.like || "").slice(0, 8)}…`
      : t.source === "none" ? "people only" : `“${t.description}”`;
    const how = t.mode === "top" ? `best ${Number(t.limit).toLocaleString()}` : `≥ ${Number(t.cutoff).toFixed(3)}`;
    const bits = [what, how];
    if (t.engine === "searchplus") bits.push("Search+ model");
    if (t.media === "VIDEO") bits.push("only videos"); else if (t.media === "IMAGE") bits.push("only photos");
    if (t.people && t.people.length) {
      const names = t.people.map((id) => (state.people.find((p) => p.id === id) || {}).name || "someone");
      bits.push(`with ${names.join(t.people_match === "any" ? " or " : " & ")}`);
    }
    if (t.taken_after || t.taken_before) bits.push(`taken ${t.taken_after || "…"} – ${t.taken_before || "…"}`);
    if (t.exclude_albums && t.exclude_albums.length) bits.push(`skip ${t.exclude_albums.join(", ")}`);
    if (t.only_unfiled) bits.push("only unfiled");
    if (t.all_of && t.all_of.length) bits.push(`+ ${t.all_of.join(", ")}`);
    if (t.none_of && t.none_of.length) bits.push(`− ${t.none_of.join(", ")}`);
    if (t.archive) bits.push("archives"); if (t.favorite) bits.push("favourites");
    return bits.join(" · ");
  }

  function renderThemes() {
    const box = $("themes-rows");
    box.textContent = "";
    if (!themesState.themes.length) {
      const p = document.createElement("p"); p.className = "hint";
      p.textContent = "No smart albums yet — press “+ New smart album”, or build a search and press “Save as smart album…”.";
      box.appendChild(p);
    }
    themesState.themes.forEach((t) => {
      const row = document.createElement("div");
      row.className = "rule-row" + (t.enabled ? "" : " is-off");
      const left = document.createElement("div");
      const name = document.createElement("strong");
      name.textContent = t.name + (t.enabled ? "" : " (not hourly)");
      const detail = document.createElement("small");
      const last = t.lastRun ? `last run ${new Date(t.lastRun).toLocaleString()}: +${t.lastAdded} (${t.lastMatched ?? "?"} match)` : "never run";
      detail.textContent = `${themeSummary(t)} → album “${t.album}” · ${last}` + (t.lastError ? ` · ERROR: ${t.lastError}` : "");
      left.append(name, detail);
      const actions = document.createElement("div");
      actions.className = "row-actions";
      const mk = (label, fn, cls = "btn btn-quiet") => {
        const b = document.createElement("button"); b.type = "button"; b.className = cls; b.textContent = label;
        b.addEventListener("click", fn); actions.appendChild(b);
      };
      mk("Run now", () => runThemes([t.id]));
      mk("Edit", () => openThemeEditor(t));
      mk("Delete", () => deleteTheme(t), "btn btn-quiet danger-text");
      row.append(left, actions);
      box.appendChild(row);
    });
  }

  function openThemeEditor(theme, prefill = null) {
    const t = theme || prefill || {};
    themesState.editing = theme || null;
    themesState.preview = null;
    $("theme-editor-title").textContent = theme ? `Edit “${theme.name}”` : "New smart album";
    tSource.set(t.source || "text");
    $("t-cutoff").value = Number(t.cutoff ?? 0.1).toFixed(3);
    tEngine.set(t.engine || "immich");
    $("t-description").value = t.description || "";
    $("t-like").value = t.like || "";
    $("t-name").value = t.name || "";
    $("t-album").value = t.album || "";
    tMode.set(t.mode || "cutoff");
    $("t-cutoff").value = Number(t.cutoff ?? 0.1).toFixed(3);
    $("t-limit").value = String(t.limit || 200);
    $("t-enabled").checked = theme ? theme.enabled !== false : !(prefill && prefill.enabled === false);
    $("t-media").value = t.media || "";
    tPeople.set(t.people || []);
    tPeopleMatch.set(t.people_match || "all");
    $("t-after").value = t.taken_after || "";
    $("t-before").value = t.taken_before || "";
    tSkip.set(t.exclude_albums || []);
    $("t-unfiled").checked = !!t.only_unfiled;
    $("t-allof").value = (t.all_of || []).join(", ");
    $("t-noneof").value = (t.none_of || []).join(", ");
    $("t-archive").checked = !!t.archive;
    $("t-favorite").checked = !!t.favorite;
    $("t-filters").open = Boolean((t.people && t.people.length) || t.media || t.taken_after || t.taken_before
      || (t.exclude_albums && t.exclude_albums.length) || t.only_unfiled || (t.all_of && t.all_of.length)
      || (t.none_of && t.none_of.length) || t.archive || t.favorite);
    $("t-forget").classList.toggle("is-hidden", !theme);
    $("t-grid").textContent = "";
    $("t-count").textContent = "";
    $("theme-editor").classList.remove("is-hidden");
    $("theme-editor").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function editorSpec() {
    const description = $("t-description").value.trim();
    const label = description || ($("t-like").value.trim() ? "Similar photos" : "People");
    return {
      id: themesState.editing ? themesState.editing.id : undefined,
      source: tSource.get(),
      engine: tEngine.get(),
      description,
      like: $("t-like").value.trim() || null,
      name: $("t-name").value.trim() || label,
      album: $("t-album").value.trim() || $("t-name").value.trim() || label,
      mode: tMode.get(),
      cutoff: Number($("t-cutoff").value),
      limit: Math.round(Number($("t-limit").value) || 0),
      enabled: $("t-enabled").checked,
      media: $("t-media").value,
      people: tPeople.get(),
      people_match: tPeopleMatch.get(),
      taken_after: $("t-after").value || null,
      taken_before: $("t-before").value || null,
      exclude_albums: tSkip.get(),
      only_unfiled: $("t-unfiled").checked,
      all_of: splitTerms($("t-allof").value),
      none_of: splitTerms($("t-noneof").value),
      archive: $("t-archive").checked,
      favorite: $("t-favorite").checked,
    };
  }

  function renderThemePreview() {
    const data = themesState.preview;
    if (!data) return;
    const top = tMode.get() === "top";
    const cutoff = Number($("t-cutoff").value) || 0;
    const limit = Math.round(Number($("t-limit").value) || 0);
    const grid = $("t-grid");
    grid.textContent = "";
    const frag = document.createDocumentFragment();
    let inside = 0;
    data.items.forEach((item, index) => {
      const scored = item.score != null;
      const on = top ? index < limit : (!scored || item.score >= cutoff);
      if (on) inside++;
      const tile = document.createElement("button");
      tile.type = "button"; tile.className = "tile pick-tile"; tile.dataset.index = String(index);
      tile.setAttribute("aria-pressed", on ? "true" : "false");
      tile.classList.toggle("below-cut", !on);
      tile.title = scored ? `#${index + 1} · score ${item.score.toFixed(3)} — click: everything up to here matches` : `#${index + 1}`;
      const img = document.createElement("img"); img.loading = "lazy"; img.decoding = "async"; img.alt = ""; img.src = thumbUrl(item.id);
      const rank = document.createElement("span"); rank.className = "rank";
      rank.textContent = scored ? `#${index + 1} · ${item.score.toFixed(3)}` : `#${index + 1}`;
      tile.append(img, rank, openButton());
      frag.appendChild(tile);
    });
    grid.appendChild(frag);
    const all = data.items.length;
    const counts = data.counts || {};
    const buckets = Object.entries(counts).filter(([k]) => k !== "total")
      .map(([k, n]) => `${k}: ${n.toLocaleString()}`).join(" · ");
    let text;
    if (top) text = `Adds the best ${limit.toLocaleString()} of ${Number(counts.total || all).toLocaleString()} photos that pass the filters (preview shows up to ${all}).`;
    else if (!data.scored) text = `${Number(counts.total || all).toLocaleString()} photos match the filters (preview shows up to ${all}).`;
    else text = (inside === all && all > 0
      ? `At ${cutoff.toFixed(3)}, at least ${all} photos match (only the best ${all} are shown). `
      : `At ${cutoff.toFixed(3)}, ${inside.toLocaleString()} photos match. `) + (buckets ? `Counts per cut-off — ${buckets}.` : "");
    $("t-count").textContent = text;
  }

  $("t-grid").addEventListener("click", (e) => {
    const tile = e.target.closest(".tile");
    if (!tile || !themesState.preview) return;
    const index = Number(tile.dataset.index);
    if (e.target.closest(".open-btn")) return openViewer(themesState.preview.items, index, null);
    const item = themesState.preview.items[index];
    if (tMode.get() === "top") $("t-limit").value = String(index + 1);
    else if (item.score != null) $("t-cutoff").value = (Math.floor(item.score * 1000) / 1000).toFixed(3);
    renderThemePreview();
  });
  $("t-cutoff").addEventListener("input", renderThemePreview);
  $("t-limit").addEventListener("input", renderThemePreview);
  $("t-preview").addEventListener("click", async () => {
    const spec = editorSpec();
    if (spec.source === "text" && !spec.description) return toast("Describe what the photos should show first.", true);
    if (spec.source === "like" && !spec.like) return toast("Paste the ID of the photo to match.", true);
    if (spec.source === "none" && !spec.people.length) return toast("Pick at least one person under Filters.", true);
    busy(true, "Scoring your whole library…");
    try {
      themesState.preview = await api("/api/themes/preview", { method: "POST", body: JSON.stringify({ theme: spec, limit: 300 }) });
      renderThemePreview();
    } catch (err) { toast(err.message, true); } finally { busy(false); }
  });

  $("t-save").addEventListener("click", async () => {
    const theme = editorSpec();
    busy(true, "Saving…");
    try {
      const data = await api("/api/themes/save", { method: "POST", body: JSON.stringify({ theme }) });
      themesState.editing = data.theme;
      toast(`Saved “${data.theme.name}”. Press Run now to fill the album straight away.`);
      await loadThemes();
    } catch (err) { toast(err.message, true); } finally { busy(false); }
  });
  $("t-forget").addEventListener("click", async () => {
    if (!themesState.editing) return;
    if (!confirm("Let this smart album add back photos you removed from its album (or undid) on its next run?")) return;
    try { await api("/api/themes/forget", { method: "POST", body: JSON.stringify({ id: themesState.editing.id }) }); toast("Done."); }
    catch (err) { toast(err.message, true); }
  });
  $("t-cancel").addEventListener("click", () => $("theme-editor").classList.add("is-hidden"));
  $("themes-new").addEventListener("click", () => openThemeEditor(null));
  $("themes-run-all").addEventListener("click", () => runThemes(null));

  async function runThemes(ids) {
    busy(true, ids ? "Running smart album…" : "Running all hourly smart albums…");
    try {
      const data = await api("/api/themes/run", { method: "POST", body: JSON.stringify(ids ? { ids } : {}) });
      const errs = data.results.filter((r) => r.error);
      const added = data.results.reduce((n, r) => n + (r.added || 0), 0);
      toast(`Added ${added} photo(s) across ${data.results.length} smart album(s).${errs.length ? ` ${errs.length} failed: ${errs[0].error}` : ""}`, errs.length > 0);
      await Promise.all([loadThemes(), loadAlbums()]);
      albumsState.loaded = false;
    } catch (err) { toast(err.message, true); } finally { busy(false); }
  }

  async function deleteTheme(t) {
    if (!confirm(`Delete the smart album “${t.name}”? It stops filling “${t.album}”.`)) return;
    const deleteAlbum = t.albumId && confirm(`Also delete the album “${t.album}”? (Photos are kept; undo is in History.)\n\nOK = delete the album too\nCancel = keep the album`);
    try {
      await api("/api/themes/delete", { method: "POST", body: JSON.stringify({ id: t.id, deleteAlbum: Boolean(deleteAlbum) }) });
      toast(`Deleted smart album “${t.name}”${deleteAlbum ? " and its album" : ""}.`);
      await loadThemes();
      albumsState.loaded = false;
    } catch (err) { toast(err.message, true); }
  }

  // ------------------------------------------------------------------- faces

  const facesState = { loaded: false, people: [], selected: new Set() };
  const personThumb = (id) => `/thumb/person/${encodeURIComponent(id)}?t=${encodeURIComponent(token)}`;

  function visibleFaces() {
    const show = $("faces-show").value;
    return facesState.people.filter((p) => show === "all" || (show === "hidden" ? p.hidden : !p.hidden));
  }

  function renderFaces() {
    const grid = $("faces-grid");
    grid.textContent = "";
    const list = visibleFaces();
    const frag = document.createDocumentFragment();
    list.forEach((person) => {
      const tile = document.createElement("button");
      tile.type = "button";
      tile.className = "tile face-tile" + (person.hidden ? " is-hidden-person" : "");
      tile.dataset.id = person.id;
      tile.setAttribute("aria-pressed", facesState.selected.has(person.id) ? "true" : "false");
      tile.title = `${person.faces} face(s) · sharpest ${person.best ?? "not measured"}${person.hidden ? " · hidden" : ""}`;
      const img = document.createElement("img");
      img.loading = "lazy"; img.decoding = "async"; img.alt = "unnamed person";
      img.src = personThumb(person.id);
      const mark = document.createElement("span");
      mark.className = "mark"; mark.textContent = "✓";
      const rank = document.createElement("span");
      rank.className = "rank";
      rank.textContent = person.best == null ? "?" : `${Math.round(person.best)}${person.hidden ? " · hidden" : ""}`;
      tile.append(img, mark, rank);
      frag.appendChild(tile);
    });
    grid.appendChild(frag);
    updateFaceCounts();
  }

  function updateFaceCounts() {
    const list = visibleFaces();
    const sel = list.filter((p) => facesState.selected.has(p.id)).length;
    $("faces-count").textContent = `${list.length.toLocaleString()} people · ${sel.toLocaleString()} selected`;
    $("faces-hide").disabled = !sel;
    $("faces-unhide").disabled = !sel;
  }

  $("faces-grid").addEventListener("click", (e) => {
    const tile = e.target.closest(".tile");
    if (!tile) return;
    const id = tile.dataset.id;
    if (facesState.selected.has(id)) facesState.selected.delete(id); else facesState.selected.add(id);
    tile.setAttribute("aria-pressed", facesState.selected.has(id) ? "true" : "false");
    updateFaceCounts();
  });

  async function loadFaces() {
    busy(true, "Loading people…");
    try {
      const data = await api("/api/faces");
      facesState.loaded = true;
      facesState.people = data.people || [];
      facesState.selected.clear();
      $("faces-meta").textContent = data.measured
        ? `Measured ${new Date(data.generated).toLocaleString()}. Press Re-measure after new uploads.`
        : `Not measured yet (${data.unnamed} unnamed people). Press Re-measure — it takes about 20 seconds.`;
      renderFaces();
    } catch (err) {
      toast(err.message, true);
    } finally {
      busy(false);
    }
  }

  async function setHidden(hidden) {
    const ids = visibleFaces().filter((p) => facesState.selected.has(p.id)).map((p) => p.id);
    if (!ids.length) return;
    if (hidden && !confirm(`Hide ${ids.length} people from the People page? Nothing is deleted; you can unhide them here.`)) return;
    busy(true, hidden ? "Hiding…" : "Unhiding…");
    try {
      const data = await api("/api/faces/visibility", { method: "POST", body: JSON.stringify({ ids, hidden }) });
      toast(`${hidden ? "Hid" : "Unhid"} ${data.changed}${data.failed ? `, ${data.failed} failed` : ""}.`);
      const changed = new Set(ids);
      facesState.people.forEach((p) => { if (changed.has(p.id)) p.hidden = hidden; });
      facesState.selected.clear();
      renderFaces();
    } catch (err) {
      toast(err.message, true);
    } finally {
      busy(false);
    }
  }

  $("faces-show").addEventListener("change", renderFaces);
  $("faces-select-cut").addEventListener("click", () => {
    const cut = Number($("faces-cut").value);
    visibleFaces().forEach((p) => { if (p.best != null && p.best < cut) facesState.selected.add(p.id); });
    renderFaces();
  });
  $("faces-clear").addEventListener("click", () => { facesState.selected.clear(); renderFaces(); });
  $("faces-hide").addEventListener("click", () => setHidden(true));
  $("faces-unhide").addEventListener("click", () => setHidden(false));
  $("faces-measure").addEventListener("click", async () => {
    busy(true, "Measuring every face… (about 20 seconds)");
    try {
      const data = await api("/api/faces/measure", { method: "POST", body: "{}" });
      toast(`Measured ${data.faces.toLocaleString()} faces of ${data.people.toLocaleString()} people.`);
      facesState.loaded = false;
    } catch (err) { toast(err.message, true); } finally { busy(false); }
    loadFaces();
  });
  $("faces-covers").addEventListener("click", async () => {
    if (!confirm("Set every unnamed person's cover photo to their sharpest face? Named people are not changed.")) return;
    busy(true, "Updating covers…");
    try {
      const data = await api("/api/faces/covers", { method: "POST", body: "{}" });
      toast(`Updated ${data.changed} covers${data.failed ? `, ${data.failed} failed` : ""}. Immich regenerates them in the background.`);
    } catch (err) { toast(err.message, true); } finally { busy(false); }
  });

  // -------------------------------------------------------------- bootstrap

  async function loadAlbums() {
    try {
      state.albums = await api("/api/albums");
      const list = $("album-list");
      list.textContent = "";
      state.albums.forEach((album) => {
        const option = document.createElement("option");
        option.value = album.name;
        option.label = `${album.count} items`;
        list.appendChild(option);
      });
    } catch { /* the status line already reports connection trouble */ }
  }

  async function loadPeople() {
    try {
      const data = await api("/api/people");
      state.people = data.people || [];
      state.peopleByLabel = new Map();
      const counts = {};
      const list = $("people-list");
      list.textContent = "";
      state.people.forEach((person) => {
        counts[person.name] = (counts[person.name] || 0) + 1;
        const label = counts[person.name] > 1 ? `${person.name} (${counts[person.name]})` : person.name;
        state.peopleByLabel.set(label, person);
        const option = document.createElement("option");
        option.value = label;
        list.appendChild(option);
      });
      $("people-hint").textContent = `${state.people.length} named people. Pick several to find photos where they all ` +
        "appear together. Leave the description empty to get every photo of them (newest first)." +
        (data.unnamed ? ` ${data.unnamed} unnamed ${data.unnamed === 1 ? "person" : "people"} can't be picked until you name them in Immich.` : "");
      searchPeople.render();
      rulePeople.render();
    } catch { /* people are optional */ }
  }

  async function loadRules() {
    try { renderRules(await api("/api/rules")); } catch (err) { toast(err.message, true); }
  }

  async function boot() {
    if (!token) {
      askForToken("Locked — enter the access key below.");
      return;
    }
    $("status").classList.remove("bad");
    try {
      const status = await api("/api/status");
      $("status").textContent = status.connected
        ? `${status.user} · Immich ${status.version}`
        : `Not connected: ${status.error}`;
      $("status").classList.toggle("bad", !status.connected);
    } catch (err) {
      $("status").textContent = err.message;
      $("status").classList.add("bad");
      return;
    }
    await loadPrefs();
    await loadAlbums();
    await loadPeople();
    await loadRules();
  }

  // Sort choices are remembered on the server, shared with the phone app.
  const prefs = { albumsSort: "name", albumSort: "taken_desc", searchEngine: "immich" };
  async function loadPrefs() {
    try {
      Object.assign(prefs, await api("/api/prefs"));
      $("albums-sort").value = prefs.albumsSort;
      searchEngine.set(prefs.searchEngine || "immich");
      if (albumsState.loaded) renderAlbumList();
    } catch { /* defaults are fine */ }
    searchEngine.set(searchEngine.get());       // fills in the hint
    searchEngineReady = true;
  }
  async function savePref(key, value) {
    if (prefs[key] === value) return;
    prefs[key] = value;
    try { await api("/api/prefs", { method: "POST", body: JSON.stringify({ changes: { [key]: value } }) }); }
    catch (err) { toast(`Couldn't remember the sort: ${err.message}`, true); }
  }

  $("token-save").addEventListener("click", saveToken);
  $("token-input").addEventListener("keydown", (e) => { if (e.key === "Enter") saveToken(); });
  $("run").addEventListener("click", runSearch);
  $("query").addEventListener("keydown", (e) => { if (e.key === "Enter") runSearch(); });
  $("like").addEventListener("keydown", (e) => { if (e.key === "Enter") runSearch(); });
  $("file").addEventListener("click", fileSelected);
  $("save-as-rule").addEventListener("click", saveSearchAsRule);
  $("select-all").addEventListener("click", () => {
    state.selected = new Set(state.assets.map((a) => a.id));
    renderResults();
  });
  $("select-none").addEventListener("click", () => {
    state.selected.clear();
    renderResults();
  });
  $("preview-rules").addEventListener("click", () => runRules(false));
  $("apply-rules").addEventListener("click", () => runRules(true));
  $("new-rule").addEventListener("click", () => openEditor(-1));
  $("r-save").addEventListener("click", saveEditor);
  $("r-cancel").addEventListener("click", () => $("rule-editor").classList.add("is-hidden"));
  $("refresh-history").addEventListener("click", loadHistory);

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => { /* http on LAN: fine */ });
  }

  boot();
})();
