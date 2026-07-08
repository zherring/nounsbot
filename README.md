# Constitution Forge

A personal governance agent for Nouns DAO. The constitution is the product; the
agent is plumbing. **V1 is live**: Noun 1251 votes by a written constitution, and
anyone can delegate theirs to the same standing vote — or fork the repo and run
their own.

- **Site:** https://nounsvote.com — GitHub Pages, fully static, zero keys
- **Bot delegate (vote-only EOA):** `0xF6e7501dFe7003299108020c5830C4c5B3CA6aA9`
- **Constitution:** [constitution.md](constitution.md) — v0.4, amendment history on the site
- **Spec:** [PRD.md](PRD.md) · **Status & roadmap:** see the bottom of the PRD

## How it works

```
Railway loop (bot/poller.py)
 ├── every 120s ── subgraph ingest → new/edited props AND candidates →
 │                 evaluate → Telegram card (spend-guarded: ≤3/prop/day, ≤20/day)
 ├── every 120s ── Telegram commands: /status /hold /release /override /cast
 ├── every 120s ── cast scheduler: auto-fire unflagged verdicts at 65% of the
 │                 voting window, ≥24h after the verdict; flagged props NEVER
 │                 auto-fire; a held prop just doesn't vote (logged publicly)
 ├── castRefundableVoteWithReason ── the vote, gas refunded, reason onchain
 └── commits docs/verdicts.json to this repo ── record + audit trail
     (the key box accepts NO inbound connections; the site is static Pages)
Browser (no bot involvement)
 ├── hero: delegation count + Noun gallery straight from the Nouns subgraph
 └── delegate button: builds the delegate() tx in-page — no custody, reversible
```

**Evaluation pipeline** (`bot/evaluator.py`): proposals over ~6k chars get crunched
by Sonnet 5 into a structured brief; Opus 4.8 judges brief + raw onchain actions
against the constitution (in the system prompt, prompt-cached). Proposal text is
quarantined as untrusted data (prompt-injection defense). Output: vote, confidence,
clauses cited, publishable reason, flags. Verdicts are keyed to
`(prop, content-hash, constitution git rev)` — edits and amendments re-evaluate,
history is append-only and published.

## Operating it (Telegram)

Channel: `⌐◨-◨ Constitution Forge`. Cards arrive per verdict with the scheduled
cast time. Commands:

| Command | Effect |
|---|---|
| `/status` | all open props: verdict, state, cast block, flags |
| `/hold <id>` | freeze the cast; hold wins at the deadline (no vote, public miss) |
| `/release <id>` | back on schedule |
| `/override <id> <for\|against\|abstain> <reason>` | replace the verdict; reason mandatory + logged |
| `/cast <id>` | cast now — also the explicit ratify for ⚑flagged props |
| `/sponsor c<num>` | sign EIP-712 sponsorship for a candidate with our delegated weight (from its 🌿 card; never automatic) |

**Never run `python -m bot.poller` locally while Railway is live** — two loops
fight over the Telegram update queue.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # ANTHROPIC_API_KEY at minimum

.venv/bin/python -m bot.backtest --last 20 --dry-run   # cost estimate, no API calls
.venv/bin/python -m bot.backtest --last 20             # judge history vs constitution
.venv/bin/python -m bot.backtest --from-id 955 --to-id 980
.venv/bin/python -m bot.telegram                       # discover channel chat id
.venv/bin/python -m bot.keygen                         # generate vote-only EOA (run yourself)
```

Backtests write agreement/divergence reports to `backtests/`. Divergence from
history is the point: the constitution disagrees with the DAO's outcomes exactly
where the DAO was frozen (see the [faction page](docs/faction.html)).

Amending the constitution: edit `constitution.md` (+ mirror `docs/constitution.html`),
add the new git rev to `docs/amendments.json`, commit with the trigger in the
message, push. Railway redeploys and re-evaluates open props under the new rev.

## Deployment (Railway)

Project `distinguished-surprise` → service `nounsbot` (repo dir is `railway link`ed).
Auto-deploys on push to main (`railway.json` — the publisher's own verdicts.json
commits are excluded from redeploy triggers). Volume at `/data` holds SQLite;
`data/seed.db` bootstraps fresh volumes with the paper-era history.

Env vars (set via `railway variables --set`): `ANTHROPIC_API_KEY`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `BOT_PRIVATE_KEY`, `DB_PATH`,
`GIT_PUSH_TOKEN` (GitHub fine-grained PAT, contents:write on this repo only —
**required**: it's how verdicts reach the static site), plus optional
`INGEST_INTERVAL_SECONDS`, `MAX_EVALS_PER_DAY`, `MAX_EVALS_PER_PROP_PER_DAY`,
`NOUNS_CLIENT_ID`, `RPC_URL`.

**Hardening (post-Winter):** the Railway service has no public domain and no
inbound port — the box holding the key is outbound-only (subgraph, RPC, Anthropic,
Telegram, GitHub API). The public site is GitHub Pages: static files, no runtime,
no keys. Worst case per credential: ETH key → bad votes until re-delegation;
`GIT_PUSH_TOKEN` → site defacement, reverted with git.

Key safety model: the EOA can **vote but never transfer** — delegation isn't
custody. Worst case on key compromise: bad votes until re-delegation.

## Repo map

```
constitution.md      the product — versioned, forkable, CC0
PRD.md               spec + status + roadmap
bot/                 the agent: poller, evaluator, executor, telegram, publisher
docs/                the site (GitHub Pages): pages, verdicts.json, amendments.json
backtests/           agreement/divergence reports per constitution version
data/seed.db         paper-era verdict history for fresh deploys
notes/               gitignored — private strategy
```
