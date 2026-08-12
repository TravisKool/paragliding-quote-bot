# Paragliding Quote Bot

A daily-run Python app that pulls curated quotes from a paragliding masterclass
book, has Claude write a short piece of context for each one, pairs it with a
relevant paragliding photo, and posts it to a dedicated Instagram account.

One post per day, no repeats, no manual intervention once live. Runs on GitHub
Actions cron — no paid hosting.

## Status

Phase 0 (repo bootstrap) is done. The pipeline modules under `src/` are
documented stubs; each carries a docstring naming its phase and intended
surface. See [Build phases](#build-phases).

## How it works

```
select_quote      unused quote, highest quality_score
      |
generate_post     Claude: caption + context + hashtags + alt text
      |
pick_image        theme match against images/library/manifest.json
      |
publish_image_host  -> public raw.githubusercontent.com URL
      |
instagram_client  Graph API: create media container -> publish
      |
db                write posts row + stamp quotes.used_at (one transaction)
```

A quote is only marked used after a successful publish, so a failed run never
burns one.

## Stack

| Concern        | Choice                                                    |
| -------------- | --------------------------------------------------------- |
| Language       | Python 3.11+                                              |
| AI             | Anthropic API (`claude-opus-5` by default), `anthropic` SDK |
| Database       | SQLite via Turso — same DB locally and in CI, no sync step |
| PDF parsing    | `pdfplumber` (seed step only)                             |
| Image hosting  | `raw.githubusercontent.com` serving `/images` from this repo |
| Publishing     | Meta Graph API via plain `requests` (two endpoints)       |
| Scheduling     | GitHub Actions `schedule`                                 |
| Tests          | `pytest`, lint with `ruff`                                |

## Prerequisites — things only you can do

These need a human with account access. Nothing in this repo can script around
them, and the daily Action cannot run until they're all done.

1. **Meta developer app** — create at [developers.facebook.com](https://developers.facebook.com/),
   add the Instagram Graph API product.
2. **Instagram account** — convert the target account to Professional (Business
   or Creator) and link it to a Facebook Page.
3. **Long-lived access token** — generate through the standard OAuth flow, store
   as the `IG_ACCESS_TOKEN` secret. It expires in ~60 days;
   `scripts/refresh_ig_token.py` will rotate it once implemented, but the
   *first* token has to be obtained by hand.
4. **Turso database** — create a free DB at [turso.tech](https://turso.tech),
   store the URL and auth token as `TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN`.
5. **Anthropic API key** — from [console.anthropic.com](https://console.anthropic.com/),
   store as `ANTHROPIC_API_KEY`.
6. **Image hosting** — the default is `raw.githubusercontent.com`, which needs
   no setup. If you'd rather use GitHub Pages, enable it under Settings → Pages
   and change `IMAGE_BASE_URL` accordingly.
7. **Source PDF** — drop the book at `book/source.pdf`. PDFs in `book/` are
   gitignored by default; see [Open decisions](#open-decisions).
8. **Photo library** — add ~15-20 tagged paragliding photos under
   `images/library/` with entries in `manifest.json`.
9. **Failure notifications** — confirm GitHub email notifications are on for
   this repo, since a failed daily run surfaces only as a workflow-failure email.

### Secrets

Repo → Settings → Secrets and variables → Actions:

| Secret               | Used by                        |
| -------------------- | ------------------------------ |
| `ANTHROPIC_API_KEY`  | caption generation, seeding    |
| `TURSO_DATABASE_URL` | all DB access                  |
| `TURSO_AUTH_TOKEN`   | all DB access                  |
| `IG_USER_ID`         | Graph API publish              |
| `IG_ACCESS_TOKEN`    | Graph API publish              |
| `META_APP_ID`        | token refresh                  |
| `META_APP_SECRET`    | token refresh                  |
| `GH_PAT`             | token refresh (writes the secret back) |

`IMAGE_BASE_URL` is not secret — set it as a repository **variable**.

## Local setup

```bash
git clone <this repo> && cd paragliding-quote-bot
python -m venv .venv && .venv\Scripts\activate   # PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env                            # then fill it in
pytest
```

Leave `TURSO_DATABASE_URL` blank locally and the code falls back to a SQLite
file at `LOCAL_DB_PATH`. The test suite needs no credentials at all.

## Running

```bash
python -m src.seed_quotes book/source.pdf   # one-off: build the quote pool
python -m src.init_launch --dry-run         # review the 8 launch posts
python -m src.init_launch                   # publish them for real
python -m src.main                          # one daily post
```

`DRY_RUN=1` skips the Graph API publish while running everything else.

## The daily Action

`.github/workflows/daily-post.yml` runs `python -m src.main` on a cron. **The
schedule is commented out** until the pipeline is implemented and a dry run has
been reviewed — until then the workflow is `workflow_dispatch` only, so you can
trigger it by hand from the Actions tab. Uncomment the `schedule:` block to go
live, and pick a fixed UTC time (cron has no timezone; account for DST yourself
if you care about a particular local slot).

`.github/workflows/ci.yml` runs `ruff` and `pytest` on every push and PR.

One run a day is trivial against the Actions free-minute allowance.

## Build phases

- [x] **0 — Bootstrap.** Structure, config, `.env.example`, `.gitignore`, CI.
- [ ] **1 — Database.** `db.py` connection + idempotent schema, init CLI, tests.
- [ ] **2 — Quote extraction.** `seed_quotes.py`: PDF → Claude → scored pool.
- [ ] **3 — Image library.** `manifest.json` schema, `pick_image.py` + fallbacks.
- [ ] **4 — Caption generation.** `generate_post.py`.
- [ ] **5 — Image hosting.** `publish_image_host.py`.
- [ ] **6 — Publishing.** `instagram_client.py`, token refresh + its Action.
- [ ] **7 — Orchestration.** `main.py`, structured logging, loud failures.
- [ ] **8 — Actions.** Enable the daily cron, add the token-refresh schedule.
- [ ] **9 — Launch script.** `init_launch.py` with `--dry-run`.
- [ ] **10 — Polish.** Alt text, full runbook, final visibility/gitignore check.

## Open decisions

Defaults chosen for Phase 0 — say the word and any of these change:

- **Image hosting: `raw.githubusercontent.com`.** Simplest, no Pages build step.
- **Repo visibility: private.** Recommended given the book content.
- **Source PDF: gitignored.** `book/*.pdf` is excluded, so the book stays local
  and is supplied out-of-band. If you'd rather commit it, remove that line from
  `.gitignore` — but only on a private repo.
- **Posting time: not yet fixed.** The commented cron suggests 16:00 UTC.

## Runbook

To be completed in Phase 10: re-seeding a low quote pool, adding photos,
rotating credentials, pausing the daily Action.
