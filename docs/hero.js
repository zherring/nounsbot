// Dynamic hero: the Nouns delegated to the agent, as a playful stack.
// One Noun renders big; more pile up behind it, front 2-3 clearly visible.
// Hover a card to see the Noun + its owner/delegator.
// Nothing here is hardcoded as fact: the headline and stack come only from the
// subgraph. While loading, or if the query fails, the headline stays a neutral
// "Nouns vote." with no count and the stack stays hidden. A successful query
// that finds no delegation renders the honest "Zero Nouns vote."
// DELEGATE_ADDRESS is the vote-only bot EOA. Preview another: ?delegate=0x...

const DELEGATE_ADDRESS = "0xf6e7501dfe7003299108020c5830c4c5b3ca6aa9"; // vote-only bot EOA

// Public, keyless Goldsky endpoint (the nouns.camp proxy now 404s).
const SUBGRAPH =
  "https://api.goldsky.com/api/public/project_clnbcoajmebxn33wdbt98f439/subgraphs/nouns-mainnet/1.0.0/gn";
const WORDS = [
  "Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight",
  "Nine", "Ten", "Eleven", "Twelve",
];

// Noun images: read tokenURI() straight off the NounsToken contract instead
// of a third-party render service. noun.pics (the prior source) 500s or
// stalls for ~1 in 3 requests on cold ids — the chain is the one renderer
// that's always up, and it's the same SVG nouns.wtf shows.
const NOUNS_TOKEN = "0x9C8fF314C9Bc7F6e59A9d9225Fb22946427eDC03";
const TOKEN_URI_SELECTOR = "0xc87b56dd"; // tokenURI(uint256)
const RPC_ENDPOINTS = ["https://ethereum-rpc.publicnode.com", "https://rpc.ankr.com/eth"];
const imageCache = new Map();

async function rpcCall(method, params) {
  for (const url of RPC_ENDPOINTS) {
    try {
      const resp = await fetch(url, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
      });
      const json = await resp.json();
      if (json.result) return json.result;
    } catch {
      // try the next endpoint
    }
  }
  throw new Error("all RPC endpoints failed");
}

// ABI-decode a single dynamic `string` return value.
function decodeAbiString(hex) {
  const data = hex.slice(2);
  const length = parseInt(data.slice(64, 128), 16);
  const strHex = data.slice(128, 128 + length * 2);
  let str = "";
  for (let i = 0; i < strHex.length; i += 2) str += String.fromCharCode(parseInt(strHex.slice(i, i + 2), 16));
  return str;
}

async function onChainNounImage(id) {
  if (imageCache.has(id)) return imageCache.get(id);
  const calldata = TOKEN_URI_SELECTOR + BigInt(id).toString(16).padStart(64, "0");
  const result = await rpcCall("eth_call", [{ to: NOUNS_TOKEN, data: calldata }, "latest"]);
  const tokenUri = decodeAbiString(result);
  const meta = JSON.parse(atob(tokenUri.split(",")[1]));
  imageCache.set(id, meta.image);
  return meta.image;
}

async function loadNounImage(id, img) {
  try {
    img.src = await onChainNounImage(id);
  } catch {
    img.src = `https://noun.pics/${id}`; // last-resort fallback if every RPC is unreachable
  }
}

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
  a.href = `https://www.nouns.camp/nouns/${noun.id}`;
  a.dataset.label = noun.owner
    ? `Noun ${noun.id} — delegated by ${short(noun.owner)}`
    : `Noun ${noun.id}`;
  a.style.setProperty("--rot", `${rot}deg`);
  const img = document.createElement("img");
  img.alt = a.dataset.label;
  img.loading = "lazy";
  a.appendChild(img);
  loadNounImage(noun.id, img);
  return a;
}

function renderStack(nouns) {
  // nouns: [{id, owner}] — featured + side picks are randomized per load.
  // An empty list clears the stack and hides the gallery (zero state).
  const stack = document.getElementById("noun-gallery");
  if (!stack) return;
  stack.innerHTML = "";
  stack.classList.toggle("filled", nouns.length > 0);
  if (nouns.length === 0) return;

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
  if (!resp.ok) throw new Error(`subgraph HTTP ${resp.status}`);
  const json = await resp.json();
  if (json.errors?.length) throw new Error(json.errors[0].message || "subgraph error");
  if (!json.data) throw new Error("subgraph returned no data");
  return json.data;
}

// "<count> Noun(s) vote(s)." — a null count is the neutral, number-free headline.
function setHeadline(count) {
  const countEl = document.getElementById("noun-count");
  const nounWordEl = document.getElementById("noun-word");
  const verbEl = document.getElementById("noun-verb");
  const singular = count === 1;
  if (countEl) countEl.textContent = count === null ? "" : count < WORDS.length ? WORDS[count] : String(count);
  if (nounWordEl) nounWordEl.textContent = singular ? "Noun" : "Nouns";
  if (verbEl) verbEl.textContent = singular ? "votes." : "vote.";
}

async function loadDelegation() {
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
    delegate = data.delegate; // null = no such delegate, i.e. nothing delegated
  } catch {
    return; // query failed: claim nothing, the neutral headline stays
  }

  const count = delegate ? parseInt(delegate.delegatedVotes, 10) : 0;
  if (Number.isNaN(count)) return; // malformed response: same as a failure

  setHeadline(count);
  renderStack(count > 0 ? (delegate.nounsRepresented || []).map((x) => ({ id: x.id, owner: x.owner?.id })) : []);
}

setHeadline(null); // neutral until the subgraph answers
loadDelegation();
