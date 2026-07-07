# Signer plan: what contract should the eternal delegate address be?

**Problem.** Nouns delegation is address-sticky: `delegates(owner) → address`, no
forwarding, owner-only changes. The delegate address is therefore a permanent
rally point — if it's a raw EOA, every key rotation is an address change, and
every address change is a "everyone please re-delegate" campaign. The address
must become a smart account **before** delegations accumulate, so security
upgrades behind it never touch delegators again.

Candidates: **Safe + Zodiac Roles Modifier** vs **Splits multisig** (smart
accounts via the Splits platform — passkey + EOA signers, threshold, proposal/
sign/UserOp pipeline, `splits` CLI).

## Requirements

| # | Requirement | Why |
|---|---|---|
| R1 | Eternal address | Delegations point here forever |
| R2 | Key rotation without re-delegation | The whole point of the migration |
| R3 | Hot key scoped to voting only | Railway box holds it; popped box must yield only bad votes |
| R4 | Admin ops (signer changes) require the cold root — **enforced onchain** | Otherwise a popped hot key captures the eternal address permanently |
| R5 | Cast liveness = RPC only | A vote window must never be missed because a SaaS was down |
| R6 | Autonomous casting (no human per vote) | The constitution votes; the human holds a veto, not a pen |
| R7 | Low ops burden | One-person cheap experiment |

## Safe + Zodiac Roles Modifier

Owner = hardware wallet only (1-of-1). Bot EOA is **not an owner** — it holds a
Zodiac Roles role scoped to `castRefundableVoteWithReason` on the governor,
nothing else (no other targets, no value, no delegatecall, cannot touch the
Safe's own config).

| Req | Verdict | Notes |
|---|---|---|
| R1 | ✅ | Safe address never changes |
| R2 | ✅ | Owner revokes/assigns role membership; delegators untouched |
| R3 | ✅ **onchain-enforced** | Roles Modifier rejects everything but the one selector+target |
| R4 | ✅ **onchain-enforced** | Only the owner key can touch owners/modules/scopes |
| R5 | ✅ | Bot signs a plain tx to the Roles Modifier; needs only an RPC |
| R6 | ✅ | Threshold never involves a human for votes |
| R7 | ⚠️ | DIY: deploy ceremony, no dashboard, manual gas float on the bot EOA, refunds accrue to the Safe and need occasional sweeping |

Cons: setup ceremony (Safe + module + scope config, ~an afternoon); Zodiac is an
extra audited-but-real contract dependency; ops are raw Etherscan/CLI, no memos
or humans-friendly history.

## Splits multisig

Smart account with passkey signers (biometric, effectively the cold root) + EOA
signers explicitly designed for headless agents ("Primary use case: adding an
external (EOA) key so an agent or automation can operate on the account
headlessly"). Votes = `transactions create custom` (raw call to the governor) →
`transactions sign` with the local EOA → auto-submitted UserOp at threshold.

| Req | Verdict | Notes |
|---|---|---|
| R1 | ✅ | Smart account address survives signer changes ("updates apply to every active network") |
| R2 | ✅ | `accounts update-signers` swaps the bot EOA; delegators untouched |
| R3 | ❌ (as far as the CLI shows) | No per-signer target/function scoping visible. A threshold-1 bot EOA can propose+sign **any** custom transaction, not just votes. Mitigation: the account holds nothing but gas + refunds, so theft ceiling ≈ dust + bad votes — but the scope is policy-by-poverty, not enforcement |
| R4 | ❓ **the load-bearing unknown** | Signer-set changes "must be approved and signed on the web"; "recovery/resetting signers stays web-only." If that's **contract-enforced** (admin ops demand passkey sig / higher threshold onchain), capture resistance is real. If it's **backend policy**, a popped hot key at threshold 1 could bypass the API, construct its own UserOp, and rotate you out of your own rally point — the catastrophic outcome |
| R5 | ⚠️ | The normal cast path runs through the Splits API + bundler. Keys are local, so a manual UserOp is possible in an outage, but that's emergency surgery during a voting window |
| R6 | ✅ | EOA signer at threshold 1 casts headlessly |
| R7 | ✅✅ | Dashboard, tx history, memos, passkey UX, multi-chain sync — genuinely the best ops story, and it's your own product (support = a hallway conversation; this use case might even drive a per-signer-policy feature) |

One place Splits is *cleaner* than Safe: the 4337 account pays its own gas and
receives its own `castRefundable` refunds — self-contained, no bot-EOA gas float
to babysit.

## Decision

**Two questions decide it — both answerable by the Splits team this week:**

1. **(R4)** Are signer-set changes enforced *onchain* to require a passkey/web
   approval, or is web-only a backend policy in front of a contract that would
   accept any threshold-meeting signature?
2. **(R3)** Is there any per-signer transaction policy (target/selector
   allowlist) live or on the near roadmap?

- **If R4 = contract-enforced:** Splits is viable today. R3's gap is tolerable
  for a dust-only account (theft ceiling ≈ bad votes + gas), R7 is a big win,
  and dogfooding your own multisig on a public governance agent is worth
  something real. If R3 lands on the roadmap later, Splits becomes the
  strictly better choice.
- **If R4 = backend policy:** Splits is disqualified for *this* address — the
  rally point cannot be capturable by the hot key under any bypass — and the
  answer is **Safe + Zodiac**, whose R3/R4 are provable onchain today by anyone
  reading Etherscan (which is itself a trust signal for delegators).
- **Either way:** a Splits account is the right home for the *ops* side as this
  grows — gas treasury, refund sweeps, any future funding — where its UX wins
  and the R3/R4 stakes don't apply.

**Timeline guard:** the EOA→contract migration should happen before delegator
#2 shows up. If the R4 answer takes more than ~a week, deploy the Safe — the
cost of choosing "wrong" between two eternal-address designs is zero for
delegators (they delegate once either way); the cost of staying on the EOA is
a re-delegation campaign that grows with every new delegator.

---

## Appendix: exactly what's ambiguous in the `splits --llms` text (docs feedback)

Written for the Splits team: each item quotes the manifest verbatim, gives the
two readings the text supports, and suggests the sentence that would collapse
the ambiguity. The root pattern in both: **the docs specify the workflow but
not the enforcement layer** — and for a security evaluation, "what does the
chain reject" vs "what does the backend refuse" is the whole question.

### Q1 — Are signer-set changes contract-enforced or backend policy?

**The quotes doing the work** (all from `splits accounts update-signers`):

> "The proposal is created immediately; it **must be approved and signed on the
> web via the returned signUrl**."

> "**Recovery / resetting signers stays web-only.**"

and from `splits transactions sign`:

> "Fetches the transaction's signingHash, produces a personal_sign signature
> locally, and **submits it via POST /public/v1/transactions/:id/sign. By
> default auto-submits the UserOp** when this signature meets threshold."

**Why this underdetermines the answer.** "UserOp" confirms these are ERC-4337
accounts — which means the account contract's `validateUserOp` is the *real*
gatekeeper, and nothing in the manifest describes its rules. That leaves two
readings, both fully consistent with the quoted text:

- **Reading A (backend policy):** the contract accepts any threshold-meeting
  signature set for *all* operations, including signer changes. "Must be signed
  on the web" is the Splits API declining to accept CLI approvals for admin
  ops, and "web-only" describes product surface (no CLI command exists for
  recovery). Consequence: an attacker holding a threshold-meeting EOA key
  doesn't need the API — the contract is public; they construct a UserOp
  themselves, submit to any bundler, and rotate the signer set. The rule is
  bypassable by exactly the adversary it exists for.
- **Reading B (contract-enforced):** the account distinguishes operation
  classes onchain — signer/config changes require a passkey signature, a
  higher threshold, an owner-class signer, or a timelock. "Web-only" is then
  not UX but physics: the web is the only place a biometric passkey can sign,
  and the chain rejects admin UserOps signed by EOAs alone.

**Circumstantial evidence pointing at B — but never stated:** the
update-signers text says the proposal "must be approved and signed on the web"
*unconditionally*, even though a threshold-1 EOA org could plainly meet
threshold via `transactions sign` — suggesting admin ops are categorically a
different signature class. And `update-signers` motivates EOA signers with
"passkeys require a biometric 2nd factor that agents cannot provide,"
implying passkeys are a distinct, stronger signer class the platform leans on.
But neither line says the *contract* knows the difference.

**One sentence would resolve it.** Either: "Signer-set changes are enforced by
the account contract to require ≥1 passkey signature (equivalently: EOA
signers cannot modify the signer set even via UserOps submitted directly to
the EntryPoint)." Or the honest converse: "Web-only approval is platform
policy; onchain, any threshold-meeting signature set can modify signers —
size your EOA threshold accordingly."

### Q2 — Does any per-signer scoping exist?

**What the text shows.** From `splits transactions create custom`:

> "Create a transaction proposal with raw EVM calls. Use for **any on-chain
> action** including contract interactions, approvals, and swaps."

From `splits accounts create` / `update-signers`, the complete vocabulary for
configuring authority is: `--passkeyIds`, `--eoaSignerIds`, and

> "`--threshold` — Number of signers required to approve **transactions**"

**Why I concluded "no scoping" — and why I'm not certain.** This is an
absence-of-evidence conclusion: one global threshold, no per-signer or
per-operation parameters anywhere in the account schema, no policy/role/
allowlist vocabulary in the manifest. A signer appears to be a uniform-power
object. But two commands hint at an undocumented policy layer whose reach the
manifest never defines:

> "`splits automations list` — List automations for your org"

> "`splits tokens blocklist` — List your org's blocked tokens" /
> "`splits tokens whitelist` — List your org's allowlisted tokens"

If **automations** can execute onchain actions under constraints without
fresh threshold signatures, they may be precisely the scoped-execution
primitive R3 wants ("this key/automation may only call the governor") — but
the manifest gives them one line and no schema. If the **token allow/block
lists** are enforced at proposal/signing time (vs. being display filters),
that's evidence a policy-enforcement layer exists that could plausibly grow
target/selector scoping. The docs say neither.

**What would resolve it.** For each: state the enforcement point and the
boundary. "Automations execute [with / without] signer approval and are
constrained to [X]." "Token lists are enforced at [proposal creation /
signing / not enforced — informational]." And explicitly: "Per-signer
transaction restrictions (by target contract or function) [do not exist /
exist via X / are roadmapped]."

### Minor ambiguities noticed along the way (same root cause)

- **Cast liveness (R5):** `transactions sign` routes submission through
  `POST /public/v1/transactions/:id/sign` which "auto-submits the UserOp."
  Unstated: whether a signer holding local keys can construct and submit the
  UserOp independently if the API is unavailable (what's fetchable offline:
  account nonce, validation params, bundler access?).
- **Gas:** who pays for the UserOp — account balance or a Splits paymaster?
  Closest text is `transactions create transfer`: "Returns the proposal with
  gas estimates." Matters here because `castRefundableVoteWithReason` refunds
  `msg.sender` (the account), so the gas/refund loop is self-contained only
  if the account itself pays.
- **Contract identity:** the manifest never names the account implementation
  (audited? Safe-derived? custom validator?). For a "delegate to this address
  forever" pitch, "read the deployed contract yourself" is part of the trust
  story, and the docs don't say what a reader would find.

---

## Head-to-head test plan (run both, screencap everything)

Purpose: a level comparison of Safe+Zodiac vs Splits multisig as the eternal
delegate address — and a live falsification test of the Appendix ambiguities.
Every step marked 📸 is a screencap checkpoint; every step marked 🔬 directly
answers an open question from the appendix.

> **Session bootstrap (read first if you're a fresh chat).** Production is
> LIVE and must not be touched by this test: the agent runs on Railway
> (project `distinguished-surprise`, service `nounsbot`) casting from EOA
> `0xF6e7501dFe7003299108020c5830C4c5B3CA6aA9`, which holds Noun 1251's
> delegation. Rules: (1) never run `python -m bot.poller` locally — it fights
> Railway for the Telegram queue; (2) never print private keys or API keys
> into the transcript — pipe via stdin/env, suppress command output that
> echoes secrets; (3) the production EOA and its delegation stay untouched —
> all probes use fresh test keys; (4) casts from TEST accounts on live props
> are safe only because they carry zero voting weight (a "reached-the-
> governor" revert is a PASS for P1). Repo context: README (ops manual),
> PRD §13 (current state), this file (the plan).

### Inputs needed before starting

| Input | Used by | Notes |
|---|---|---|
| `COLD_1` (hardware wallet address) | both | The root of trust, same in both tests for fairness |
| `COLD_2` (optional second cold address) | both | Loss-redundancy, not a co-signer (threshold stays 1) |
| Two fresh hot keys: `HOT_SAFE`, `HOT_SPLITS` | one each | Never reuse the production EOA (0xF6e7…6aA9) — it stays live and untouched throughout |
| ~0.02 ETH test gas | both | ~0.01 Safe deploy+config, ~0.005 each for test calls |
| Splits org + owner-scoped API key + passkey enrolled | Splits | `splits auth login`; passkey is the cold root on this side |
| One ACTIVE Nouns prop id | both | The allowed-call target (any of 981–983 once voting opens) |

Constants: governor `0x6f3E6272A167e8AcCb32072d08E0957F9c79223d`; allowed
function `castRefundableVoteWithReason(uint256,uint8,string,uint32)` =
selector `0x8136730f`.

### The three probes (identical on both sides)

Each setup gets the same three attempts from its hot key:

- **P1 — allowed call:** cast a vote on the active prop. (A governor-side
  revert like "voting is closed" still counts as PASS for the wrapper — it
  proves the call *reached the governor*; the account layer let it through.)
- **P2 — disallowed call:** send 0.0001 ETH from the account to `COLD_1`.
  Safe+Zodiac predicts **hard block** at the Roles Modifier. Splits (per the
  manifest) predicts **allowed** — 🔬 that outcome, screencapped, is the R3
  finding in one image.
- **P3 — self-administration attempt:** try to change the signer/owner set
  using only the hot key. Safe+Zodiac predicts block (role can't reach the
  Safe). Splits: 🔬 observe whether the `update-signers` proposal can complete
  without a passkey/web signature — this is the UX-layer evidence for Q1
  (the contract-layer answer still needs the team, or a direct-UserOp probe).

### Track A — Safe + Zodiac (est. 45–60 min)

1. 📸 app.safe.global → create Safe on mainnet: owners `[COLD_1, COLD_2]`,
   threshold 1. Note deploy cost.
2. 📸 Zodiac Roles app (roles.gnosisguild.org) → enable Roles Modifier on the
   Safe (owner signs).
3. 📸 Create role `voter`: target `0x6f3E…223d`, function `0x8136730f` only,
   no delegatecall, no send. Assign member `HOT_SAFE`. (This screen *is* the
   R3 story on the Safe side: the scope is visible, onchain, auditable.)
4. Fund `HOT_SAFE` with ~0.005 ETH (it pays gas on this track).
5. 📸 Run P1, P2, P3 (P1/P2 via a small script we add to `bot/`; P3 = attempt
   `swapOwner` through the role — expect revert).
6. 📸 Rotation drill: owner revokes `HOT_SAFE`, assigns a new member. Count
   clicks/signatures; confirm the Safe address never changed.
7. Record: total setup time, gas spent, where the refund from a real
   `castRefundable` would land (the Safe), ops feel.

### Track B — Splits multisig (est. 20–30 min if the CLI is as smooth as it reads)

1. 📸 `splits auth login` → `splits auth whoami` (org, scopes).
2. 📸 `splits auth create-key --register` on the Railway-side machine →
   this becomes `HOT_SPLITS`; note the returned signer id.
3. 📸 `splits accounts create` — signers: your passkey + `HOT_SPLITS`,
   threshold 1. Record the account address. 🔬 Check: does the CLI/web state
   the account's contract implementation anywhere? (Appendix: contract
   identity.)
4. Fund the *account* with ~0.005 ETH. 🔬 Note who actually pays UserOp gas
   at execution (account balance vs paymaster) — appendix gas question.
5. 📸 P1: `splits transactions create custom` with the cast calldata →
   `splits transactions sign <id>` → confirm UserOp lands. 🔬 Note whether
   any step would work without the Splits API reachable (liveness question).
6. 📸 P2: `transactions create custom` transferring dust to `COLD_1`, sign,
   execute. Expected per manifest: succeeds → R3 finding.
7. 📸 P3: `splits accounts update-signers` swapping `HOT_SPLITS` for a fresh
   key using only the EOA/API — 🔬 does it demand the web `signUrl` +
   passkey even though threshold is met? Screencap whichever way it goes.
8. 📸 Rotation drill: complete the signer swap properly (web approval).
   Confirm account address unchanged. Count clicks vs Track A step 6.
9. Record: setup time, gas, refund destination, ops feel, dashboard views.

### Scoring sheet (fill in per track)

| Dimension | Safe+Zodiac | Splits |
|---|---|---|
| Setup time / #steps / gas | | |
| P1 allowed call reached governor | | |
| P2 disallowed call blocked? (R3) | | |
| P3 self-admin blocked? (R4, UX layer) | | |
| Rotation: clicks, address unchanged? (R2) | | |
| Cast path deps (RPC-only vs API) (R5) | | |
| Refund/gas loop self-contained? | | |
| "Read the contract" trust story | | |
| Ops UX (subjective, screencaps tell it) | | |

**Decision rule after the test:** if Splits P3 blocks without a passkey *and*
the team confirms that block is contract-enforced, Splits wins on ops and the
R3 gap is accepted for a dust-only account (or closed later by a policy
feature). Any other P3 outcome → Safe+Zodiac takes the rally point.

### What the agent needs after the winner is chosen

Either way, `bot/executor.py` gains a small adapter (Safe: wrap casts in
`execTransactionWithRole`; Splits: create-custom + sign via CLI/API), the
site's `DELEGATE_ADDRESS` moves to the winning account, and Noun 1251
re-delegates once — the last re-delegation any delegator ever performs.
