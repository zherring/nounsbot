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
