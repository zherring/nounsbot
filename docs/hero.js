// Dynamic hero: count the Nouns delegated to the agent and show them.
// Set DELEGATE_ADDRESS when the bot EOA exists (M1). Until then the hero
// stays at "One". Preview with ?delegate=0x... in the URL.

const DELEGATE_ADDRESS = ""; // <- bot EOA, lowercase, goes here at M1

const SUBGRAPH = "https://www.nouns.camp/subgraphs/nouns";
const WORDS = [
  "Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight",
  "Nine", "Ten", "Eleven", "Twelve",
];

async function loadDelegation() {
  const param = new URLSearchParams(location.search).get("delegate");
  const address = (param || DELEGATE_ADDRESS).toLowerCase();
  if (!address) return;

  const query = `{
    delegate(id: "${address}") {
      delegatedVotes
      nounsRepresented(first: 100, orderBy: id) { id }
    }
  }`;
  let delegate;
  try {
    const resp = await fetch(SUBGRAPH, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ query }),
    });
    delegate = (await resp.json()).data?.delegate;
  } catch {
    return; // network failure: hero stays static
  }
  if (!delegate) return;

  const nouns = delegate.nounsRepresented || [];
  const count = Math.max(1, parseInt(delegate.delegatedVotes, 10) || 0);

  const countEl = document.getElementById("noun-count");
  const nounWordEl = document.getElementById("noun-word");
  if (countEl) countEl.textContent = count < WORDS.length ? WORDS[count] : String(count);
  if (nounWordEl && count > 1) nounWordEl.textContent = "Nouns.";

  const gallery = document.getElementById("noun-gallery");
  if (!gallery || nouns.length === 0) return;
  gallery.innerHTML = "";
  for (const { id } of nouns) {
    const a = document.createElement("a");
    a.href = `https://nouns.wtf/noun/${id}`;
    a.title = `Noun ${id}`;
    const img = document.createElement("img");
    img.src = `https://noun.pics/${id}`;
    img.alt = `Noun ${id}`;
    img.loading = "lazy";
    a.appendChild(img);
    gallery.appendChild(a);
  }
  gallery.classList.add("filled");
}

loadDelegation();
