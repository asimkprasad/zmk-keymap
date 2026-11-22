(() => {
  "use strict";

  const CSRF = document.querySelector('meta[name="keymap-csrf"]').getAttribute("content");

  const SNAPSHOT_CAP = 5;

  const state = {
    workspaceRoot: "",
    files: { snapshots: [] },
    current: null,          // SnapshotPayload
    lastLive: null,         // most recent live keyboard SnapshotPayload, if read
    lastLiveReadAt: null,   // epoch ms when lastLive was read from hardware
    readingLive: false,
    liveError: null,        // last live read error string, if currently unread
    importedSnapshot: null, // most recently imported external JSON SnapshotPayload
    lastUndoPath: null,     // latest pre-restore backup path so Undo survives banner dismiss
    showAllSnapshots: false,
    layerIndex: 0,
    rawMode: false,
    allLayers: false,
    selectedPos: null,
    diff: null,             // { kind: "restore"|"offline", proposal, planDigest, diffLayers, baseline, boardMode: "diff"|"target"|"baseline", forceDevice: false }
    busy: false,
  };

  const $ = (id) => document.getElementById(id);

  // Explicit character-entity map so esc() is safe in both HTML text nodes AND
  // single- or double-quoted HTML attribute values (never DOM textContent/innerHTML,
  // which leaves '"' and "'" unescaped).
  const _ESC_MAP = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  };

  function esc(str) {
    return (str == null ? "" : String(str)).replace(/[&<>"']/g, (ch) => _ESC_MAP[ch]);
  }

  function formatRelativeAgo(epochMs) {
    if (!epochMs) return "";
    const sec = Math.max(0, Math.floor((Date.now() - epochMs) / 1000));
    if (sec < 45) return "just now";
    const mins = Math.floor(sec / 60);
    if (mins < 60) return `${mins} min${mins === 1 ? "" : "s"} ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs} hr${hrs === 1 ? "" : "s"} ago`;
    const days = Math.floor(hrs / 24);
    return `${days}d ago`;
  }

  function formatSnapshotTimestamp(isoStr, fallback) {
    if (!isoStr) return fallback || "Snapshot";
    const dt = new Date(isoStr);
    if (Number.isNaN(dt.getTime())) return fallback || isoStr;
    const now = new Date();
    const sameDay = dt.toDateString() === now.toDateString();
    const yesterday = new Date(now);
    yesterday.setDate(now.getDate() - 1);
    const isYesterday = dt.toDateString() === yesterday.toDateString();
    const timePart = dt.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    if (sameDay) return `Today, ${timePart}`;
    if (isYesterday) return `Yesterday, ${timePart}`;
    const datePart = dt.toLocaleDateString([], { month: "short", day: "numeric" });
    return `${datePart}, ${timePart}`;
  }

  async function apiGet(path) {
    const res = await fetch(path, {
      headers: {
        "Accept": "application/json",
        "X-Keymap-Token": CSRF,
      },
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }

  async function apiPost(path, body) {
    const res = await fetch(path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Keymap-Token": CSRF,
      },
      body: JSON.stringify(body || {}),
    });
    const data = await res.json();
    if (!res.ok) {
      const err = new Error(data.error || `HTTP ${res.status}`);
      err.data = data;
      throw err;
    }
    return data;
  }

  function setBusy(flag) {
    state.busy = flag;
    for (const id of ["btn-refresh-live", "btn-backup", "btn-plan-restore"]) {
      const el = $(id);
      if (el) el.disabled = flag;
    }
  }

  function showAlert(kind, htmlContent, undoPath) {
    const area = $("alert-area");
    if (!kind) {
      area.innerHTML = "";
      fitStageBoard();
      return;
    }
    let actions = `<button type="button" class="sm" id="alert-dismiss">Dismiss</button>`;
    if (undoPath) {
      actions = `<button type="button" class="primary sm" id="alert-undo">Undo (Plan Restore from Backup)</button>` + actions;
    }
    area.innerHTML = `
      <div class="banner ${kind}">
        <div class="banner-row">
          <div>${htmlContent}</div>
          <div style="display:flex;gap:6px">${actions}</div>
        </div>
      </div>`;
    fitStageBoard();
    const dismiss = $("alert-dismiss");
    if (dismiss) dismiss.addEventListener("click", () => { area.innerHTML = ""; fitStageBoard(); });
    const undoBtn = $("alert-undo");
    if (undoBtn) {
      undoBtn.addEventListener("click", async () => {
        await loadWorkspacePath(undoPath, true);
      });
    }
  }

  function renderLiveCard() {
    const card = $("live-card");
    const dot = $("live-dot");
    const nameEl = $("live-device-name");
    const statusEl = $("live-status-text");
    const refreshBtn = $("btn-refresh-live");
    const hintEl = $("live-hint");
    const specsEl = $("live-specs");

    const isViewingLive = Boolean(state.current && state.current.source && state.current.source.kind === "live");
    card.classList.toggle("active", isViewingLive);
    refreshBtn.classList.toggle("spinning", state.readingLive);

    const specSource = state.lastLive || state.current;
    const knownName = (specSource && specSource.device && specSource.device.name)
      || (state.files.snapshots && state.files.snapshots[0] && state.files.snapshots[0].device_name)
      || "Keyboard";
    nameEl.textContent = knownName;

    if (specSource) {
      const specRows = [
        ["id", specSource.device.id || "—"],
        ["layout", specSource.layout.name || "—"],
        ["keys", String(specSource.layout.keys.length)],
        ["layers", String(specSource.layers.length)],
        ["behaviors", String(specSource.behaviors.length)],
      ];
      specsEl.style.display = "grid";
      specsEl.innerHTML = specRows
        .map(([k, v]) => `<span>${esc(k)}</span><code>${esc(v)}</code>`)
        .join("");
    } else {
      specsEl.style.display = "none";
      specsEl.innerHTML = "";
    }

    if (state.readingLive) {
      dot.className = "status-dot busy";
      statusEl.textContent = "Reading live keymap…";
      hintEl.style.display = "none";
      return;
    }

    if (state.lastLive && state.lastLiveReadAt) {
      dot.className = "status-dot ok";
      statusEl.textContent = `Live view · taken ${formatRelativeAgo(state.lastLiveReadAt)}`;
      hintEl.style.display = "none";
      return;
    }

    dot.className = "status-dot offline";
    statusEl.textContent = "Not read yet · click ↻";
    if (state.liveError) {
      hintEl.style.display = "block";
      hintEl.textContent = "Unlock ZMK Studio on the keyboard and close the Studio web app, then click ↻.";
      hintEl.title = state.liveError;
    } else {
      hintEl.style.display = "none";
    }
  }

  function renderSidebar() {
    $("brand-header").title = state.workspaceRoot ? `Workspace: ${state.workspaceRoot}` : "";
    renderLiveCard();

    const activeKind = state.current && state.current.source ? state.current.source.kind : null;
    const activePath = state.current && state.current.source ? state.current.source.path : null;

    // Backup section: newest-first snapshots capped at SNAPSHOT_CAP
    const snaps = state.files.snapshots || [];
    const listEl = $("list-snapshots");
    const moreBtn = $("btn-toggle-more");
    if (!snaps.length) {
      listEl.innerHTML = `<li class="empty-note">No snapshots saved yet</li>`;
      moreBtn.style.display = "none";
    } else {
      const visible = state.showAllSnapshots ? snaps : snaps.slice(0, SNAPSHOT_CAP);
      listEl.innerHTML = visible.map((f) => {
        const isAct = activePath && activePath === f.rel_path;
        if (f.error) {
          return `
            <li class="file-item ${isAct ? "active" : ""}" data-rel-path="${esc(f.rel_path)}">
              <div class="file-name"><span>${esc(f.name)}</span></div>
              <div class="file-meta" style="color:var(--err)">invalid snapshot</div>
            </li>`;
        }
        const friendlyWhen = formatSnapshotTimestamp(f.captured_at, f.name);
        return `
          <li class="file-item ${isAct ? "active" : ""}" data-rel-path="${esc(f.rel_path)}" title="${esc(f.rel_path)} · ${esc(f.captured_at)}">
            <div class="file-name">
              <span>${esc(friendlyWhen)}</span>
              <span class="kbd-hint">${esc(f.device_name || "")}</span>
            </div>
            <div class="file-meta">${esc(f.name)}</div>
          </li>`;
      }).join("");
      listEl.querySelectorAll(".file-item").forEach((li) => {
        li.addEventListener("click", () => loadWorkspacePath(li.dataset.relPath, false));
      });

      const hiddenCount = snaps.length - SNAPSHOT_CAP;
      if (hiddenCount > 0) {
        moreBtn.style.display = "inline-flex";
        moreBtn.textContent = state.showAllSnapshots
          ? "Show fewer ▴"
          : `Show ${hiddenCount} older snapshot${hiddenCount === 1 ? "" : "s"} ▾`;
      } else {
        moreBtn.style.display = "none";
      }
    }

    // Restore section: active imported external JSON (if any)
    const impList = $("list-imported");
    if (state.importedSnapshot) {
      impList.style.display = "flex";
      const isImpActive = activeKind === "imported";
      const imp = state.importedSnapshot;
      impList.innerHTML = `
        <li class="file-item ${isImpActive ? "active" : ""}" id="item-imported">
          <div class="file-name">
            <span>${esc(imp.source.label || "imported.json")}</span>
            <span class="kbd-hint">imported</span>
          </div>
          <div class="file-meta">${esc(imp.device.name || "Keyboard")} · ${imp.layers.length} layers</div>
        </li>`;
      $("item-imported").addEventListener("click", () => setSnapshot(state.importedSnapshot));
    } else {
      impList.style.display = "none";
      impList.innerHTML = "";
    }

    // Restore section: persistent pre-restore Undo snapshot (survives dismissing banner)
    const undoList = $("list-undo");
    if (undoList) {
      if (state.lastUndoPath) {
        undoList.style.display = "flex";
        const isUndoActive = activeKind === "undo" && activePath === state.lastUndoPath;
        const undoName = state.lastUndoPath.split("/").pop() || state.lastUndoPath;
        undoList.innerHTML = `
          <li class="file-item ${isUndoActive ? "active" : ""}" id="item-last-undo">
            <div class="file-name">
              <span>Undo last restore</span>
              <span class="kbd-hint">pre-restore</span>
            </div>
            <div class="file-meta">${esc(undoName)}</div>
          </li>`;
        $("item-last-undo").addEventListener("click", () => {
          loadWorkspacePath(state.lastUndoPath, true);
        });
      } else {
        undoList.style.display = "none";
        undoList.innerHTML = "";
      }
    }

    populateCompareDropdown();
  }

  function populateCompareDropdown() {
    const sel = $("compare-baseline-select");
    const opts = [];
    if (state.lastLive) {
      opts.push(`<option value="__live__">Live view (${esc(state.lastLive.device.name)})</option>`);
    }
    if (state.importedSnapshot) {
      opts.push(`<option value="__imported__">Imported: ${esc(state.importedSnapshot.source.label)}</option>`);
    }
    for (const f of state.files.snapshots || []) {
      if (!f.error) {
        opts.push(`<option value="${esc(f.rel_path)}">${esc(formatSnapshotTimestamp(f.captured_at, f.name))} (${esc(f.name)})</option>`);
      }
    }
    sel.innerHTML = opts.length ? opts.join("") : `<option value="">No other snapshots available</option>`;
  }

  function recordLiveReading(payload) {
    const liveCopy = Object.assign({}, payload, {
      source: { kind: "live", label: "Live keyboard", path: null },
    });
    state.lastLive = liveCopy;
    state.lastLiveReadAt = Date.now();
    state.liveError = null;
    return liveCopy;
  }

  function setSnapshot(payload, preserveLayer = false) {
    state.current = payload;
    if (payload && payload.source && payload.source.kind === "live") {
      recordLiveReading(payload);
    }
    state.diff = null;
    if (!preserveLayer || !payload || state.layerIndex >= payload.layers.length) {
      state.layerIndex = 0;
    }
    if (state.selectedPos != null && payload && state.selectedPos >= payload.layout.keys.length) {
      state.selectedPos = null;
    }
    renderSidebar();
    renderMain();
  }

  function renderMain() {
    const snap = state.current;
    if (!snap) {
      $("empty-stage").style.display = "block";
      $("doc-stage").style.display = "none";
      $("empty-heading").textContent = state.readingLive
        ? "Reading live keymap from keyboard…"
        : "No keymap loaded yet";
      closeInspector();
      return;
    }
    $("empty-stage").style.display = "none";
    $("doc-stage").style.display = "flex";

    $("doc-title").textContent = `${snap.device.name || "Keyboard"} keymap`;
    const badge = $("doc-badge");
    const restoreBtn = $("btn-plan-restore");
    if (state.diff) {
      badge.className = "badge diff";
      badge.textContent = state.diff.kind === "restore" ? "RESTORE PREVIEW" : "OFFLINE DIFF";
    } else if (snap.source.kind === "live") {
      badge.className = "badge live";
      badge.textContent = "LIVE VIEW";
    } else if (snap.source.kind === "imported") {
      badge.className = "badge";
      badge.textContent = `IMPORTED · ${snap.source.label}`;
    } else if (snap.source.kind === "undo") {
      badge.className = "badge";
      badge.textContent = `UNDO SNAPSHOT · ${snap.source.label}`;
    } else {
      badge.className = "badge";
      badge.textContent = `SAVED SNAPSHOT · ${snap.source.label}`;
    }
    // Snapshot-only actions ("Export HTML" & "Restore to Keyboard…") stay hidden on Live view.
    const isLive = snap.source.kind === "live";
    $("btn-export-html").style.display = isLive ? "none" : "inline-flex";
    restoreBtn.style.display = isLive ? "none" : "inline-flex";

    renderDiffBanner();
    renderLayerTabs();
    renderBoards();
    renderLowerDetails();
    renderInspector();
  }

  function renderDiffBanner() {
    const box = $("diff-banner");
    const legendDiff = $("legend-diff-item");
    const changesCard = $("changes-card");

    if (!state.diff) {
      box.style.display = "none";
      legendDiff.style.display = "none";
      changesCard.style.display = "none";
      return;
    }

    box.style.display = "flex";
    legendDiff.style.display = "list-item";
    const d = state.diff;
    const prop = d.proposal;
    const count = prop.changes.length;

    const notes = [];
    if (prop.identity_warning) {
      notes.push(`<li><b style="color:var(--warn)">Different keyboard:</b> ${esc(prop.identity_warning)}</li>`);
    }
    if (prop.remapping && !prop.remapping.is_identity) {
      notes.push(`<li>Behavior IDs shifted across builds; <b>${Number(prop.remapping.matched_count)}</b> bound behaviors were matched safely by display name.</li>`);
    }
    if (prop.remapping && prop.remapping.unused_and_missing && prop.remapping.unused_and_missing.length) {
      notes.push(`<li>Keyboard omits unused behavior(s): <code>${esc(prop.remapping.unused_and_missing.join(", "))}</code>.</li>`);
    }

    const headline = count === 0
      ? `<b style="color:var(--ok)">Already matches</b> — 0 differences across ${Number(prop.compared)} compared bindings.`
      : `<b>${count} of ${Number(prop.compared)}</b> key bindings differ ${d.kind === "restore" ? `on connected <code>${esc(prop.device_name)}</code>` : "between snapshots"}.`;

    const modeBtns = `
      <div class="seg" title="Board display mode">
        <button type="button" data-bmode="diff" class="${d.boardMode === "diff" ? "active" : ""}">Visual Diff</button>
        <button type="button" data-bmode="target" class="${d.boardMode === "target" ? "active" : ""}">Target</button>
        ${d.baseline ? `<button type="button" data-bmode="baseline" class="${d.boardMode === "baseline" ? "active" : ""}">Baseline</button>` : ""}
      </div>`;

    let commitControls = "";
    if (d.kind === "restore" && count > 0) {
      const forceBox = prop.identity_warning
        ? `<label style="font-size:.77rem;display:inline-flex;align-items:center;gap:5px;color:var(--warn);font-weight:600">
             <input type="checkbox" id="chk-force-device" ${d.forceDevice ? "checked" : ""}>
             Restore onto this different keyboard
           </label>`
        : "";
      const disableCommit = Boolean(prop.identity_warning && !d.forceDevice);
      commitControls = `
        ${forceBox}
        <button type="button" class="primary" id="btn-commit-restore" ${disableCommit ? "disabled" : ""}>
          Commit ${count} Change${count === 1 ? "" : "s"} to Keyboard
        </button>`;
    }

    box.innerHTML = `
      <div class="banner-row">
        <div>${headline}</div>
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          ${modeBtns}
          ${commitControls}
          <button type="button" class="sm" id="btn-exit-diff">Exit Diff</button>
        </div>
      </div>
      ${notes.length ? `<ul class="banner-notes">${notes.join("")}</ul>` : ""}
      ${d.kind === "restore" && count > 0 ? `<div class="kbd-hint">Dry run only — nothing has been written yet. Clicking Commit saves a timestamped undo snapshot to <code>undo-snapshots/</code> before writing.</div>` : ""}
    `;

    box.querySelectorAll("[data-bmode]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.diff.boardMode = btn.dataset.bmode;
        renderDiffBanner();
        renderBoards();
      });
    });
    const forceChk = $("chk-force-device");
    if (forceChk) {
      forceChk.addEventListener("change", () => {
        state.diff.forceDevice = forceChk.checked;
        renderDiffBanner();
      });
    }
    const commitBtn = $("btn-commit-restore");
    if (commitBtn) {
      commitBtn.addEventListener("click", commitRestore);
    }
    $("btn-exit-diff").addEventListener("click", () => {
      state.diff = null;
      renderMain();
    });

    if (count > 0) {
      changesCard.style.display = "block";
      $("changes-card-title").textContent = `Changed Bindings (${count})`;
      const layerIdxById = {};
      state.current.layers.forEach((l, idx) => { layerIdxById[l.id] = idx; });
      $("changes-tbody").innerHTML = prop.changes.map((c) => `
        <tr class="clickable" data-jump-layer="${Number(layerIdxById[c.layer_id] || 0)}" data-jump-pos="${Number(c.position)}">
          <td><b>${esc(c.layer_name)}</b> <span class="kbd-hint">id ${Number(c.layer_id)}</span></td>
          <td class="mono">${Number(c.position)}</td>
          <td class="mono">${esc(state.rawMode ? c.before : c.before_friendly)} <span class="kbd-hint">${state.rawMode ? "" : esc(c.before)}</span></td>
          <td class="mono"><b>${esc(state.rawMode ? c.after : c.after_friendly)}</b> <span class="kbd-hint">${state.rawMode ? "" : esc(c.after)}</span></td>
        </tr>`).join("");
      $("changes-tbody").querySelectorAll("tr.clickable").forEach((tr) => {
        tr.addEventListener("click", () => {
          state.layerIndex = Number(tr.dataset.jumpLayer);
          state.selectedPos = Number(tr.dataset.jumpPos);
          renderMain();
        });
      });
    } else {
      changesCard.style.display = "none";
    }
  }

  function renderLayerTabs() {
    const snap = state.current;
    const tabs = $("layer-tabs");
    tabs.style.opacity = state.allLayers ? ".48" : "1";
    const diffCounts = {};
    if (state.diff) {
      for (const c of state.diff.proposal.changes) {
        diffCounts[c.layer_id] = (diffCounts[c.layer_id] || 0) + 1;
      }
    }
    tabs.innerHTML = snap.layers.map((layer, idx) => {
      const cnt = diffCounts[layer.id] || 0;
      const badge = cnt ? `<span class="cbadge">${cnt}</span>` : "";
      const active = !state.allLayers && idx === state.layerIndex ? "active" : "";
      return `
        <button type="button" class="layer-tab ${active}" data-lidx="${idx}">
          <span>${esc(layer.name || `Layer ${layer.id}`)}</span>
          <span class="lid">id ${Number(layer.id)}</span>
          ${badge}
        </button>`;
    }).join("");
    tabs.querySelectorAll(".layer-tab").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.layerIndex = Number(btn.dataset.lidx);
        if (state.allLayers) {
          const el = document.getElementById(`layer-sec-${state.layerIndex}`);
          if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
        } else {
          renderLayerTabs();
          renderBoards();
          renderLowerDetails();
          renderInspector();
        }
      });
    });
  }

  function activeSvgForLayer(idx) {
    if (state.diff) {
      if (state.diff.boardMode === "diff") {
        return state.diff.diffLayers[idx].svg;
      }
      if (state.diff.boardMode === "baseline" && state.diff.baseline) {
        const lid = state.current.layers[idx].id;
        const found = state.diff.baseline.layers.find((l) => l.id === lid);
        if (found) return found.svg;
      }
    }
    return state.current.layers[idx].svg;
  }

  function renderBoards() {
    const snap = state.current;
    const container = $("boards-container");
    const indices = state.allLayers
      ? snap.layers.map((_, i) => i)
      : [state.layerIndex];

    container.innerHTML = indices.map((idx) => {
      const layer = snap.layers[idx];
      const heading = state.allLayers
        ? `<h3 class="layer-block-title">${esc(layer.name)}<span>layer id ${Number(layer.id)}</span></h3>`
        : "";
      return `
        <section class="layer-block" id="layer-sec-${idx}" data-layer-idx="${idx}">
          ${heading}
          <div class="board">${activeSvgForLayer(idx)}</div>
        </section>`;
    }).join("");

    applySelectedKeyHighlight();

    container.querySelectorAll(".key[data-pos]").forEach((g) => {
      g.addEventListener("click", (ev) => {
        const sec = ev.currentTarget.closest(".layer-block");
        if (sec && state.allLayers) {
          state.layerIndex = Number(sec.dataset.layerIdx);
          renderLayerTabs();
          renderLowerDetails();
        }
        state.selectedPos = Number(g.dataset.pos);
        applySelectedKeyHighlight();
        renderInspector();
      });
    });

    fitStageBoard();
  }

  function fitStageBoard() {
    const container = $("boards-container");
    if (!container) return;
    const single = Boolean(state.current && !state.allLayers);
    container.classList.toggle("single-stage", single);
    if (!single || window.innerWidth <= 1120) {
      container.querySelectorAll(".board").forEach((b) => { b.style.minHeight = ""; });
      container.querySelectorAll("svg").forEach((s) => {
        s.style.width = "";
        s.style.height = "";
      });
      return;
    }
    const board = container.querySelector(".board");
    const svg = board && board.querySelector("svg");
    if (!board || !svg) return;

    const ws = document.querySelector(".workspace");
    board.style.minHeight = "0px";
    svg.style.width = "0px";
    svg.style.height = "0px";

    const cs = getComputedStyle(board);
    const padX = (parseFloat(cs.paddingLeft) || 0) + (parseFloat(cs.paddingRight) || 0);
    const padY = (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0)
      + (parseFloat(cs.borderTopWidth) || 0) + (parseFloat(cs.borderBottomWidth) || 0);

    const wsRect = ws.getBoundingClientRect();
    const boardRect = board.getBoundingClientRect();
    const topOffsetInsideWs = Math.max(0, boardRect.top - wsRect.top);

    const legendEl = ws.querySelector(".legend-strip");
    const details = $("lower-details");
    const summaryEl = details ? details.querySelector("summary") : null;
    const legendH = legendEl ? legendEl.getBoundingClientRect().height + 18 : 36;
    const summaryH = summaryEl ? summaryEl.getBoundingClientRect().height + 24 : 44;
    const wsPadBottom = parseFloat(getComputedStyle(ws).paddingBottom) || 18;

    const availBoardOuterH = Math.max(
      320,
      window.innerHeight - topOffsetInsideWs - legendH - summaryH - wsPadBottom - 4
    );
    const availW = Math.max(280, board.clientWidth - padX);
    const availH = Math.max(280, availBoardOuterH - padY);

    const vbParts = (svg.getAttribute("viewBox") || "").trim().split(/[\s,]+/).map(Number);
    const vbW = vbParts.length === 4 && vbParts[2] > 0 ? vbParts[2] : 1568;
    const vbH = vbParts.length === 4 && vbParts[3] > 0 ? vbParts[3] : 605;
    const scale = Math.min(availW / vbW, availH / vbH);

    board.style.minHeight = `${Math.floor(availBoardOuterH)}px`;
    svg.style.width = `${Math.floor(vbW * scale)}px`;
    svg.style.height = `${Math.floor(vbH * scale)}px`;
  }

  function applySelectedKeyHighlight() {
    document.querySelectorAll("#boards-container .key.selected").forEach((el) => {
      el.classList.remove("selected");
    });
    if (state.selectedPos == null) return;
    document.querySelectorAll(`#boards-container .key[data-pos="${Number(state.selectedPos)}"]`).forEach((el) => {
      el.classList.add("selected");
    });
  }

  function renderLowerDetails() {
    const snap = state.current;
    const layer = snap.layers[state.layerIndex];
    if (layer) {
      const lines = Object.values(layer.bindings)
        .sort((a, b) => a.position - b.position)
        .map((b) => `${String(b.position).padStart(3, " ")}  ${b.rendered || b.behavior_name}`);
      $("layer-text-dump").textContent = lines.join("\n");
    }
    $("behavior-table-heading").textContent = `Behavior Table — ${snap.behaviors.length} entries`;
    $("behaviors-tbody").innerHTML = snap.behaviors.map((b) => `
      <tr>
        <td class="mono">${Number(b.id)}</td>
        <td class="mono">${esc(b.display_name)}</td>
        <td class="mono">${Number(b.parameter_sets)}</td>
      </tr>`).join("");
  }

  function closeInspector() {
    state.selectedPos = null;
    $("app").classList.remove("inspector-open");
    applySelectedKeyHighlight();
  }

  function renderInspector() {
    const app = $("app");
    const snap = state.current;
    if (!snap || state.selectedPos == null) {
      app.classList.remove("inspector-open");
      return;
    }
    const pos = state.selectedPos;
    const keyGeom = snap.layout.keys[pos];
    if (!keyGeom) {
      app.classList.remove("inspector-open");
      return;
    }
    app.classList.add("inspector-open");

    $("insp-title").textContent = `Key Position #${pos}`;
    const rot = keyGeom.rotation ? ` · rot ${(keyGeom.rotation / 100).toFixed(1)}°` : "";
    $("insp-geom").textContent = `${(keyGeom.width / 100).toFixed(2)}u × ${(keyGeom.height / 100).toFixed(2)}u · (${keyGeom.x}, ${keyGeom.y})${rot}`;

    const layer = snap.layers[state.layerIndex];
    const binding = layer.bindings[String(pos)];
    $("insp-layer-heading").textContent = `Layer ${layer.id}: ${layer.name}`;

    // Diff box on this layer/key
    const diffBox = $("insp-diff-box");
    const matchingChange = state.diff
      ? state.diff.proposal.changes.find((c) => c.layer_id === layer.id && c.position === pos)
      : null;
    if (matchingChange) {
      diffBox.style.display = "block";
      $("insp-diff-before").textContent = `${matchingChange.before_friendly}  (${matchingChange.before})`;
      $("insp-diff-after").textContent = `${matchingChange.after_friendly}  (${matchingChange.after})`;
    } else {
      diffBox.style.display = "none";
    }

    if (binding) {
      $("insp-rendered").textContent = binding.rendered || binding.behavior_name;
      $("insp-friendly").textContent = binding.friendly;
      $("insp-behavior").textContent = `${binding.behavior_name} (id ${binding.behavior_id})`;
      if (binding.params && binding.params.length) {
        $("insp-params").innerHTML = binding.params.map((p, i) => `
          <div class="kv-row">
            <span>param${i + 1} (${esc(p.name || p.kind)})</span>
            <code>${esc(p.decoded)} · ${esc(p.raw_hex)}</code>
          </div>`).join("");
      } else {
        $("insp-params").innerHTML = `<div class="kbd-hint">No parameters declared</div>`;
      }
    } else {
      $("insp-rendered").textContent = "(unbound)";
      $("insp-friendly").textContent = "—";
      $("insp-behavior").textContent = "—";
      $("insp-params").innerHTML = "";
    }

    $("insp-all-layers").innerHTML = snap.layers.map((l, idx) => {
      const b = l.bindings[String(pos)];
      const activeStyle = idx === state.layerIndex ? "font-weight:700;color:var(--accent)" : "";
      return `
        <tr class="clickable" data-insp-lidx="${idx}">
          <td style="${activeStyle}">${esc(l.name)} <span class="kbd-hint">#${Number(l.id)}</span></td>
          <td class="mono">${esc(b ? b.friendly : "—")}</td>
          <td class="mono kbd-hint">${esc(b ? (b.rendered || b.behavior_name) : "—")}</td>
        </tr>`;
    }).join("");
    $("insp-all-layers").querySelectorAll("tr.clickable").forEach((tr) => {
      tr.addEventListener("click", () => {
        state.layerIndex = Number(tr.dataset.inspLidx);
        renderLayerTabs();
        renderBoards();
        renderLowerDetails();
        renderInspector();
      });
    });
  }

  // --- Actions --- //

  async function refreshWorkspace() {
    const ws = await apiGet("/api/workspace");
    state.workspaceRoot = ws.root;
    state.files = ws.files;
    renderSidebar();
    return ws;
  }

  async function loadWorkspacePath(relPath, autoPlanRestore = false) {
    try {
      setBusy(true);
      showAlert(null);
      const data = await apiPost("/api/snapshots/load", { path: relPath });
      setSnapshot(data.snapshot);
      if (autoPlanRestore) {
        await planRestore();
      }
    } catch (err) {
      showAlert("err", `<b>Could not load snapshot:</b> ${esc(err.message)}`);
    } finally {
      setBusy(false);
    }
  }

  async function readLiveKeyboard({ manual = true } = {}) {
    try {
      state.readingLive = true;
      setBusy(true);
      if (manual) showAlert(null);
      renderSidebar();
      if (!state.current) renderMain();
      const data = await apiPost("/api/keyboard/read", {});
      setSnapshot(data.snapshot, Boolean(state.current && state.current.source.kind === "live"));
      return true;
    } catch (err) {
      state.liveError = err.message;
      if (manual) {
        showAlert("err", `<b>Could not read keyboard:</b> ${esc(err.message)}`);
      }
      return false;
    } finally {
      state.readingLive = false;
      setBusy(false);
      renderSidebar();
      if (!state.current) renderMain();
    }
  }

  async function takeSnapshotNow() {
    try {
      setBusy(true);
      showAlert(null);
      const data = await apiPost("/api/backup", {});
      state.files = data.files;
      recordLiveReading(data.snapshot);
      setSnapshot(data.snapshot, true);
      showAlert("ok", `Saved snapshot to <code>${esc(data.saved_path)}</code>.`);
    } catch (err) {
      state.liveError = err.message;
      renderSidebar();
      showAlert("err", `<b>Snapshot failed:</b> ${esc(err.message)}`);
    } finally {
      setBusy(false);
    }
  }

  async function planRestore() {
    if (!state.current) return;
    try {
      setBusy(true);
      showAlert(null);
      const data = await apiPost("/api/restore/plan", { raw_json: state.current.raw_json });
      if (data.live_snapshot) {
        recordLiveReading(data.live_snapshot);
      }
      state.diff = {
        kind: "restore",
        proposal: data.proposal,
        planDigest: data.plan_digest,
        diffLayers: data.diff_layers,
        baseline: data.live_snapshot || null,
        boardMode: "diff",
        forceDevice: false,
      };
      // Jump automatically to the first layer that has a difference, if any.
      if (data.proposal.changes.length > 0) {
        const firstChangedLayerId = data.proposal.changes[0].layer_id;
        const idx = state.current.layers.findIndex((l) => l.id === firstChangedLayerId);
        if (idx >= 0) state.layerIndex = idx;
      }
      renderSidebar();
      renderMain();
    } catch (err) {
      showAlert("err", `<b>Restore refused before writing:</b> ${esc(err.message)}`);
    } finally {
      setBusy(false);
    }
  }

  async function commitRestore() {
    if (!state.current || !state.diff || state.diff.kind !== "restore") return;
    const count = state.diff.proposal.changes.length;
    try {
      setBusy(true);
      showAlert(null);
      const data = await apiPost("/api/restore/commit", {
        raw_json: state.current.raw_json,
        plan_digest: state.diff.planDigest,
        force_different_device: state.diff.forceDevice,
      });
      state.files = data.files;
      state.lastUndoPath = data.undo_path || null;
      setSnapshot(data.live_snapshot, true);
      showAlert(
        "ok",
        `<b>Wrote ${count} binding${count === 1 ? "" : "s"} and saved to flash.</b> Undo snapshot written to <code>${esc(data.undo_path)}</code>.`,
        data.undo_path
      );
    } catch (err) {
      if (err.data && err.data.files) {
        state.files = err.data.files;
        renderSidebar();
      }
      showAlert("err", `<b>Restore failed:</b> ${esc(err.message)}`);
    } finally {
      setBusy(false);
    }
  }

  async function runOfflineDiff() {
    if (!state.current) return;
    const choice = $("compare-baseline-select").value;
    if (!choice) return;
    try {
      setBusy(true);
      showAlert(null);
      let beforeJson = "";
      if (choice === "__live__" && state.lastLive) {
        beforeJson = state.lastLive.raw_json;
      } else if (choice === "__imported__" && state.importedSnapshot) {
        beforeJson = state.importedSnapshot.raw_json;
      } else {
        const loaded = await apiPost("/api/snapshots/load", { path: choice });
        beforeJson = loaded.snapshot.raw_json;
      }
      const data = await apiPost("/api/diff/snapshots", {
        before_json: beforeJson,
        after_json: state.current.raw_json,
      });
      state.diff = {
        kind: "offline",
        proposal: data.proposal,
        planDigest: null,
        diffLayers: data.diff_layers,
        baseline: data.baseline_snapshot,
        boardMode: "diff",
        forceDevice: false,
      };
      $("compare-bar").style.display = "none";
      if (data.proposal.changes.length > 0) {
        const firstChangedLayerId = data.proposal.changes[0].layer_id;
        const idx = state.current.layers.findIndex((l) => l.id === firstChangedLayerId);
        if (idx >= 0) state.layerIndex = idx;
      }
      renderMain();
    } catch (err) {
      showAlert("err", `<b>Cannot diff snapshots:</b> ${esc(err.message)}`);
    } finally {
      setBusy(false);
    }
  }

  async function importRawJsonText(rawText, filename) {
    try {
      setBusy(true);
      showAlert(null);
      const data = await apiPost("/api/snapshots/load", {
        raw_json: rawText,
        label: filename || "imported.json",
      });
      state.importedSnapshot = data.snapshot;
      setSnapshot(data.snapshot);
    } catch (err) {
      showAlert("err", `<b>Invalid snapshot file:</b> ${esc(err.message)}`);
    } finally {
      setBusy(false);
    }
  }

  function toggleRawMode(forceVal) {
    state.rawMode = typeof forceVal === "boolean" ? forceVal : !state.rawMode;
    document.body.classList.toggle("mode-raw", state.rawMode);
    $("btn-mode-friendly").classList.toggle("active", !state.rawMode);
    $("btn-mode-raw").classList.toggle("active", state.rawMode);
    if (state.diff) renderDiffBanner();
  }

  function toggleAllLayers(forceVal) {
    state.allLayers = typeof forceVal === "boolean" ? forceVal : !state.allLayers;
    $("btn-layout-single").classList.toggle("active", !state.allLayers);
    $("btn-layout-all").classList.toggle("active", state.allLayers);
    renderLayerTabs();
    renderBoards();
  }

  function downloadBlob(filename, mime, content) {
    const blob = new Blob([content], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  // --- Event wiring --- //

  $("btn-refresh-live").addEventListener("click", (ev) => {
    ev.stopPropagation();
    if (!state.busy) readLiveKeyboard({ manual: true });
  });
  $("live-card").addEventListener("click", () => {
    if (state.busy) return;
    if (state.lastLive) {
      setSnapshot(state.lastLive, true);
    } else {
      readLiveKeyboard({ manual: true });
    }
  });
  $("live-card").addEventListener("keydown", (ev) => {
    if (ev.target !== $("live-card")) return;
    if (ev.key === "Enter" || ev.key === " ") {
      ev.preventDefault();
      $("live-card").click();
    }
  });
  $("btn-backup").addEventListener("click", takeSnapshotNow);
  $("btn-toggle-more").addEventListener("click", () => {
    state.showAllSnapshots = !state.showAllSnapshots;
    renderSidebar();
  });
  $("btn-plan-restore").addEventListener("click", planRestore);

  $("btn-compare-toggle").addEventListener("click", () => {
    const bar = $("compare-bar");
    bar.style.display = bar.style.display === "none" ? "flex" : "none";
    populateCompareDropdown();
    fitStageBoard();
  });
  $("btn-close-compare").addEventListener("click", () => {
    $("compare-bar").style.display = "none";
    fitStageBoard();
  });
  $("btn-run-offline-diff").addEventListener("click", runOfflineDiff);

  $("btn-mode-friendly").addEventListener("click", () => toggleRawMode(false));
  $("btn-mode-raw").addEventListener("click", () => toggleRawMode(true));
  $("btn-layout-single").addEventListener("click", () => toggleAllLayers(false));
  $("btn-layout-all").addEventListener("click", () => toggleAllLayers(true));

  $("btn-import-json").addEventListener("click", () => $("file-input").click());
  $("file-input").addEventListener("change", async (ev) => {
    const file = ev.target.files && ev.target.files[0];
    if (!file) return;
    const text = await file.text();
    ev.target.value = "";
    await importRawJsonText(text, file.name);
  });

  $("btn-export-html").addEventListener("click", async () => {
    if (!state.current || state.current.source.kind === "live") return;
    try {
      const data = await apiPost("/api/render/html", { raw_json: state.current.raw_json });
      const label = (state.current.source.label || "").replace(/\.json$/i, "");
      const safeStem = (label || state.current.device.name || "keymap")
        .toLowerCase()
        .replace(/[^a-z0-9._-]+/g, "-")
        .replace(/^-+|-+$/g, "");
      downloadBlob(`${safeStem || "keymap"}.html`, "text/html;charset=utf-8", data.html);
    } catch (err) {
      showAlert("err", `<b>Export failed:</b> ${esc(err.message)}`);
    }
  });

  $("insp-close").addEventListener("click", closeInspector);
  $("insp-prev").addEventListener("click", () => {
    if (!state.current || state.selectedPos == null) return;
    const n = state.current.layout.keys.length;
    state.selectedPos = (state.selectedPos - 1 + n) % n;
    applySelectedKeyHighlight();
    renderInspector();
  });
  $("insp-next").addEventListener("click", () => {
    if (!state.current || state.selectedPos == null) return;
    const n = state.current.layout.keys.length;
    state.selectedPos = (state.selectedPos + 1) % n;
    applySelectedKeyHighlight();
    renderInspector();
  });
  $("insp-copy").addEventListener("click", async () => {
    const txt = $("insp-rendered").textContent || "";
    if (navigator.clipboard && txt) {
      await navigator.clipboard.writeText(txt);
      $("insp-copy").textContent = "Copied";
      setTimeout(() => { $("insp-copy").textContent = "Copy"; }, 1000);
    }
  });

  document.querySelectorAll("[data-theme-btn]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const mode = btn.dataset.themeBtn;
      if (mode === "auto") {
        document.documentElement.removeAttribute("data-theme");
      } else {
        document.documentElement.setAttribute("data-theme", mode);
      }
      document.querySelectorAll("[data-theme-btn]").forEach((b) => {
        b.classList.toggle("active", b === btn);
      });
    });
  });

  // Drag-and-drop JSON anywhere
  window.addEventListener("dragover", (ev) => {
    ev.preventDefault();
    document.body.classList.add("dragging");
  });
  window.addEventListener("dragleave", (ev) => {
    if (ev.target === document.documentElement || ev.target === document.body) {
      document.body.classList.remove("dragging");
    }
  });
  window.addEventListener("drop", async (ev) => {
    ev.preventDefault();
    document.body.classList.remove("dragging");
    const file = ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files[0];
    if (file) {
      const text = await file.text();
      await importRawJsonText(text, file.name);
    }
  });

  // Keyboard shortcuts
  window.addEventListener("keydown", (ev) => {
    const tag = (ev.target && ev.target.tagName) || "";
    if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA" || ev.metaKey || ev.ctrlKey || ev.altKey) {
      return;
    }
    if (ev.key === "Escape") {
      if (state.selectedPos != null) closeInspector();
      else if (state.diff) { state.diff = null; renderMain(); }
      return;
    }
    if (!state.current) return;
    if (ev.key === "r" || ev.key === "R") {
      toggleRawMode();
    } else if (ev.key === "a" || ev.key === "A") {
      toggleAllLayers();
    } else if (ev.key >= "1" && ev.key <= "9") {
      const idx = Number(ev.key) - 1;
      if (idx < state.current.layers.length) {
        state.layerIndex = idx;
        renderMain();
      }
    } else if (ev.key === "ArrowLeft" && state.selectedPos != null) {
      ev.preventDefault();
      $("insp-prev").click();
    } else if (ev.key === "ArrowRight" && state.selectedPos != null) {
      ev.preventDefault();
      $("insp-next").click();
    }
  });

  window.addEventListener("resize", () => {
    requestAnimationFrame(fitStageBoard);
  });

  // Keep "Live view · taken X mins ago" fresh as time passes
  setInterval(() => {
    if (state.lastLive && state.lastLiveReadAt && !state.readingLive) {
      renderLiveCard();
    }
  }, 15000);

  // Boot: list saved backups, try auto-reading the live keyboard, and fall back calmly to latest backup
  (async () => {
    try {
      const ws = await refreshWorkspace();
      const liveOk = await readLiveKeyboard({ manual: false });
      if (!liveOk) {
        const latest = ws.files.snapshots && ws.files.snapshots.find((f) => !f.error);
        if (latest) {
          await loadWorkspacePath(latest.rel_path, false);
        } else {
          renderMain();
        }
      }
    } catch (err) {
      showAlert("err", `<b>Workspace initialization failed:</b> ${esc(err.message)}`);
    }
  })();
})();
