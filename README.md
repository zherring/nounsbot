# Constitution Forge

A personal governance agent for Nouns DAO. The constitution is the product; the
agent is plumbing.

- **What it does:** evaluates every Nouns proposal against a written, git-versioned
  constitution; the owner ratifies via Telegram; votes are cast onchain with
  clause-cited reasoning; the record is public.
- **Why:** governance participation is bottlenecked on attention, not voting power.
  Lowering the cost of expressing a consistent position converts dormant votes into
  standing ones.

See [PRD.md](PRD.md) for the full spec. [constitution.md](constitution.md) is the
document everything else serves (v0.1 draft, unratified).

## The site

`docs/` is a dependency-free static site served by GitHub Pages, styled on nouns.wtf's
own design tokens (Londrina Solid / PT Root UI, cool `#d5d7e1` / warm `#e1d7d5`):

- **[index](docs/index.html)** — what a standing vote is and how it works
- **[constitution](docs/constitution.html)** — the constitution, clause by clause
- **[faction](docs/faction.html)** — The Dead Nouns Faction: the captured bloc's
  playbook, receipts, and the euphemism watchlist — published so Plan A/Plan B
  proposals arrive pre-labeled

Open locally with `open docs/index.html`, or serve via `python3 -m http.server -d docs`.

## The bot (M0 — paper agent)

`bot/` is the M0 runtime: event-driven ingest from the Nouns subgraph, constitution
evaluation via Claude, verdicts to stdout/Telegram. No keys, no casting — the human
votes by hand while the verdict quality proves itself.

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # add ANTHROPIC_API_KEY

.venv/bin/python -m bot.backtest --last 20 --dry-run   # cost estimate, no API calls
.venv/bin/python -m bot.backtest --last 20             # evaluate history vs constitution
.venv/bin/python -m bot.poller                         # watch live props
```

Backtest reports land in `backtests/` — agreement vs. divergence against what the
DAO actually did, with clause-cited reasoning per prop.

**Delegate:** if you've read the constitution and want your Noun to vote by it,
the site will have a one-click `delegate()` button — no custody, reversible
any time. It ships once there's a voting record to judge us by.
