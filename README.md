# marketplace-watch

Hunts Facebook Marketplace hourly for electric dirt bikes (Sur-Ron / Talaria / E Ride Pro class) whose
sellers look open to a trade, and Telegrams them to you with a tap-to-copy pitch. You send the
message yourself from your own account.

Runs free on GitHub Actions cron. No server, no browser, no Facebook login anywhere in the loop —
the scraping happens on ScrapeCreators' infrastructure, so your account is never involved.

## How it decides

1. **Search** every brand in `QUERIES` (1 credit each). Results rotate and any single query can
   return 0, so several queries run every time and get deduped by listing id.
2. **Title filter.** Must match a brand or model in `REQUIRE`, must not match a part word in
   `EXCLUDE`. This runs before any paid detail call. The allowlist exists because the API
   keyword-matches loosely — `ventus` otherwise returns golf shafts and `rawrr` returns sweaters.
3. **Read the description** for survivors (1 credit each) to judge trade intent.
4. **Drop refusers** — anything saying "no trades", "cash only", etc. (`NO_TRADE`).
5. **Rank.** Sellers saying "trades welcome", "swap", "trade for" (`TRADE_OK`) are flagged 🔥 and
   sent first. Everyone else follows.

Junk-word matching is title-only on purpose: real listings mention batteries and tires in their
descriptions constantly, so judging on description would drop the genuine bikes and keep the parts.

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

Everything is an env var in `.github/workflows/watch.yml` — `QUERIES`, `REQUIRE`, `EXCLUDE`,
`NO_TRADE`, `TRADE_OK`, `MAX_PRICE`, `RADIUS_KM`, `PITCH`. Add brands to `QUERIES` *and* `REQUIRE`
together, or the allowlist will reject them.

Cost is roughly 10 credits per run for searches plus 1 per new candidate, so ~250-300/day hourly.
Halve it with `cron: "0 */2 * * *"`.

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
