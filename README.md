# marketplace-watch

Hunts Facebook Marketplace hourly for electric dirt bikes (Sur-Ron / Talaria / E Ride Pro class) whose
sellers look open to a trade, and Telegrams them to you with a tap-to-copy pitch. You send the
message yourself from your own account.

Runs free on GitHub Actions cron. No server, no browser, no Facebook login anywhere in the loop —
the scraping happens on ScrapeCreators' infrastructure, so your account is never involved.

## How it decides

1. **Search** `QUERIES_PER_RUN` of the brands in `QUERIES`, striding through the list so each run
   samples a spread and every query comes round every few hours. 1 credit each. A query that fails
   is logged and skipped; the run only dies if *all* of them fail.
2. **Free title pass** (`shortlist`). Must match a brand or model in `REQUIRE`, must not match a
   part word in `EXCLUDE`, must not already say "no trades"/"cash only" in the title. No credits
   spent here — typically 80% of results die at this step. Survivors are ordered so titles already
   advertising a trade go first.
3. **Cap at `MAX_ALERTS`.** The rest are *held*, not dropped: they stay unseen and arrive on later
   runs. This is what keeps the standing inventory arriving as a trickle instead of 50 at once.
4. **Read the description** for that batch only (1 credit each) to judge trade intent.
5. **Drop refusers**, then **rank** — sellers saying "trades welcome", "swap", "trade for"
   (`TRADE_OK`) are flagged 🔥 and sent first.

Two deliberate asymmetries:

- Junk matching is **title-only**. Real listings mention batteries and tires in their descriptions
  constantly, so judging on description would drop the genuine bikes and keep the parts.
- `NO_TRADE` is checked **before** `TRADE_OK`, because "no trades" contains "trade" and would
  otherwise be promoted instead of dropped.

Allowlist entries are qualified wherever the bare word is a common noun — `strike shadow` not
`strike`, `onyx rcr` not `onyx` — and `mantis` was removed outright because it matches Arc'teryx
packs and a Marvel action figure.

A listing is recorded as seen only after its alert is actually delivered, so a crash or rate-limit
mid-run re-alerts the stragglers next time rather than burying them. If a run fails outright it
sends you a ⚠️ message and turns the Actions job red.

## Setup

1. **Search API key** — sign up at https://scrapecreators.com.
2. **Telegram bot** — `@BotFather` → `/newbot` → token. Message the bot once, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and grab the numeric `chat.id`.
3. **Push to GitHub**, then Settings → Secrets and variables → Actions:
   `SCRAPECREATORS_KEY`, `TG_TOKEN`, `TG_CHAT`.
4. **Run once manually** (Actions tab → Run workflow). The first run seeds `seen.json` and sends
   nothing; alerts start on the next run.

Keep the repo **public** — private repos cap Actions minutes and hourly polling would eat them.
The `seen.json` commit each run also keeps the schedule alive; GitHub disables cron on repos idle
60 days.

## Tuning

Everything is an env var in `.github/workflows/watch.yml` — `QUERIES`, `QUERIES_PER_RUN`,
`MAX_ALERTS`, `REQUIRE`, `EXCLUDE`, `NO_TRADE`, `TRADE_OK`, `MAX_PRICE`, `RADIUS_KM`, `SEND_DELAY`,
`PITCH`. Add brands to `QUERIES` *and* `REQUIRE` together, or the allowlist will reject its own
results.

Cost per run is `QUERIES_PER_RUN` credits plus at most `MAX_ALERTS` — 3 + up to 6 on the defaults,
so ~72/day steady-state once the standing inventory has been worked through. Levers, cheapest first:
raise the cron interval, lower `QUERIES_PER_RUN`, lower `MAX_ALERTS`.

`altis motors`, `rawrr` and `ventus` are intentionally not in `QUERIES` — over several live runs they
returned only sneakers, hockey cards and golf shafts. The brands remain in `REQUIRE`, so a genuine
one still gets caught via the other queries. Re-add them if you want the direct coverage.

## Local run

```sh
pip install requests
python watch.py --selftest          # logic check, no network, no credits
SCRAPECREATORS_KEY=... TG_TOKEN=... TG_CHAT=... python watch.py
```

## Why it doesn't auto-send

Meta's spam policy names "repetitive or unsolicited contact". Identical text at machine cadence
across many one-sided threads is the exact signature — soft block within a day or two, permanent
ban on repeat. Sellers also filter obvious bot copy-paste. Hand-sending from a ping converts better
and costs you ten seconds.
