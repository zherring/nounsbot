// The record: rendered client-side from verdicts.json, which the bot
// commits to this repo after every evaluation and cast.

const STATUS_LABEL = {
  paper: "📝 paper",
  scheduled: "🕒 scheduled",
  held: "✋ held",
  cast: "🗳 cast",
  missed: "⏹ missed",
  skipped: "📝 paper",
};

let revMap = {};

async function loadRecord() {
  const tbody = document.getElementById("record-body");
  if (!tbody) return;
  let data;
  try {
    const resp = await fetch("verdicts.json", { cache: "no-store" });
    if (!resp.ok) return;
    data = await resp.json();
    const am = await fetch("amendments.json", { cache: "no-store" });
    if (am.ok) revMap = (await am.json()).revs || {};
  } catch {
    return; // keep the placeholder row
  }
  const verdicts = data.verdicts || [];
  if (!verdicts.length) return;

  tbody.innerHTML = "";
  for (const v of verdicts) {
    const tr = document.createElement("tr");
    const pill = pillClass(v.vote);
    const status = STATUS_LABEL[v.status] || v.status;
    const tx = v.tx_hash
      ? ` · <a href="https://etherscan.io/tx/0x${v.tx_hash.replace(/^0x/, "")}">tx</a>`
      : "";
    const flags = v.flags?.length ? ` ⚑ ${v.flags.join(", ")}` : "";
    const override = v.overridden ? " · human override" : "";
    const ver = revMap[v.constitution_rev] || v.constitution_rev || "";
    const historyCount = diffHistory(v).length;
    const detailsLabel = historyCount
      ? `Full rationale + ${historyCount} earlier verdict${historyCount > 1 ? "s" : ""} →`
      : "Full rationale →";
    tr.innerHTML = `
      <td><a href="https://www.nouns.camp/proposals/${v.prop_id}">${v.prop_id}</a><br>
          <span class="muted" style="font-size:0.85rem">${esc(v.title || "")}</span></td>
      <td><span class="pill ${pill}">${v.vote}</span><br>
          <span class="muted" style="font-size:0.8rem">${status} · ${ver}${tx}${override}</span></td>
      <td>${(v.clauses || []).join(", ")}</td>
      <td class="reason-cell" style="max-width:26rem">
        <span class="reason-text">${esc(v.reason || "")}<span class="muted">${esc(flags)}</span></span>
        <button type="button" class="reason-toggle">${detailsLabel}</button>
      </td>
      <td>${esc(v.outcome || "")}</td>`;
    tr.querySelector(".reason-toggle").addEventListener("click", () => openVerdictModal(v));
    tbody.appendChild(tr);
  }
  renderCandidates(data.candidates || []);

  const note = document.getElementById("record-note");
  if (note) {
    note.textContent =
      `Every verdict publishes here — vote, clauses cited, overrides with reasons. ` +
      `Append-only. Updated ${new Date(data.generated_at).toLocaleString()}.`;
  }
}

function txLink(hash) {
  return hash
    ? ` · <a href="https://etherscan.io/tx/0x${hash.replace(/^0x/, "")}">tx</a>`
    : "";
}

// What the agent actually DID about a candidate — sponsoring and signaling
// are onchain acts, everything else is a verdict awaiting a human.
function candAction(c) {
  if (c.sponsor_state === "sponsored") return `🌱 sponsored${txLink(c.sponsor_tx)}`;
  if (c.sponsor_state === "stale") {
    return c.revoke_available
      ? "⚠️ updated after sponsorship · revocation available"
      : "⚠️ updated after sponsorship";
  }
  if (c.sponsor_state === "revoked") return `🛑 sponsorship revoked${txLink(c.revoke_tx)}`;
  if (c.sponsor_state === "expired") return "⌛ sponsorship expired";
  if (c.signal_tx) return `📣 signaled ${c.signal_stance || ""}${txLink(c.signal_tx)}`;
  if (c.vote === "FOR") return "🌱 sponsor-worthy — awaiting human sign-off";
  return "👀 watching";
}

function renderCandidates(cands) {
  const tab = document.getElementById("cand-tab");
  if (tab && cands.length) tab.textContent = `Candidates (${cands.length})`;
  const tbody = document.getElementById("cand-body");
  if (!tbody || !cands.length) return;
  tbody.innerHTML = "";
  for (const c of cands) {
    const tr = document.createElement("tr");
    const flags = c.flags?.length ? ` ⚑ ${c.flags.join(", ")}` : "";
    const update = c.change_summary
      ? `Changed (${c.change_materiality || "material"}): ${c.change_summary}\n\n`
      : "";
    const summary = c.tldr ? `TL;DR: ${c.tldr}\n\n${update}` : update;
    tr.innerHTML = `
      <td><a href="https://www.nouns.camp/candidates/${encodeURIComponent(c.cand_id)}">c${c.num}</a><br>
          <span class="muted" style="font-size:0.85rem">${esc(c.title || "")}</span></td>
      <td><span class="pill ${pillClass(c.vote)}">${esc(c.vote || "")}</span><br>
          <span class="muted" style="font-size:0.8rem">${c.vote === "FOR" ? "sponsor-worthy" : "not sponsor-worthy"}</span></td>
      <td>${(c.clauses || []).join(", ")}</td>
      <td class="reason-cell" style="max-width:26rem">
        <span class="reason-text">${esc(summary + (c.reason || ""))}<span class="muted">${esc(flags)}</span></span>
        <button type="button" class="reason-toggle">Full rationale →</button>
      </td>
      <td>${candAction(c)}</td>`;
    tr.querySelector(".reason-toggle").addEventListener("click", () => openCandidateModal(c));
    tbody.appendChild(tr);
  }
}

function initRecordTabs() {
  const tabs = document.querySelectorAll(".record-tab");
  if (!tabs.length) return;
  tabs.forEach((btn) =>
    btn.addEventListener("click", () => {
      tabs.forEach((b) => b.classList.toggle("active", b === btn));
      const props = document.getElementById("tab-props");
      const cands = document.getElementById("tab-cands");
      if (props) props.hidden = btn.dataset.tab !== "props";
      if (cands) cands.hidden = btn.dataset.tab !== "cands";
    })
  );
}

function pillClass(vote) {
  return vote === "FOR" ? "pill-for" : vote === "AGAINST" ? "pill-against" : "pill-flag";
}

// Verdicts get re-run on every constitution amendment, but most re-runs land
// on the same vote. We only want the points where the vote actually flipped —
// that's the trail of "this amendment changed the outcome" — not a log of
// every unchanged re-evaluation. Returns chronological (oldest-first) entries
// from v.history, excluding v itself, empty if the vote never changed.
function diffHistory(v) {
  const history = v.history || [];
  if (!history.length) return [];
  const chain = history.concat([v]);
  const neverChanged = chain.every((h) => h.vote === chain[0].vote);
  if (neverChanged) return [];
  const changePoints = [];
  let prevVote;
  chain.forEach((h, i) => {
    if (i === 0 || h.vote !== prevVote) changePoints.push(h);
    prevVote = h.vote;
  });
  return changePoints.filter((h) => h !== v);
}

function ensureModal() {
  let overlay = document.getElementById("verdict-modal");
  if (overlay) return overlay;
  overlay = document.createElement("div");
  overlay.id = "verdict-modal";
  overlay.className = "modal-overlay";
  overlay.hidden = true;
  overlay.innerHTML = `
    <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="verdict-modal-title">
      <button type="button" class="modal-close" aria-label="Close">&times;</button>
      <h3 id="verdict-modal-title"></h3>
      <p class="muted modal-meta"></p>
      <div class="modal-section">
        <h4>Rationale</h4>
        <p class="modal-reason"></p>
      </div>
      <div class="modal-section modal-history" hidden>
        <h4>How this vote changed across amendments</h4>
        <div class="modal-history-list"></div>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  overlay.querySelector(".modal-close").addEventListener("click", closeVerdictModal);
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) closeVerdictModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !overlay.hidden) closeVerdictModal();
  });
  return overlay;
}

function openVerdictModal(v) {
  const overlay = ensureModal();
  const status = STATUS_LABEL[v.status] || v.status;
  const ver = revMap[v.constitution_rev] || v.constitution_rev || "";
  const tx = v.tx_hash
    ? ` · <a href="https://etherscan.io/tx/0x${v.tx_hash.replace(/^0x/, "")}" target="_blank" rel="noopener">tx ↗</a>`
    : "";
  const override = v.overridden ? " · human override" : "";
  const flags = v.flags?.length ? ` · ⚑ ${esc(v.flags.join(", "))}` : "";
  const clauses = (v.clauses || []).join(", ") || "—";
  const confidence = v.confidence != null ? ` · confidence ${v.confidence.toFixed(2)}` : "";

  overlay.querySelector("#verdict-modal-title").textContent = `Prop ${v.prop_id} — ${v.title || ""}`;
  overlay.querySelector(".modal-meta").innerHTML =
    `<a href="https://www.nouns.camp/proposals/${v.prop_id}" target="_blank" rel="noopener">View on nouns.camp ↗</a> · ` +
    `<span class="pill ${pillClass(v.vote)}">${esc(v.vote || "")}</span> · clauses ${esc(clauses)} · ` +
    `${status} · rev ${esc(ver)}${confidence}${tx}${override} · ${esc(v.outcome || "")}${flags}`;
  overlay.querySelector(".modal-reason").textContent = v.reason || "";

  const historySection = overlay.querySelector(".modal-history");
  const historyList = overlay.querySelector(".modal-history-list");
  const history = diffHistory(v);
  if (history.length) {
    historyList.innerHTML = history
      .slice()
      .reverse()
      .map((h) => {
        const hVer = revMap[h.constitution_rev] || h.constitution_rev || "";
        const hConf = h.confidence != null ? ` (${h.confidence.toFixed(2)})` : "";
        return `<div class="history-entry">
          <p class="history-meta"><b>${esc(hVer)}</b>${hConf} · ${esc((h.clauses || []).join(", "))} ·
            <span class="pill ${pillClass(h.vote)}">${esc(h.vote || "")}</span></p>
          <p class="muted">${esc(h.reason || "")}</p>
        </div>`;
      })
      .join("");
    historySection.hidden = false;
  } else {
    historyList.innerHTML = "";
    historySection.hidden = true;
  }

  overlay.hidden = false;
  document.body.style.overflow = "hidden";
  overlay.querySelector(".modal-close").focus();
}

function openCandidateModal(c) {
  const overlay = ensureModal();
  const clauses = (c.clauses || []).join(", ") || "—";
  const confidence = c.confidence != null ? ` · confidence ${c.confidence.toFixed(2)}` : "";
  const flags = c.flags?.length ? ` · ⚑ ${esc(c.flags.join(", "))}` : "";

  overlay.querySelector("#verdict-modal-title").textContent = `Candidate c${c.num} — ${c.title || ""}`;
  overlay.querySelector(".modal-meta").innerHTML =
    `<a href="https://www.nouns.camp/candidates/${encodeURIComponent(c.cand_id)}" target="_blank" rel="noopener">View on nouns.camp ↗</a> · ` +
    `<span class="pill ${pillClass(c.vote)}">${esc(c.vote || "")}</span> · clauses ${esc(clauses)}` +
    `${confidence} · ${candAction(c)}${flags}`;
  overlay.querySelector(".modal-reason").textContent = c.reason || "";

  overlay.querySelector(".modal-history-list").innerHTML = "";
  overlay.querySelector(".modal-history").hidden = true;

  overlay.hidden = false;
  document.body.style.overflow = "hidden";
  overlay.querySelector(".modal-close").focus();
}

function closeVerdictModal() {
  const overlay = document.getElementById("verdict-modal");
  if (!overlay) return;
  overlay.hidden = true;
  document.body.style.overflow = "";
}

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

initRecordTabs();
loadRecord();
