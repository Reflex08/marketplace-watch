# marketplace-watch

Polls Facebook Marketplace every 15 min for matching listings, Telegrams you each new one with a
tap-to-copy trade pitch. You send the message yourself from your real account.

Runs free on GitHub Actions cron. No server, no browser, no logged-in session.

## Setup

1. **Search API key** — sign up at https://scrapecreators.com/facebookMarketplace-search-api.
   A third party does the scraping, so your Facebook account never touches automation.
2. **Telegram bot** — message `@BotFather` → `/newbot` → copy the token. Message your new bot once,
   then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and grab the numeric `chat.id`.
3. **Push this repo to GitHub**, then Settings → Secrets and variables → Actions, add:
   `SCRAPECREATORS_KEY`, `TG_TOKEN`, `TG_CHAT`.
4. **Tune the search** in `.github/workflows/watch.yml` — `QUERY`, `MAX_PRICE`, `PITCH`.
   Coords default to North York, Toronto with a 60 km radius.
5. **Run once manually** (Actions tab → marketplace-watch → Run workflow). First run seeds
   `seen.json` and sends nothing; every run after alerts only on new listings.

Keep the repo **public**. Private repos cap Actions at 2,000 min/month and 15-min polling burns
~1,400 of them. Public is unlimited. The `seen.json` commit each run also keeps the schedule alive —
GitHub disables cron on repos idle 60 days.

## Local run

```sh
pip install requests
python watch.py --selftest          # logic check, no network
SCRAPECREATORS_KEY=... TG_TOKEN=... TG_CHAT=... python watch.py
```

## Why it doesn't auto-send

Meta's spam policy names "repetitive or unsolicited contact". Identical text at machine cadence
across many one-sided threads is the exact signature — soft block within a day or two, permanent ban
on repeat. Sellers also filter obvious bot copy-paste. Hand-sending from a ping converts better and
costs you ten seconds. For more volume, add search queries or widen the radius — not auto-send.
