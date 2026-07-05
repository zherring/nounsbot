// Dynamic hero: the Nouns delegated to the agent, as a playful stack.
// One Noun renders big; more pile up behind it, front 2-3 clearly visible.
// Hover a card to see the Noun + its owner/delegator.
// Set DELEGATE_ADDRESS when the bot EOA exists (M1). Preview: ?delegate=0x...

const DELEGATE_ADDRESS = "0xf6e7501dfe7003299108020c5830c4c5b3ca6aa9"; // vote-only bot EOA
const SEED_NOUNS = [1251]; // shown until live delegation data replaces them

const SUBGRAPH = "https://www.nouns.camp/subgraphs/nouns";
const WORDS = [
  "Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight",
  "Nine", "Ten", "Eleven", "Twelve",
];

function short(addr) {
  return addr ? `${addr.slice(0, 6)}…${addr.slice(-4)}` : "";
}

// Deterministic jitter from the noun id, so the pile doesn't reshuffle on reload.
function jitter(id, salt, range) {
  let h = 2166136261 ^ salt;
  const s = String(id);
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619);
  return ((h >>> 8) % (range * 2 + 1)) - range;
}

const MAX_SIDE = 4; // side-column cards before the +N overflow kicks in

function makeCard(noun, cls, rot) {
  const a = document.createElement("a");
  a.className = `noun-card ${cls}`;
  a.href = `https://nouns.wtf/noun/${noun.id}`;
  a.dataset.label = noun.owner
    ? `Noun ${noun.id} — delegated by ${short(noun.owner)}`
    : `Noun ${noun.id}`;
  a.style.setProperty("--rot", `${rot}deg`);
  const img = document.createElement("img");
  img.src = `https://noun.pics/${noun.id}`;
  img.alt = a.dataset.label;
  img.loading = "lazy";
  a.appendChild(img);
  return a;
}

function renderStack(nouns) {
  // nouns: [{id, owner}] — featured + side picks are randomized per load
  const stack = document.getElementById("noun-gallery");
  if (!stack || nouns.length === 0) return;
  stack.innerHTML = "";
  stack.classList.add("filled");

  const shuffled = [...nouns].sort(() => Math.random() - 0.5);
  const featured = shuffled[0];
  const sides = shuffled.slice(1, 1 + MAX_SIDE);
  const overflow = shuffled.length - 1 - sides.length;

  stack.appendChild(makeCard(featured, "featured", -1.5));

  if (sides.length) {
    const col = document.createElement("div");
    col.className = "noun-side";
    sides.forEach((noun, i) => {
      const rot = (i % 2 ? 1 : -1) * (7 + Math.abs(jitter(noun.id, i, 5)));
      col.appendChild(makeCard(noun, "side", rot));
    });
    if (overflow > 0) {
      const more = document.createElement("div");
      more.className = "noun-more";
      more.textContent = `+${overflow}`;
      more.title = `${overflow} more Nouns delegated`;
      col.appendChild(more);
    }
    stack.appendChild(col);
  }
}

async function gql(query) {
  const resp = await fetch(SUBGRAPH, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ query }),
  });
  return (await resp.json()).data;
}

async function loadSeed() {
  const ids = SEED_NOUNS.map((id) => `"${id}"`).join(",");
  try {
    const data = await gql(`{ nouns(where: {id_in: [${ids}]}) { id owner { id } } }`);
    renderStack((data?.nouns || []).map((x) => ({ id: x.id, owner: x.owner?.id })));
  } catch {
    renderStack(SEED_NOUNS.map((id) => ({ id })));
  }
}

async function loadDelegation() {
  await loadSeed();

  const param = new URLSearchParams(location.search).get("delegate");
  const address = (param || DELEGATE_ADDRESS).toLowerCase();
  if (!address) return;

  let delegate;
  try {
    const data = await gql(`{
      delegate(id: "${address}") {
        delegatedVotes
        nounsRepresented(first: 100, orderBy: id) { id owner { id } }
      }
    }`);
    delegate = data?.delegate;
  } catch {
    return; // network failure: seed stays
  }
  if (!delegate) return;

  const nouns = delegate.nounsRepresented || [];
  const count = Math.max(1, parseInt(delegate.delegatedVotes, 10) || 0);

  const countEl = document.getElementById("noun-count");
  const nounWordEl = document.getElementById("noun-word");
  const verbEl = document.getElementById("noun-verb");
  if (countEl) countEl.textContent = count < WORDS.length ? WORDS[count] : String(count);
  if (nounWordEl && count > 1) nounWordEl.textContent = "Nouns";
  if (verbEl) verbEl.textContent = count > 1 ? "vote." : "votes.";

  if (nouns.length) {
    renderStack(nouns.map((x) => ({ id: x.id, owner: x.owner?.id })));
  }
}

loadDelegation();
