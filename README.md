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

`site/` is a dependency-free static site (GitHub Pages-ready), styled on nouns.wtf's
own design tokens (Londrina Solid / PT Root UI, cool `#d5d7e1` / warm `#e1d7d5`):

- **[index](site/index.html)** — what a standing vote is and how it works
- **[constitution](site/constitution.html)** — the constitution, clause by clause
- **[faction](site/faction.html)** — The Dead Nouns Faction: the captured bloc's
  playbook, receipts, and the euphemism watchlist — published so Plan A/Plan B
  proposals arrive pre-labeled

Open locally with `open site/index.html`, or serve via `python3 -m http.server -d site`.

**Delegate:** if you've read the constitution and want your Noun to vote by it,
the site will have a one-click `delegate()` button — no custody, reversible
any time. It ships once there's a voting record to judge us by.
