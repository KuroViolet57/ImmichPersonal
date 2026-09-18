/* Immich Organizer - mobile UI. No framework, no build step. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const TOKEN_KEY = "immich-organizer-token";

  // The token arrives in the URL the CLI prints. Stash it and scrub the URL so
  // it does not linger in history, screenshots, or a shared home-screen link.
  function resolveToken() {
    const fromUrl = new URLSearchParams(location.search).get("t");
    if (fromUrl) {
      try { localStorage.setItem(TOKEN_KEY, fromUrl); } catch { /* private mode */ }
      history.replaceState(null, "", location.pathname);
      return fromUrl;
    }
    try { return localStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; }
  }

  const token = resolveToken();
  const state = { assets: [], selected: new Set(), albums: [] };

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
      throw new Error(res.status === 401
        ? "Access token missing or wrong. Reopen the link the terminal printed."
        : detail);
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
    toastTimer = setTimeout(() => el.classList.add("is-hidden"), bad ? 6000 : 3200);
  }

  function busy(on, text = "Working…") {
    $("veil-text").textContent = text;
    $("veil").classList.toggle("is-hidden", !on);
  }

  const thumbUrl = (id) => `/thumb/${encodeURIComponent(id)}?t=${encodeURIComponent(token)}`;

  function splitTerms(value) {
    return value.split(",").map((s) => s.trim()).filter(Boolean);
  }

  // ------------------------------------------------------------------ chrome

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.remove("is-active"));
      document.querySelectorAll(".panel").forEach((p) => p.classList.remove("is-active"));
      tab.classList.add("is-active");
      $(`panel-${tab.dataset.panel}`).classList.add("is-active");
    });
  });

  let mode = "query";
  document.querySelectorAll(".seg-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      mode = btn.dataset.mode;
      document.querySelectorAll(".seg-btn").forEach((b) => b.classList.remove("is-active"));
      btn.classList.add("is-active");
      $("field-query").classList.toggle("is-hidden", mode !== "query");
      $("field-like").classList.toggle("is-hidden", mode !== "like");
    });
  });

  // ------------------------------------------------------------------ search

  function renderResults() {
    const grid = $("results");
    grid.textContent = "";
    state.assets.forEach((asset, index) => {
      const tile = document.createElement("button");
      tile.className = "tile";
      tile.type = "button";
      tile.setAttribute("aria-pressed", state.selected.has(asset.id) ? "true" : "false");
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

      tile.append(img, mark, rank);
      tile.addEventListener("click", () => {
        if (state.selected.has(asset.id)) state.selected.delete(asset.id);
        else state.selected.add(asset.id);
        tile.setAttribute("aria-pressed", state.selected.has(asset.id) ? "true" : "false");
        updateCounts();
      });
      grid.appendChild(tile);
    });
    updateCounts();
  }

  function updateCounts() {
    $("results-count").textContent =
      `${state.assets.length} result${state.assets.length === 1 ? "" : "s"} · ${state.selected.size} selected`;
    $("file-count").textContent = String(state.selected.size);
    $("file").disabled = state.selected.size === 0;
  }

  async function runSearch() {
    const body = {
      limit: Number($("limit").value) || 60,
      filters: {},
      refine: { all_of: splitTerms($("allOf").value), none_of: splitTerms($("noneOf").value) },
    };
    if (mode === "query") {
      body.query = $("query").value.trim();
      if (!body.query) return toast("Describe what you are looking for first.", true);
    } else {
      body.like = $("like").value.trim();
      if (!body.like) return toast("Paste an asset ID to match against.", true);
    }
    if ($("type").value) body.filters.type = $("type").value;
    if ($("takenAfter").value) body.filters.taken_after = $("takenAfter").value;
    if ($("takenBefore").value) body.filters.taken_before = $("takenBefore").value;
    if ($("unfiled").checked) body.filters.only_unfiled = true;

    busy(true, "Searching your library…");
    try {
      const data = await api("/api/search", { method: "POST", body: JSON.stringify(body) });
      state.assets = data.assets || [];
      // Everything starts selected; deselecting the tail is faster than
      // picking winners out of a long list.
      state.selected = new Set(state.assets.map((a) => a.id));
      renderResults();
      $("results-card").classList.remove("is-hidden");
      $("file-card").classList.toggle("is-hidden", state.assets.length === 0);
      toast(state.assets.length ? `Found ${state.assets.length}.` : "No matches.");
    } catch (err) {
      toast(err.message, true);
    } finally {
      busy(false);
    }
  }

  async function fileSelected() {
    const album = $("album").value.trim();
    if (!album) return toast("Name the album to add them to.", true);
    const assetIds = state.assets.map((a) => a.id).filter((id) => state.selected.has(id));
    if (!assetIds.length) return toast("Nothing selected.", true);

    busy(true, `Adding ${assetIds.length} to ${album}…`);
    try {
      const data = await api("/api/file", {
        method: "POST",
        body: JSON.stringify({
          assetIds, album,
          createAlbum: true,
          archive: $("archive").checked,
          favorite: $("favorite").checked,
        }),
      });
      const bits = [`Added ${data.added} to “${data.album}”`];
      if (data.created) bits.push("(album created)");
      if (data.duplicates) bits.push(`${data.duplicates} already there`);
      toast(bits.join(" "));
      if (data.failures && data.failures.length) {
        toast(`${data.failures.length} failed: ${data.failures[0]}`, true);
      }
      await loadAlbums();
    } catch (err) {
      toast(err.message, true);
    } finally {
      busy(false);
    }
  }

  // ------------------------------------------------------------------- rules

  function renderRules(data) {
    const list = $("rules-list");
    list.textContent = "";
    $("rules-path").textContent = data.loaded
      ? data.path
      : (data.error || "No rules file loaded — start the server with --rules.");

    const hasRules = Boolean(data.rules && data.rules.length);
    $("preview-rules").disabled = !hasRules;
    $("apply-rules").disabled = !hasRules;
    if (!hasRules) return;

    data.rules.forEach((rule) => {
      const row = document.createElement("div");
      row.className = "rule-row" + (rule.enabled ? "" : " is-off");
      const left = document.createElement("div");
      const name = document.createElement("strong");
      name.textContent = rule.name;
      const detail = document.createElement("small");
      detail.textContent = `${rule.match} → ${rule.album} (top ${rule.limit})`;
      left.append(name, detail);
      const btn = document.createElement("button");
      btn.className = "btn btn-quiet";
      btn.textContent = "Preview";
      btn.disabled = !rule.enabled;
      btn.addEventListener("click", () => runRules(false, [rule.name]));
      row.append(left, btn);
      list.appendChild(row);
    });
  }

  function renderPlan(data) {
    const body = $("plan-body");
    body.textContent = "";
    $("plan-card").classList.remove("is-hidden");

    const head = document.createElement("p");
    head.className = "hint";
    head.textContent = data.applied
      ? `Applied — ${data.added} asset(s) added.${data.createdAlbums?.length ? ` Created: ${data.createdAlbums.join(", ")}.` : ""}`
      : `${data.totalToAdd} asset(s) would be added.${data.newAlbums?.length ? ` New albums: ${data.newAlbums.join(", ")}.` : ""}`;
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
        entry.assets.slice(0, 24).forEach((asset) => {
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
  }

  async function runRules(apply, only = null) {
    if (apply && !confirm("Apply every enabled rule? This adds photos to albums for real.")) return;
    busy(true, apply ? "Applying rules…" : "Building plan…");
    try {
      const data = await api(apply ? "/api/apply" : "/api/plan", {
        method: "POST",
        body: JSON.stringify(only ? { only } : {}),
      });
      renderPlan(data);
      toast(apply ? `Applied: ${data.added} added.` : `Plan ready: ${data.totalToAdd} to add.`);
    } catch (err) {
      toast(err.message, true);
    } finally {
      busy(false);
    }
  }

  // -------------------------------------------------------------- bootstrap

  async function loadAlbums() {
    try {
      state.albums = await api("/api/albums");
      const list = $("album-list");
      list.textContent = "";
      state.albums.forEach((album) => {
        const option = document.createElement("option");
        option.value = album.name;
        option.label = `${album.count} photos`;
        list.appendChild(option);
      });
    } catch { /* the status line already reports connection trouble */ }
  }

  async function boot() {
    if (!token) {
      $("status").textContent = "No access token — open the link the terminal printed.";
      $("status").classList.add("bad");
      return;
    }
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
    await loadAlbums();
    try { renderRules(await api("/api/rules")); } catch { /* optional */ }
  }

  $("run").addEventListener("click", runSearch);
  $("query").addEventListener("keydown", (e) => { if (e.key === "Enter") runSearch(); });
  $("like").addEventListener("keydown", (e) => { if (e.key === "Enter") runSearch(); });
  $("file").addEventListener("click", fileSelected);
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

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => { /* http on LAN: fine */ });
  }

  boot();
})();
