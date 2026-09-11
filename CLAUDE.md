# Dispatch — CLAUDE.md

Start here. For full narrative context (what's changed, what's been tried and failed, next steps), read **[handoff.md](handoff.md)**. For detailed technical rationale on the data pipeline, read **[README.md](README.md)**.

## What this is

A live news globe/map plotting real geolocated conflict, climate, and political events, refreshed automatically. Live at https://glnewsmapping.github.io/Global-News-Daily/. Accuracy and honesty matter more than volume or polish here — always verify claims empirically (live API checks, real data samples) rather than assuming.

## Critical gotchas — don't relearn these the hard way

- **In `fetch_news_data.py`, conflict/political relevance filtering happens on a *larger raw pool*, before capping to the display target — never after.** Filtering an already-capped ~33-story set drops it to single digits. See `enrich_with_real_headlines()` and how `main()` calls it (fetches 2x the target, filters, then balances/caps).
- **Don't use `opacity: 0` for invisible-but-interactive WebGL meshes** (globe points/polygons in `index.html`). Transparent materials force expensive per-frame depth-sorting (~35ms/frame measured). Use `colorWrite: false` on an opaque material instead — same invisible result, no cost, raycasting/hover unaffected.
- **GDELT's DOC 2.0 search API is rate-limited from every network tested, including GitHub Actions' own runners.** Don't try to fix this with retries/backoff — conflict and political both correctly use GDELT's raw bulk event files instead (`data.gdeltproject.org/gdeltv2/*.export.CSV.zip`, no rate limit).
- **Verify domain availability live (RDAP), never assume.** `dispatchworld.com` was assumed available from earlier discussion; it's actually been registered since 2002. `dispatchsphere.com` is the verified-available recommendation.

## Before committing/pushing

Always confirm with the user before `git push` — this deploys to a live public site. When changing the data pipeline, regenerate `data/news_data.json` (`python fetch_news_data.py`) and spot-check real output before committing.
