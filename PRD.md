# Constitution Forge — PRD v0.1

A personal governance agent for Nouns DAO. I write a constitution; an agent evaluates every proposal against it; I ratify or override via Telegram; votes are cast onchain with clause-cited reasoning; a public page shows the constitution, the voting record, and a delegate button.

**Status:** V1 LIVE (see §13) · **Owner:** zach · **Last updated:** 2026-07-05

---

## 1. Problem

Nouns governance has calcified into a specific failure mode:

- **A captured voting bloc.** A minority accumulated a large position (~24% of votable supply) at prices far below treasury book value. Their position is a claim on the treasury, and their optimal strategy is to freeze the DAO: vote down or starve every grant, kill every structural fix, and let book value ratchet up while spending goes to zero.
- **A passive, defeated majority.** The bloc doesn't need to win arguments — it needs dormant supply to stay dormant. Quorum starvation and last-hours ambush voting beat organic proposals without ever persuading anyone. The cost of *consistently* participating in governance (reading every prop, tracking a ~9-day lifecycle each, showing up every time) is what keeps the majority passive.
- **Time is the weapon.** Every quarter of inactivity accretes value to the squatting position. Delay is free for them and expensive for the mission.

The core insight: **the bottleneck is not voting power, it's attention.** Lowering the cost of forming and expressing a consistent governance position converts dormant supply into standing votes — which directly attacks both dependencies of the freeze strategy (dormancy and free time).

## 2. Thesis

- The **constitution is the product**. A versioned markdown document stating my governance values and decision rules is the durable artifact — the account, the identity, the thing worth forking. Agents are fungible plumbing that execute it.
- **Delegation-seeking is a red herring.** The point is not to accumulate power; it's to delegate *my time* to an agent that reflects *my values*, and to make that pattern legible and copyable. (This aligns with Vitalik's Feb 2026 argument: delegation concentrates power; personal AI agents voting per stated preferences re-enfranchise.)
- **Consistency compounds.** A 1-Noun voter who shows up on every prop, every candidate, every flip window, with public clause-cited reasoning, punches far above its weight — and produces a verdict dataset that makes the constitution testable and forkable later.

Prior art: GoverNoun (agent-as-candidate; didn't compound), Event Horizon (7M ARB delegated to personal voting agents — proof that "users want to be heard but don't want to do governance" — but needed yield subsidies), x23.ai/Karma (summarizers only). Nobody has done **constitution-as-forkable-document**.

## 3. Goals / Non-goals

### V1 goals
1. Every Nouns proposal is evaluated against `constitution.md` within hours of becoming evaluable, and my Noun votes on 100% of active props.
2. Every vote is cast onchain **with clause-cited reasoning** visible in Nouns clients.
3. I spend ≤ 10 minutes/week on governance (ratification taps + weekly thesis review).
4. The constitution, live verdicts, and full voting record are public at a URL, with a delegate button for anyone who wants their Noun to follow the same constitution.
5. Every verdict, override, and outcome is logged — the backtest dataset for V2.

### Non-goals (V1)
- Multi-user infrastructure, hosted constitutions, or per-delegator customization (that's V2).
- Campaigning for delegation. The delegate button exists; we don't chase it.
- Autonomous voting without a ratification window (agent never casts without my window to override — see §6.4).
- Winning votes. The goal is standing, consistent, legible participation.

## 4. Users

| User | Need | Surface |
|---|---|---|
| **Me (primary)** | Vote my values on everything without spending the time | Telegram ratification + agent runtime |
| **Nouns voters/observers** | See what this delegate stands for and how it has voted, verify reasoning | Public site |
| **Potential delegators** | One-click delegate to a constitution they've read | Public site delegate button |
| **Future forkers (V2)** | A constitution format + verdict dataset worth forking | Repo + published record |

## 5. Surfaces

Three surfaces, one repo:

1. **Public site** (static, GitHub Pages or equivalent):
   - The constitution, rendered from `constitution.md`, with version history (it's just git).
   - Live proposal verdicts: prop → vote → confidence → clauses cited → reasoning.
   - Full voting record + weekly thesis archive.
   - **Delegate button**: connect wallet → `delegate(agentAddress)` on the Nouns token. No custody, no signup, reversible any time.
2. **Agent runtime** (single Python process + SQLite, deployed on Railway/Fly for always-on):
   - Ingest → enrich → evaluate → notify → execute → publish loop (see §6).
3. **Telegram** (control channel):
   - Verdict cards with ✅ ratify / ❌ override / 💬 discuss.
   - Tripwire alerts (flips, late-window vote dumps, prop edits, euphemism-pattern props).

## 6. V1 Functional requirements

### 6.1 Ingest (event-driven, not cron)
- Subscribe to Nouns governor + candidate events via subgraph polling (~1–5 min interval). Props drop continuously (~2–5/week), each with its own ~9-day clock; a weekly batch loop cannot meet the objection-window and edit-window requirements.
- Track **proposals** (ProposalCreated/Updated/Canceled/Vetoed, vote tallies) and **candidates** (ProposalCandidateCreated/Updated, signatures, feedback).
- Read all governance parameters (voting delay/period, thresholds, quorum params, fork params) **from the governor at runtime** — every one of them is DAO-adjustable; hardcode nothing.

### 6.2 Enrichment
- Decode every action's calldata; verify the transactions **match the prose claims**. Mismatch ⇒ auto-flag, never auto-ratify.
- Proposer history: prior props, outcomes, prior payouts.
- Candidate-phase context: Farcaster discussion, candidate feedback signals.
- Pin every verdict to a **content hash** of (description + actions). Props are editable for the first ~2.5 days; `ProposalUpdated` ⇒ invalidate verdict, re-evaluate, re-notify with a diff.

### 6.3 Evaluation
- One Claude call per evaluable prop version. `constitution.md` in the system prompt; proposal text **quarantined as untrusted data** (prompt-injection surface — prop authors know agents are reading).
- Output schema: `{vote: FOR|AGAINST|ABSTAIN, confidence: 0–1, clauses_cited: [...], draft_reason: str, flags: [...]}`.
- Low confidence or any flag (calldata mismatch, injection suspicion, constitution gap) ⇒ escalate to me with the specific uncertainty named.

### 6.4 Ratification (Telegram — optimistic execution with a hold window)
The agent never asks permission and never acts without a veto window. Every decision is reported; unflagged decisions cast themselves on a timer; one command stops any of them.

- **Verdict card per evaluation** (and re-evaluation): prop, verdict, confidence, clauses cited, draft reason, and the **scheduled cast time**. Plus a daily digest of everything open (verdicts pending cast, holds, timers) whenever ≥1 item is open.
- **Default-fire with a guaranteed gap.** High-confidence, unflagged verdicts cast automatically at the scheduled time (~60–70% through the voting window). There must be **≥24h between the verdict card and the cast**; since cards normally post when voting opens (or during the updatable phase), the natural gap is ~2.5 days. If a late re-evaluation (prop edit, flip) compresses the gap below 24h, default-fire is disabled for that prop and an explicit command is required.
- **Commands:**
  - `/hold <prop>` — freeze the cast. Nothing casts while held; reminders at 75% and 90% of the voting window.
  - `/release <prop>` — resume the scheduled cast.
  - `/override <prop> <for|against|abstain> <reason>` — reason is **mandatory**; replaces the verdict and casts on schedule.
  - `/cast <prop>` — cast immediately (also the explicit ratification for flagged props).
  - `/status` — all open props: verdict, timer, held/flagged state.
- **Flagged verdicts never default-fire.** Structural props (Art. II), calldata mismatch, injection suspicion, low confidence, or a compressed gap all require an explicit `/cast` or `/override`. Silence on a flagged prop = no vote, logged publicly as such.
- **Hold wins at the deadline.** A held prop whose window closes simply doesn't get a vote; the miss is logged publicly. Hold means "not without me," never "cast anyway eventually."
- The override log is the diff between my written constitution and my actual constitution — it drives amendments and is the most valuable training data we produce.

### 6.5 Execution
- `delegate()` my Noun once to a fresh hot EOA. The bot key can **vote but never transfer** — worst case on key compromise is bad votes until I re-delegate.
- Cast via `castRefundableVoteWithReason` (gas refunded; reasoning renders in every Nouns client), at ~60–70% through the voting window — late enough to see the field, early enough to never miss a window.
- Register a **client ID** for the client incentives program (paid per facilitated vote/prop).
- **Objection-window watch:** a late defeated→successful flip opens a window where non-voters can only vote AGAINST. If we haven't voted and a flip occurs, alert immediately — a 1-Noun AGAINST has maximum leverage exactly there.

### 6.6 Candidate lane (disproportionate leverage)
- Proposals need sponsor signatures meeting the proposal threshold; my 1 Noun = a meaningful fraction.
- Evaluate candidates against the constitution: output sponsor / feedback-for / feedback-against + drafted reason, sent via `CandidateFeedbackSent`. Ratify via the same Telegram flow.
- **Pooled auto-sponsorship (the delegation pitch, post-V1):** delegated weight
  doesn't just vote — it sponsors. When a candidate earns a FOR verdict, the bot
  signs the EIP-712 sponsorship with its delegated votes (`addSignature` on the
  Nouns Data contract), and once enough weight accrues the candidate promotes to
  a proposal via `proposeBySigs` without the proposer wrangling sponsors.
  Delegating to the constitution then means: your Noun votes every prop AND
  lowers the barrier for mission-aligned builders to even get on the ballot —
  a direct attack on the "proposals starve before they're born" half of the
  freeze. Same ratification gates as votes; sponsorship is never auto-fired
  for structural candidates.

### 6.7 Tripwires
Alert (Telegram, and publish where appropriate) on:
- Late-window vote dumps (large weight arriving in the final hours of voting).
- Prop edits during the updatable window (always re-evaluate; publish the diff).
- Defeated→successful or successful→defeated flips near window end.
- Euphemism-pattern props: anything touching treasury mechanics, buybacks, entity structure, or governance parameters gets flagged for manual review **regardless of the agent's verdict**.

### 6.8 Publishing
- Weekly thesis compiled **from votes already cast** (never speculative): what passed, how we voted, which clauses did the work. Posted to Farcaster + archived on the site.
- Verdict table (prop, content hash, verdict, confidence, clauses, my override if any, final outcome) is public and append-only — it doubles as the backtest dataset.

## 7. Proposal state machine

```
CANDIDATE ──(4 sigs)──▶ UPDATABLE ──▶ PENDING ──▶ ACTIVE ──▶ (QUEUED ▶ EXECUTED | DEFEATED | VETOED)
    │                        │            │          │
 evaluate               evaluate      snapshot     cast at ~60-70%,
 sponsor/feedback       provisionally  lands;      watch for flips
 via CandidateFeedback  re-eval on     final       + objection window
                        every edit     evaluation
```

Key timing facts (verified, but **read from chain at runtime**): lifecycle ~9 days — updatable ~2.5d, pending ~0.5d, voting 4d, queue 2d. Vote snapshot lands after the voting delay, **not** at creation. Verdicts pinned to content hash; edits invalidate.

## 8. Architecture

```
┌─ Railway/Fly ──────────────────────────────────────────┐
│  Python process                                        │
│  ├─ poller (subgraph + governor reads)                 │
│  ├─ enricher (calldata decode, history, Farcaster)     │
│  ├─ evaluator (Claude, constitution.md in sys prompt)  │
│  ├─ telegram bot (ratify/override/alerts)              │
│  ├─ executor (hot EOA, castRefundableVoteWithReason)   │
│  └─ publisher (site data JSON, weekly thesis)          │
│  SQLite: props, versions, verdicts, overrides, casts   │
└────────────────────────────────────────────────────────┘
        ▲                                    │
        │ git pull constitution.md           │ push verdicts.json / record
┌───────┴────────────────────────────────────▼───────────┐
│  This repo → static site (GitHub Pages)                │
│  constitution.md · verdict feed · record · delegate btn│
└────────────────────────────────────────────────────────┘
```

- **Constitution is git-versioned in this repo.** The runtime pulls it; amendments are commits (V2 forking falls out of this for free).
- Site is static; runtime pushes verdict/record JSON into the repo (or an equivalent static store) — no server needed for the public surface.
- Secrets (bot key, Telegram token, RPC, Anthropic key) live only on the runtime host.

## 9. Security & failure model

| Risk | Mitigation |
|---|---|
| Bot key compromise | Fresh EOA holds delegation only — can vote, can never transfer the Noun. Re-delegate to rotate. |
| Prompt injection via prop text | Prop content quarantined as untrusted data; injection-suspicion flag ⇒ mandatory manual review. |
| Prose/calldata mismatch | Calldata is ground truth; mismatch auto-flags, never auto-ratifies. |
| Prop edited after verdict | Content-hash pinning; `ProposalUpdated` invalidates and re-evaluates. |
| Governance params changed under us | All params read from governor at runtime. |
| Runtime down during a window | Missed-cast alarm: any ACTIVE prop without a cast at 80% of window ⇒ page me. |
| Agent drifts from my values | Ratification gate + mandatory override reasons + weekly review of the override log. |

## 10. Milestones

- ✅ **M0 — Paper agent.** DONE 2026-07-05. Constitution drafted and battle-tested to v0.4 across a 24-prop backtest; three amendments each triggered by a real divergence (969 → II.4 direction test, 970 → I.5 recognition in kind, 962 → II.5 reconnaissance clause). Tiered evaluation (Sonnet condenser → Opus judge), verdicts + full re-evaluation history published.
- ✅ **M1 — Hands on chain.** DONE 2026-07-05. Vote-only EOA `0xF6e7…6aA9`, Noun 1251 delegated (via the site's own delegate button), `castRefundableVoteWithReason` encoding validated against the live governor, Telegram ratification loop (`/status /hold /release /override /cast`), default-fire at 65% of window with 24h floor, flagged-never-fires, hold-wins-at-deadline, spend guards. Client ID: still 0, registration deferred.
- ✅ **M2 — Public surface (mostly).** Site live (Railway-served; nounsvote.com DNS deferred as a cheap-experiment call): constitution with amendment log, live verdict record with per-version history, dynamic delegation hero, in-app delegate button. Weekly Farcaster thesis: NOT built.
- **M3 — Full coverage (next).** Candidate lane + pooled auto-sponsorship (§6.6), calldata ABI decoding, tripwires, objection-window watch, missed-cast dead-man alarm, client ID registration.
- **V2 (later, separate PRD).** Hosted constitutions: fork mine, answer 3 questions, instant backtest against my verdict record, shadow-vote every live prop with no wallet. The verdict table is the dataset that makes this possible.

## 11. Open questions

1. ~~**Ratification default**~~ — **resolved (2026-07-05):** optimistic execution with a hold window (§6.4). Every decision reported, unflagged verdicts default-fire with a ≥24h guaranteed gap, `/hold` vetoes, flagged verdicts always require an explicit command.
2. ~~**Delegate button timing**~~ — **resolved (2026-07-05):** shipped at launch with the paper record as the track record. In-app `delegate()` tx, no libraries.
3. ~~**Site data path**~~ — **resolved (2026-07-05):** both. The runtime serves `/verdicts.json` live from SQLite (freshness) AND commits it to the repo (tamper-evident audit trail; excluded from redeploy triggers).
4. ~~**Constitution v1 scope**~~ — **resolved:** narrow-and-opinionated won. v0.1 was ~6 articles; three real divergences grew it to v0.4 within one backtest cycle — the override→amend loop works.
5. **Vote privacy (prop 972)** — open; see the constitution's "worth debating" section. Transparency-as-weapon vs privacy-as-shield.
6. **982-class direction calls** — is delegating treasury Nouns to a community body control-concentration (III.2) or dormancy-activation (II.4-adjacent)? Candidate for the next amendment.

## 13. Current state & next steps (2026-07-05)

**Live:** Railway project `distinguished-surprise`/`nounsbot`, volume `/data` —
**no inbound networking** (hardened 2026-07-05: key box is outbound-only; the site
is static GitHub Pages at nounsvote.com, updated via contents-scoped
git commits). Bot EOA `0xF6e7501dFe7003299108020c5830C4c5B3CA6aA9`
holds 1 delegated vote (Noun 1251). Telegram channel wired (`⌐◨-◨ Constitution Forge`).
Constitution v0.4. Two-speed loop: commands/casts/publish every 120s; ingest every
120s with spend guards. Verdict record: 22 paper + 3 live-rev verdicts (981 AGAINST⚑,
982 AGAINST⚑ — both awaiting explicit human ratification; 983 FOR, auto-cast armed).

**First cast landed 2026-07-08:** prop 982, AGAINST (weight 1 — snapshot predated Winter's delegation of Noun 456; props with later snapshots carry weight 2). Verdicts now append a '[ suggestions ]' section — constructive alignment feedback published with every reason. Next: prop 983, ~65% through its voting window, will be
the record's first "🗳 cast" row.

**Near-term queue (M3), in priority order:**
1. Calldata ABI decoding in the enricher — removes the "couldn't verify vs verified
   mismatch" ambiguity (the 957 flip-flop).
2. Missed-cast dead-man alarm (healthchecks.io ping per tick).
3. ~~Candidate lane~~ — SHIPPED 2026-07-08: candidates evaluated like props, 🌿 cards, /sponsor signs the EIP-712 sponsorship (digest replica validated byte-exact against 3 real onchain signatures); never automatic, edits invalidate. Update candidates are grouped by target proposal and use the UpdateProposal typed-data digest; the contract only permits the proposal's original signers to re-sign them.
4. Tripwires: late-window vote-dump alerts, defeated→successful flip alerts,
   objection-window watch.
5. Client ID registration (client incentives).
6. ~~nounsvote.com DNS~~ — done, live with enforced HTTPS.
7. **EOA → smart-account delegate address** before delegator #2 arrives — see [SIGNER-PLAN.md](SIGNER-PLAN.md) (Safe+Zodiac vs Splits multisig; decision pending two answers from the Splits team).

**Operational notes:** never run the poller locally while Railway is live (Telegram
offset contention). Amendments: edit constitution.md + docs/constitution.html, add
rev to docs/amendments.json, commit with trigger, push (auto-redeploy re-evaluates
open props). `GIT_PUSH_TOKEN` not yet set on Railway — the git audit trail of
verdicts.json only updates from local runs until it is.

## 12. Success criteria (90 days)

- 100% vote participation on props entering voting after M1 goes live; zero missed windows.
- ≥ 1 candidate sponsored or substantively fed back per month.
- Override rate trending down (constitution converging on my actual values).
- My governance time ≤ 10 min/week.
- ≥ 1 Noun delegated by someone who isn't me (validation signal, not a goal).
