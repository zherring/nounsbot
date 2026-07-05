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

async function loadRecord() {
  const tbody = document.getElementById("record-body");
  if (!tbody) return;
  let data;
  let revMap = {};
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
    const pill = v.vote === "FOR" ? "pill-for" : v.vote === "AGAINST" ? "pill-against" : "pill-flag";
    const status = STATUS_LABEL[v.status] || v.status;
    const tx = v.tx_hash
      ? ` · <a href="https://etherscan.io/tx/0x${v.tx_hash.replace(/^0x/, "")}">tx</a>`
      : "";
    const flags = v.flags?.length ? ` ⚑ ${v.flags.join(", ")}` : "";
    const override = v.overridden ? " · human override" : "";
    const ver = revMap[v.constitution_rev] || v.constitution_rev || "";
    const historyHtml = (v.history || []).length
      ? `<details class="verdict-history">
           <summary>↺ ${v.history.length} earlier verdict${v.history.length > 1 ? "s" : ""} under prior constitutions</summary>
           ${v.history
             .slice()
             .reverse()
             .map(
               (h) =>
                 `<div class="history-entry"><b>${revMap[h.constitution_rev] || h.constitution_rev}</b>:
                  ${h.vote} (${(h.confidence ?? 0).toFixed(2)}) · ${(h.clauses || []).join(", ")}<br>
                  <span class="muted">${esc(h.reason || "")}</span></div>`
             )
             .join("")}
         </details>`
      : "";
    tr.innerHTML = `
      <td><a href="https://nouns.wtf/vote/${v.prop_id}">${v.prop_id}</a><br>
          <span class="muted" style="font-size:0.85rem">${esc(v.title || "")}</span></td>
      <td><span class="pill ${pill}">${v.vote}</span><br>
          <span class="muted" style="font-size:0.8rem">${status} · ${ver}${tx}${override}</span></td>
      <td>${(v.clauses || []).join(", ")}</td>
      <td class="reason-cell" style="max-width:26rem">
        <span class="reason-text">${esc(v.reason || "")}<span class="muted">${esc(flags)}</span></span>
        <button type="button" class="reason-toggle" aria-expanded="false">Show more</button>
        ${historyHtml}
      </td>
      <td>${esc(v.outcome || "")}</td>`;
    tbody.appendChild(tr);
  }
  wireReasonToggles(tbody);
  const note = document.getElementById("record-note");
  if (note) {
    note.textContent =
      `Every verdict publishes here — vote, clauses cited, overrides with reasons. ` +
      `Append-only. Updated ${new Date(data.generated_at).toLocaleString()}.`;
  }
}

function wireReasonToggles(tbody) {
  tbody.querySelectorAll(".reason-cell").forEach((cell) => {
    const text = cell.querySelector(".reason-text");
    const toggle = cell.querySelector(".reason-toggle");
    if (!text || !toggle) return;
    if (text.scrollHeight <= text.clientHeight + 1) {
      toggle.remove();
      return;
    }
    toggle.addEventListener("click", () => {
      const expanded = cell.classList.toggle("expanded");
      toggle.textContent = expanded ? "Show less" : "Show more";
      toggle.setAttribute("aria-expanded", String(expanded));
    });
  });
}

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

loadRecord();
