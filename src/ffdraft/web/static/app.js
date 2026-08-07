"use strict";

const api = async (path, options) => {
  const res = await fetch(path, options);
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(body.detail || res.statusText);
  }
  return res.json();
};
const post = (path, body) =>
  api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });

const $ = (sel) => document.querySelector(sel);
const num = (v, digits = 0) =>
  v === null || v === undefined ? "" : Number(v).toFixed(digits);
const pct = (v) => (v === null || v === undefined ? "" : (v * 100).toFixed(0) + "%");

let state = { rows: [], draft: {}, meta: {}, nextPick: null };

// ---------------------------------------------------------------- tabs

document.querySelectorAll(".tab").forEach((tab) => {
  tab.onclick = () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    $("#panel-" + tab.dataset.tab).classList.add("active");
    if (tab.dataset.tab === "myteam") renderTeam();
  };
});

// ---------------------------------------------------------------- board

async function loadBoard() {
  const params = new URLSearchParams({
    top: "300",
    position: $("#position").value,
    include_taken: (!$("#hide-taken").checked).toString(),
  });
  if ($("#search").value) params.set("search", $("#search").value);

  const data = await api("/api/board?" + params);
  state = { rows: data.rows, draft: data.draft, meta: data.meta, nextPick: data.next_pick };
  renderAssumptions();
  renderScarcity();
  renderRows();
  renderStatus();
}

function renderAssumptions() {
  const m = state.meta;
  // ADP is hand-maintained, so its age is a first-class concern, not a footnote.
  const adpAge = m.adp_as_of ? daysSince(m.adp_as_of) : null;
  const stale = adpAge !== null && adpAge > 10;
  $("#assumptions").innerHTML =
    `<b>${m.teams}-team</b> ${m.scoring} · ${m.roster_spots} spots · ` +
    `no ${(m.excluded_positions || []).join("/") || "—"} · ` +
    `ECR <b>${m.market_as_of || "?"}</b> · ` +
    `ADP <b class="${stale ? "stale" : ""}">${m.adp_as_of || "?"}` +
    `${adpAge !== null ? ` (${adpAge}d${stale ? " — stale" : ""})` : ""}</b> · ` +
    `usage ${m.usage_season} · ordered by ECR, not projections`;
}

function daysSince(iso) {
  const then = new Date(iso + "T00:00:00Z");
  return Math.floor((Date.now() - then.getTime()) / 86400000);
}

function renderScarcity() {
  const taken = new Set(state.draft.taken || []);
  const counts = {};
  // Counted over the top 150 by ECR: how many startable players are left at each
  // position. Counting the whole 478-deep list would always read "plenty".
  state.rows
    .filter((r) => r.ecr_rank_adj <= 150 && !taken.has(r.key))
    .forEach((r) => (counts[r.position] = (counts[r.position] || 0) + 1));
  $("#scarcity").innerHTML =
    `<div>Top-150 left</div>` +
    ["QB", "RB", "WR", "TE", "DST"]
      .map((p) => `<div>${p} <b>${counts[p] || 0}</b></div>`)
      .join("");
}

function renderRows() {
  const taken = new Set(state.draft.taken || []);
  const mine = new Set((state.draft.picks || []).filter((p) => p.mine).map((p) => p.key));
  const tbody = $("#board tbody");
  tbody.innerHTML = "";

  for (const row of state.rows) {
    const tr = document.createElement("tr");
    tr.className =
      (taken.has(row.key) ? "taken " : "") + (mine.has(row.key) ? "mine" : "");
    const delta = row.ecr_vs_adp;
    const survClass = row.survives ? "surv-" + row.survives.replace("-", "") : "";

    tr.innerHTML =
      `<td class="muted">${row.ecr_rank_adj ?? ""}</td>` +
      `<td class="name">${escape(row.player)}</td>` +
      `<td><span class="pos pos-${row.position}">${row.position}</span></td>` +
      `<td class="muted">${row.team ?? ""}</td>` +
      `<td class="muted">${row.bye ?? ""}</td>` +
      `<td>${row.tier ?? ""}</td>` +
      `<td>${num(row.ecr, 1)}</td>` +
      `<td>${num(row.adp)}</td>` +
      `<td class="${delta > 0 ? "up" : delta < 0 ? "down" : "muted"}">` +
      `${delta === null || delta === undefined ? "" : (delta > 0 ? "+" : "") + delta}</td>` +
      `<td class="${survClass}">${row.survives ?? ""}</td>` +
      `<td class="muted">${pct(row.target_share)}</td>` +
      `<td class="${row.poe_per_game > 0 ? "up" : row.poe_per_game < 0 ? "down" : "muted"}">` +
      `${num(row.poe_per_game, 1)}</td>` +
      `<td class="muted">${row.role_status ?? ""}${row.rookie ? " · rookie" : ""}</td>`;

    tr.onclick = (event) => togglePick(row, event.shiftKey);
    tbody.appendChild(tr);
  }
}

function escape(s) {
  const d = document.createElement("div");
  d.textContent = s ?? "";
  return d.innerHTML;
}

async function togglePick(row, mine) {
  const taken = new Set(state.draft.taken || []);
  if (taken.has(row.key)) {
    await api("/api/draft/pick/" + encodeURIComponent(row.key), { method: "DELETE" });
  } else {
    await post("/api/draft/pick", {
      key: row.key,
      player: row.player,
      position: row.position,
      team: row.team,
      mine: !!mine,
    });
  }
  await loadBoard();
}

function renderStatus() {
  const d = state.draft;
  $("#draft-status").innerHTML =
    `Rd <b>${d.round ?? 1}</b> · pick <b>${d.next_overall ?? 1}</b>` +
    (state.nextPick ? ` · yours at <b>${state.nextPick}</b>` : "");
  $("#my-count").textContent = (d.picks || []).filter((p) => p.mine).length;
}

// ---------------------------------------------------------------- my team

function renderTeam() {
  const d = state.draft;
  const picks = d.picks || [];
  const mine = picks.filter((p) => p.mine);
  const teams = d.teams || 10;

  const counts = {};
  mine.forEach((p) => (counts[p.position] = (counts[p.position] || 0) + 1));
  // Starting requirements for this league, so the summary shows what is still
  // unfilled rather than just what has been taken.
  const need = { QB: 1, RB: 2, WR: 2, TE: 1, DST: 1 };
  $("#roster-summary").innerHTML =
    Object.entries(need)
      .map(([pos, n]) => {
        const have = counts[pos] || 0;
        return `<div class="${have < n ? "short" : ""}"><b>${have}/${n}</b>${pos}</div>`;
      })
      .join("") + `<div><b>${mine.length}</b>total</div>`;

  $("#my-picks tbody").innerHTML = mine
    .map(
      (p) =>
        `<tr><td>${Math.floor((p.overall - 1) / teams) + 1}</td><td>${p.overall}</td>` +
        `<td class="name">${escape(p.player)}</td>` +
        `<td><span class="pos pos-${p.position}">${p.position}</span></td>` +
        `<td class="muted">${p.team ?? ""}</td>` +
        `<td><button class="ghost" onclick="unmine('${p.key}')">not mine</button></td></tr>`
    )
    .join("");

  $("#all-picks tbody").innerHTML = picks
    .slice()
    .reverse()
    .map(
      (p) =>
        `<tr><td>${p.overall}</td><td>${Math.floor((p.overall - 1) / teams) + 1}</td>` +
        `<td class="name">${escape(p.player)}</td>` +
        `<td><span class="pos pos-${p.position}">${p.position}</span></td>` +
        `<td>${p.mine ? "✓" : ""}</td>` +
        `<td><button class="ghost" onclick="release('${p.key}')">undo</button></td></tr>`
    )
    .join("");
}

window.unmine = async (key) => {
  // Flips ownership only — the player stays drafted, just not by you.
  await post("/api/draft/mine", { key, mine: false });
  await loadBoard();
  renderTeam();
};
window.release = async (key) => {
  await fetch("/api/draft/pick/" + encodeURIComponent(key), { method: "DELETE" });
  await loadBoard();
  renderTeam();
};

// ---------------------------------------------------------------- compare

$("#cmp-go").onclick = async () => {
  const a = $("#cmp-a").value.trim();
  const b = $("#cmp-b").value.trim();
  if (!a || !b) return;
  try {
    const data = await api(`/api/compare?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`);
    $("#cmp-out").innerHTML =
      `<table class="cmp-table"><thead><tr><th></th>` +
      `<th class="cmp-head">${escape(data.a.player)}</th>` +
      `<th class="cmp-head">${escape(data.b.player)}</th></tr></thead><tbody>` +
      data.fields
        .map(
          (f) =>
            `<tr><td class="muted">${f.label}</td>` +
            `<td class="${f.winner === "a" ? "win" : ""}">${fmt(f.key, f.a)}</td>` +
            `<td class="${f.winner === "b" ? "win" : ""}">${fmt(f.key, f.b)}</td></tr>`
        )
        .join("") +
      `</tbody></table>`;
  } catch (err) {
    $("#cmp-out").innerHTML = `<p class="down">${escape(err.message)}</p>`;
  }
};

function fmt(key, value) {
  if (value === null || value === undefined) return "—";
  if (["target_share", "snap_pct", "team_target_share"].includes(key)) return pct(value);
  if (typeof value === "number") return Number.isInteger(value) ? value : value.toFixed(2);
  return escape(String(value));
}

$("#cmp-agent").onclick = () => {
  const a = $("#cmp-a").value.trim();
  const b = $("#cmp-b").value.trim();
  if (!a || !b) return;
  document.querySelector('.tab[data-tab="console"]').click();
  startRun("compare", `${a} ${b}`);
};

// ---------------------------------------------------------------- console

let pollTimer = null;
let currentJob = null;

document.querySelectorAll("button.run").forEach((btn) => {
  btn.onclick = () => startRun(btn.dataset.run, $("#run-args").value.trim());
});

async function startRun(command, args) {
  const out = $("#console-out");
  try {
    const job = await post("/api/run", { command, args });
    attach(job);
    if (job.attached) {
      setStatus(`already running — attached, ${job.elapsed_seconds}s elapsed`, "running");
    }
  } catch (err) {
    out.textContent = "error: " + err.message;
    setStatus("failed", "failed");
  }
}

function attach(job) {
  currentJob = { id: job.id, offset: 0, command: job.command };
  $("#console-out").textContent = "";
  $("#cancel").hidden = false;
  appendLines(job.lines);
  currentJob.offset = job.next_offset;
  clearTimeout(pollTimer);
  poll();
}

async function poll() {
  if (!currentJob) return;
  let view;
  try {
    view = await api(`/api/run/${currentJob.id}?since=${currentJob.offset}`);
  } catch (err) {
    // A dropped poll must not silently orphan a run that is still going.
    setStatus("lost contact with the job: " + err.message, "failed");
    return;
  }
  currentJob.offset = view.next_offset;
  appendLines(view.lines);

  if (view.status === "running") {
    // An agent run can sit silent for a minute between tool calls, so the timer
    // is what tells you it is alive rather than hung.
    setStatus(`running · ${fmtElapsed(view.elapsed_seconds)}`, "running");
    pollTimer = setTimeout(poll, 1000);
  } else {
    $("#cancel").hidden = true;
    setStatus(`${view.status} · ${fmtElapsed(view.elapsed_seconds)}`, view.status);
    // A finished refresh changed the warehouse; the board must not keep showing
    // pre-ingest numbers.
    if (view.command === "refresh" && view.status === "done") {
      await post("/api/board/reload");
      await loadBoard();
    }
    currentJob = null;
  }
}

function fmtElapsed(seconds) {
  if (seconds === null || seconds === undefined) return "";
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`;
}

function setStatus(text, kind) {
  const el = $("#run-status");
  el.textContent = text;
  el.className = "pill status-" + (kind || "");
}

// On load, re-attach to anything already running. Without this a page reload
// orphans a live agent run and the only sign of it is a confusing 409.
async function reattach() {
  for (const command of ["board", "compare", "refresh"]) {
    try {
      const job = await api(`/api/run?command=${command}`);
      attach(job);
      setStatus(`/${command} already running · ${fmtElapsed(job.elapsed_seconds)}`, "running");
      return true;
    } catch {
      /* nothing running for this command */
    }
  }
  return false;
}

function appendLines(lines) {
  if (!lines || !lines.length) return;
  const out = $("#console-out");
  out.textContent += lines.join("\n") + "\n";
  out.scrollTop = out.scrollHeight;
}

$("#cancel").onclick = async () => {
  if (currentJob) await post(`/api/run/${currentJob.id}/cancel`);
};

// ---------------------------------------------------------------- controls

$("#position").onchange = loadBoard;
$("#hide-taken").onchange = loadBoard;

let searchTimer;
$("#search").oninput = () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadBoard, 180);
};

$("#slot").onchange = async () => {
  const slot = $("#slot").value ? parseInt($("#slot").value, 10) : null;
  await post("/api/draft/configure", { slot });
  await loadBoard();
};

$("#undo").onclick = async () => {
  await post("/api/draft/undo");
  await loadBoard();
};

$("#reset").onclick = async () => {
  if (!confirm("Clear every pick in this draft?")) return;
  await post("/api/draft/reset");
  await loadBoard();
};

// ---------------------------------------------------------------- boot

(async () => {
  const draft = await api("/api/draft");
  const teams = draft.teams || 10;
  const slotSel = $("#slot");
  for (let i = 1; i <= teams; i++) {
    const opt = document.createElement("option");
    opt.value = String(i);
    opt.textContent = "Slot " + i;
    slotSel.appendChild(opt);
  }
  if (draft.slot) slotSel.value = String(draft.slot);
  await loadBoard();

  if (await reattach()) {
    document.querySelector('.tab[data-tab="console"]').click();
  }
})();
