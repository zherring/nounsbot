// Dynamic hero: the Nouns delegated to the agent, as a playful stack.
// One Noun renders big; more pile up behind it, front 2-3 clearly visible.
// Hover a card to see the Noun + its owner/delegator.
// Set DELEGATE_ADDRESS when the bot EOA exists (M1). Preview: ?delegate=0x...

const DELEGATE_ADDRESS = ""; // <- bot EOA, lowercase, goes here at M1
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

function renderStack(nouns) {
  // nouns: [{id, owner}]
  const stack = document.getElementById("noun-gallery");
  if (!stack || nouns.length === 0) return;
  stack.innerHTML = "";
  stack.classList.add("filled", nouns.length === 1 ? "solo" : "pile");
  stack.classList.remove(nouns.length === 1 ? "pile" : "solo");

  const n = nouns.length;
  nouns.forEach((noun, i) => {
    const a = document.createElement("a");
    a.className = "noun-card";
    a.href = `https://nouns.wtf/noun/${noun.id}`;
    a.dataset.label = noun.owner
      ? `Noun ${noun.id} — delegated by ${short(noun.owner)}`
      : `Noun ${noun.id}`;

    const size = n === 1 ? 280 : Math.max(200 - i * 24, 92);
    const rot = n === 1 ? -2 : jitter(noun.id, i, 5) + (i === 0 ? 0 : i * 2.5 * (i % 2 ? 1 : -1));
    const dx = n === 1 ? 0 : -i * 26 + jitter(noun.id, i + 7, 12);
    const dy = n === 1 ? 0 : (i % 2 ? -1 : 1) * (i * 9 + jitter(noun.id, i + 13, 8));

    a.style.width = `${size}px`;
    a.style.height = `${size}px`;
    a.style.left = `calc(50% - ${size / 2}px)`;
    a.style.top = `calc(50% - ${size / 2}px)`;
    a.style.zIndex = String(n - i);
    a.style.setProperty("--dx", `${dx}px`);
    a.style.setProperty("--dy", `${dy}px`);
    a.style.setProperty("--rot", `${rot}deg`);

    const img = document.createElement("img");
    img.src = `https://noun.pics/${noun.id}`;
    img.alt = a.dataset.label;
    img.loading = "lazy";
    a.appendChild(img);
    stack.appendChild(a);
  });
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
  if (countEl) countEl.textContent = count < WORDS.length ? WORDS[count] : String(count);
  if (nounWordEl && count > 1) nounWordEl.textContent = "Nouns.";

  if (nouns.length) {
    renderStack(nouns.map((x) => ({ id: x.id, owner: x.owner?.id })));
  }
}

loadDelegation();
