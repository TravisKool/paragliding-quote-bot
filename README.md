# Paragliding Quote Bot

A daily-run Python app that pulls curated quotes from a paragliding masterclass
book, has Claude write a short piece of context for each one, pairs it with an
image, and posts it to a dedicated Instagram account.

The image is a photo from `images/library/` when there is one, and a generated
typography card when there is not — so an empty photo library is a normal
state, not a blocker.

One post per day, no repeats, no manual intervention once live. Runs on GitHub
Actions cron — no paid hosting.

## Status

All pipeline code is written and tested (180 tests, no live credentials
required). **Nothing is live yet** — the daily schedule is deliberately
commented out, and the bot cannot run until the accounts in
[Prerequisites](#prerequisites--things-only-you-can-do) exist and the quote pool
has been seeded from the book.

## How it works

```
select_quote        best unused quote (by quality_score)
      |
pick_image          theme match against images/library/manifest.json
      |
generate_post       Claude: context paragraph + hashtags, then alt text
      |
publish_image_host  commit if needed -> public raw.githubusercontent URL
      |
instagram_client    Graph API: create container -> wait -> publish
      |
db                  posts row + quotes.used_at, in one transaction
```

Image selection runs before caption generation so alt text can be written from
the actual photo rather than invented.

### Design invariants

These are the things most likely to be broken by a well-meaning later change,
so each has a test guarding it:

- **A quote is only marked used after a successful publish.** A failed run
  records the attempt and leaves the quote in the pool for tomorrow.
- **The posts row and `used_at` are written in one transaction.** A partial
  write would either burn a quote with nothing published, or leave a published
  quote eligible to post again.
- **A quote Claude declines to caption is skipped, not retried forever.**
  `main.py` walks down three candidates before failing the run; otherwise one
  awkward quote would wedge the schedule permanently.
- **The quote text is never truncated.** When a caption is too long, hashtags
  are dropped first, then the context paragraph. A half-quote misrepresents the
  author.
- **Any unrecoverable failure exits non-zero.** GitHub's workflow-failure email
  is the only alerting this system has.

## Stack

| Concern        | Choice                                                      |
| -------------- | ----------------------------------------------------------- |
| Language       | Python 3.11+                                                |
| AI             | Anthropic API (`claude-opus-5`), adaptive thinking, structured outputs |
| Database       | SQLite via Turso — same DB locally and in CI, no sync step   |
| PDF parsing    | `pdfplumber` (seed step only)                               |
| Quote cards    | `Pillow` — typeset quote over a theme-tinted gradient        |
| Image hosting  | `raw.githubusercontent.com` serving `/images` from this repo |
| Publishing     | Meta Graph API via `requests` (three calls)                 |
| Scheduling     | GitHub Actions `schedule`                                   |
| Tests / lint   | `pytest`, `ruff`                                            |

## Prerequisites — things only you can do

These need a human with account access. Nothing in this repo can script around
them.

1. **Meta developer app** — create at [developers.facebook.com](https://developers.facebook.com/),
   add the Instagram Graph API product.
2. **Instagram account** — convert the target account to Professional (Business
   or Creator) and link it to a Facebook Page.
3. **Long-lived access token** — generate through the OAuth flow, store as the
   `IG_ACCESS_TOKEN` secret. It expires in ~60 days; the refresh workflow
   rotates it, but the *first* token must be obtained by hand.
4. **Turso database** — create a free DB at [turso.tech](https://turso.tech),
   store the URL and auth token as secrets.
5. **Anthropic API key** — from [console.anthropic.com](https://console.anthropic.com/).
6. **Repo-scoped PAT** — for the token-refresh workflow, stored as `GH_PAT`.
   The default `GITHUB_TOKEN` cannot write secrets.
7. **Source PDF** — drop the book at `book/source.pdf`. Gitignored by default.
8. **Photo library** — *optional*. With `images/library/` empty the bot posts
   generated quote cards. To use photos instead, add ~15-20 to
   `images/library/` and list them in `manifest.json`.
9. **Email notifications** — confirm they're enabled for this repo. A failed
   daily run surfaces only as a workflow-failure email.

### Secrets and variables

Repo → Settings → Secrets and variables → Actions:

| Secret               | Used by                        |
| -------------------- | ------------------------------ |
| `ANTHROPIC_API_KEY`  | caption generation, seeding    |
| `TURSO_DATABASE_URL` | all DB access                  |
| `TURSO_AUTH_TOKEN`   | all DB access                  |
| `IG_USER_ID`         | Graph API publish              |
| `IG_ACCESS_TOKEN`    | Graph API publish, refreshed monthly |
| `META_APP_ID`        | token refresh                  |
| `META_APP_SECRET`    | token refresh                  |
| `GH_PAT`             | token refresh (writes the secret back) |

`IMAGE_BASE_URL` is not secret — set it as a repository **variable**:

```
https://raw.githubusercontent.com/TravisKool/paragliding-quote-bot/main/images
```

## Local setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env      # then fill it in
pytest
```

Leave `TURSO_DATABASE_URL` blank locally and everything falls back to a SQLite
file at `LOCAL_DB_PATH`. The test suite needs no credentials at all.

## Images without a photo library

With `images/library/` empty, each post gets a generated card: the quote
typeset over a gradient tinted by its theme, 1080×1080. Look at one before
deciding whether you like it:

```powershell
python -m src.make_card --preview "Fear is information." --theme fear --attribution "Author, Title"
```

Cards are rendered per post and named after a hash of the quote, so a retried
run overwrites its own card rather than accumulating near-duplicates. They are
never reused for a different quote — the quote is baked into the picture, so
serving an old card with a new caption would publish a post that contradicts
itself. That is why `pick_image` does not treat `images/generated/` as a
fallback pool.

Alt text for a card is the quote itself, which is both the correct description
and one fewer vision call.

**Fonts.** The renderer prefers a `.ttf` dropped into `assets/fonts/`, then
system fonts, then the DejaVu copy bundled inside Pillow. Committing a font is
the only way to guarantee the Ubuntu runner produces the same card your machine
does — worth doing once you settle on a look.

Switch behaviour with the `IMAGE_MODE` variable: `auto` (default), `card` to
use cards even when photos exist, `photo` to fail the run rather than post a
card.

## Going live

In order:

```powershell
# 1. Create the schema
python -m src.db init

# 2. Build the quote pool from the book (one-off, costs API tokens)
python -m src.seed_quotes book/source.pdf --author "Author Name"

#    Read the printed score distribution. Spot-check the top quotes before
#    continuing — everything downstream assumes this pool is good.

# 3. Review a single post end to end without publishing
python -m src.main --dry-run

# 4. Review the whole launch batch
python -m src.init_launch --dry-run

# 5. Publish the 8 launch posts for real
python -m src.init_launch

# 6. Enable the daily schedule: uncomment the `schedule:` block in
#    .github/workflows/daily-post.yml and push.
```

`DRY_RUN=1` in the environment forces dry-run mode everywhere.

## Commands

| Command | What it does |
| ------- | ------------ |
| `python -m src.db init` | Create the schema (idempotent) |
| `python -m src.seed_quotes book/source.pdf` | Build the quote pool. `--dry-run`, `--limit-pages N`, `--author`, `--book-title` |
| `python -m src.make_card --preview "..."` | Render a quote card to `images/generated/` to look at. `--theme`, `--attribution` |
| `python -m src.main` | Publish one post. `--dry-run` |
| `python -m src.init_launch` | Publish the launch batch. `--dry-run`, `--count N`, `--delay S` |
| `python scripts/refresh_ig_token.py` | Rotate the IG token. `--dry-run` |
| `pytest` / `ruff check .` | Tests and lint |

## Workflows

| Workflow | Trigger | Notes |
| -------- | ------- | ----- |
| `ci.yml` | push / PR | ruff + pytest |
| `daily-post.yml` | manual only | **Cron is commented out.** Uncomment to go live |
| `refresh-token.yml` | monthly + manual | Rotates `IG_ACCESS_TOKEN` |

The daily cron is off by default on purpose: enabling it before the pool is
seeded means a failure email every night, which trains you to ignore exactly
the alerts this system depends on. One run a day is trivial against the Actions
free-minute allowance.

## Runbook

**The quote pool is running low.** `select_quote` warns when 14 or fewer
unused quotes remain (~2 weeks). Re-run the seed step — already-present quotes
are skipped, so it's safe to re-run against the same book:

```powershell
python -m src.seed_quotes book/source.pdf --author "Author Name"
```

**Adding photos.** Drop files in `images/library/`, add an entry to
`manifest.json` (see `_entry_schema` in that file), commit and push. Include
`alt_text` where you can — a human description beats a generated one, and it
skips an API call. Untagged images still get used as random fallbacks. The
first photo added takes over from generated cards automatically; no config
change needed.

**Generated cards are piling up in the repo.** Each post commits its card so
`raw.githubusercontent.com` can serve it. They are small (~100-200KB) and named
by quote hash, so a year of daily posts is well under 100MB. If it ever
matters, deleting old cards from `images/generated/` is safe — published posts
keep their image on Instagram's CDN, not from this repo.

**The daily run failed.** Check the Actions log. The quote was not consumed, so
the next run retries it. Common causes:

- *Graph API code 190 / 102* — the token expired. Run the refresh workflow; if
  that fails too, redo the OAuth flow by hand and update `IG_ACCESS_TOKEN`.
- *"did not become reachable"* — the image URL 404s. Check `IMAGE_BASE_URL`
  matches this repo and branch, and that the image is actually pushed.
- *"Container ... ended in state ERROR"* — Meta could not fetch or accept the
  image. Check the file is a valid JPEG/PNG under 8MB.
- *`QuotePoolEmpty`* — re-seed.

**Pausing posting.** Comment out the `schedule:` block in `daily-post.yml` and
push. Leave the token-refresh workflow running, or the token will expire while
posting is paused and need a manual OAuth flow to recover.

**Rotating credentials.** Anthropic and Turso keys: update the repo secret,
nothing else needed. Instagram token: run the refresh workflow, or update
`IG_ACCESS_TOKEN` by hand.

**Deleting a bad post.** Delete it in the Instagram app. The `posts` row stays
as an audit record and the quote stays marked used — to allow reposting, clear
`used_at` for that quote id.

## Repo layout

```
src/
  config.py              env loading, limits, repo paths
  db.py                  Turso/SQLite backends + query helpers
  seed_quotes.py         PDF -> Claude -> scored quote pool
  select_quote.py        candidate selection, low-pool warning
  pick_image.py          theme match against the photo library
  make_card.py           typeset a quote onto a generated background
  generate_post.py       caption assembly, hashtags, alt text
  publish_image_host.py  public URL, commit-if-needed, reachability
  instagram_client.py    Graph API container -> wait -> publish
  main.py                daily orchestration
  init_launch.py         go-live batch
scripts/refresh_ig_token.py
tests/                   180 tests, no credentials needed
```

## Open decisions

Defaults chosen during the build — say the word and any of these change:

- **Image hosting: `raw.githubusercontent.com`.** Simplest, no Pages build step.
- **Source PDF: gitignored.** Remove `book/*.pdf` from `.gitignore` to commit it
  (this repo is private).
- **Images default to generated cards.** `IMAGE_MODE=auto` uses the photo
  library when it has something and cards when it does not. Set `card` to
  always use cards even with photos present, or `photo` to fail the run rather
  than post a card.
- **Posting time: 16:00 UTC**, in the commented cron. Cron has no timezone, so
  a fixed local time across DST would need two schedules.
- **Refusal fallbacks are not enabled.** Claude Opus 5 can decline a request;
  rather than re-serving it on a fallback model, the bot skips that quote and
  uses the next one — for this use case a quote Claude won't caption is
  probably a poor fit for the account anyway. Say the word if you'd prefer the
  server-side `fallbacks` parameter instead.
