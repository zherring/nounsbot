// In-app delegation: builds the Nouns token delegate() tx in the browser.
// No libraries, no custody — the user's own wallet signs one transaction,
// reversible any time by delegating elsewhere.

const NOUNS_TOKEN = "0x9C8fF314C9Bc7F6e59A9d9225Fb22946427eDC03";
const BOT_DELEGATE = "0xF6e7501dFe7003299108020c5830C4c5B3CA6aA9";
const DELEGATE_SELECTOR = "0x5c19a95c"; // delegate(address)

function delegateCalldata() {
  return DELEGATE_SELECTOR + BOT_DELEGATE.slice(2).toLowerCase().padStart(64, "0");
}

let statusCounter = 0;

function setStatus(button, text, cls) {
  // Land the status line as a full-width block right after the whole
  // button row, not inside the button's own wrapper — that wrapper sits
  // in a flex row next to other buttons, so appending there squeezes the
  // message beside them instead of on its own line.
  const row = button.closest(".btn-row") || button.parentElement;
  if (!row.dataset.statusId) row.dataset.statusId = `delegate-status-${statusCounter++}`;
  let el = document.getElementById(row.dataset.statusId);
  if (!el) {
    el = document.createElement("p");
    el.id = row.dataset.statusId;
    el.className = "delegate-status muted";
    row.insertAdjacentElement("afterend", el);
  }
  el.textContent = text;
  el.dataset.state = cls || "";
}

// EIP-6963: discover every injected wallet instead of racing for window.ethereum.
// This is the same discovery standard RainbowKit uses — minus React and a bundler.
const discoveredWallets = [];
window.addEventListener("eip6963:announceProvider", (event) => {
  const { info } = event.detail;
  if (!discoveredWallets.some((w) => w.info.uuid === info.uuid)) {
    discoveredWallets.push(event.detail);
  }
});
window.dispatchEvent(new Event("eip6963:requestProvider"));

function statusEl(button) {
  setStatus(button, ""); // ensure the element exists
  const row = button.closest(".btn-row") || button.parentElement;
  return document.getElementById(row.dataset.statusId);
}

function showMobileWalletLinks(button) {
  const el = statusEl(button);
  const here = location.host + location.pathname;
  el.innerHTML =
    `No wallet in this browser. Open this site inside your wallet app: ` +
    `<a href="https://metamask.app.link/dapp/${here}">MetaMask</a> · ` +
    `<a href="https://go.cb-w.com/dapp?cb_url=${encodeURIComponent(location.href)}">Coinbase Wallet</a> · ` +
    `or paste <b>${location.href}</b> into any wallet's built-in browser (Rainbow: 🌈 tab).`;
}

function pickWallet(button) {
  // 0 discovered: legacy window.ethereum or nothing. 1: use it. 2+: let the user choose.
  if (discoveredWallets.length === 0) return Promise.resolve(window.ethereum || null);
  if (discoveredWallets.length === 1) return Promise.resolve(discoveredWallets[0].provider);
  return new Promise((resolve) => {
    const el = statusEl(button);
    el.innerHTML = "Choose a wallet: ";
    for (const w of discoveredWallets) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "wallet-choice";
      b.textContent = w.info.name;
      b.addEventListener("click", () => resolve(w.provider));
      el.appendChild(b);
    }
  });
}

async function delegateFlow(button) {
  const eth = await pickWallet(button);
  if (!eth) {
    if (/iPhone|iPad|Android/i.test(navigator.userAgent)) {
      showMobileWalletLinks(button); // reopen inside the wallet's own browser
    } else {
      window.open(`https://etherscan.io/address/${NOUNS_TOKEN}#writeContract`, "_blank");
      setStatus(button, `No wallet detected — use delegate(${BOT_DELEGATE}) on Etherscan.`);
    }
    return;
  }
  try {
    setStatus(button, "Connecting wallet…");
    const [account] = await eth.request({ method: "eth_requestAccounts" });

    const chainId = await eth.request({ method: "eth_chainId" });
    if (chainId !== "0x1") {
      setStatus(button, "Switching to Ethereum mainnet…");
      await eth.request({
        method: "wallet_switchEthereumChain",
        params: [{ chainId: "0x1" }],
      });
    }

    setStatus(button, "Confirm the delegation in your wallet…");
    const txHash = await eth.request({
      method: "eth_sendTransaction",
      params: [{ from: account, to: NOUNS_TOKEN, data: delegateCalldata() }],
    });

    setStatus(button, "Delegation submitted — waiting for confirmation…");
    for (let i = 0; i < 60; i++) {
      const receipt = await eth.request({
        method: "eth_getTransactionReceipt",
        params: [txHash],
      });
      if (receipt) {
        if (receipt.status === "0x1") {
          setStatus(button, "✓ Delegated. Your Noun now votes by the constitution. (Re-delegate anywhere, any time, to leave.)", "ok");
          if (typeof loadDelegation === "function") setTimeout(loadDelegation, 4000);
        } else {
          setStatus(button, "Transaction reverted — nothing changed.");
        }
        return;
      }
      await new Promise((r) => setTimeout(r, 5000));
    }
    setStatus(button, `Submitted: ${txHash.slice(0, 14)}… — check your wallet for the result.`);
  } catch (err) {
    if (err && (err.code === 4001 || err.code === "ACTION_REJECTED")) {
      setStatus(button, "Cancelled — nothing sent.");
    } else {
      setStatus(button, `Wallet error: ${err?.message || err}`);
    }
  }
}

document.querySelectorAll(".btn-delegate").forEach((button) => {
  button.addEventListener("click", (e) => {
    e.preventDefault();
    delegateFlow(button);
  });
});

// Dev helper: force any delegate-status visual state without a real wallet
// or transaction (there's no such thing as a throwaway re-delegate to test
// against). Console: __mockDelegate("success")  or  __mockDelegate("pending", 1)
// for the second button on the page. URL: index.html?mock=success applies it
// to the first button on load.
const MOCK_STATES = {
  idle: ["", ""],
  "no-wallet": [`No wallet detected — use delegate(${BOT_DELEGATE}) on Etherscan.`, ""],
  connecting: ["Connecting wallet…", ""],
  switching: ["Switching to Ethereum mainnet…", ""],
  confirm: ["Confirm the delegation in your wallet…", ""],
  pending: ["Delegation submitted — waiting for confirmation…", ""],
  success: [
    "✓ Delegated. Your Noun now votes by the constitution. (Re-delegate anywhere, any time, to leave.)",
    "ok",
  ],
  reverted: ["Transaction reverted — nothing changed.", ""],
  timeout: ["Submitted: 0xabc123def4567890… — check your wallet for the result.", ""],
  cancelled: ["Cancelled — nothing sent.", ""],
  error: ["Wallet error: user rejected the request.", ""],
};

window.__mockDelegate = function (state, buttonIndex = 0) {
  const entry = MOCK_STATES[state];
  const button = document.querySelectorAll(".btn-delegate")[buttonIndex];
  if (!button || !entry) {
    console.log("Usage: __mockDelegate(state, buttonIndex?) — states:", Object.keys(MOCK_STATES).join(", "));
    return;
  }
  setStatus(button, entry[0], entry[1]);
};

const mockParam = new URLSearchParams(location.search).get("mock");
if (mockParam) window.__mockDelegate(mockParam);
