"""Watch Facebook Marketplace for e-bikes whose sellers look open to a trade, and Telegram them to me.

Ranking, not just filtering: sellers who say they'll trade go out first and flagged,
sellers who say cash-only or no-trades are dropped entirely.
"""
import datetime
import html
import json
import os
import pathlib
import re
import sys
import time

import requests

SEARCH_API = "https://api.scrapecreators.com/v1/facebook/marketplace/search"
ITEM_API = "https://api.scrapecreators.com/v1/facebook/marketplace/item"
SEEN = pathlib.Path(__file__).with_name("seen.json")
# Vetted listings not yet alerted. Persisted because the cap defers more than it
# sends, and a deferred listing found on page 3 won't reappear on tomorrow's page 1.
PENDING = pathlib.Path(__file__).with_name("pending.json")
# Written by `watch.py --feedback` from your Telegram replies. Overrides the env
# defaults below, so every setting is adjustable by talking to the bot.
RULES = pathlib.Path(__file__).with_name("rules.json")
# Telegram cursor + message_id -> listing, so a reply can be tied to its alert.
STATE = pathlib.Path(__file__).with_name("state.json")


def load_json(path, fallback):
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text())
    except ValueError:
        print(f"{path.name} unreadable, starting fresh")
        return fallback


_RULES = load_json(RULES, {})


def tuned(name, base):
    """A list from env, minus anything you've blocked, plus anything you've added."""
    blocked = [w.lower() for w in _RULES.get(f"{name}_remove", [])]
    out = [w for w in base if not any(b in w.lower() for b in blocked)]
    return out + [w for w in _RULES.get(f"{name}_add", []) if w not in out]


def tuned_num(name, base):
    got = _RULES.get(name)
    return type(base)(got) if got is not None else base


# Each query returns a different rotating slice, so several widen coverage a lot.
QUERIES = tuned("queries", [
    q.strip()
    for q in os.environ.get(
        "QUERIES",
        # "altis motors", "rawrr" and "ventus" are deliberately absent: across several
        # live runs they returned only sneakers, hockey cards and golf shafts. Those
        # brands stay in REQUIRE, so a real one still gets caught by the other queries.
        "surron,talaria,e ride pro,79bike,rfn apollo,segway dirt bike,"
        "electric dirt bike",
    ).split(",")
    if q.strip()
])
LAT = os.environ.get("LAT", "43.7615")   # North York, Toronto
LNG = os.environ.get("LNG", "-79.4111")
RADIUS_KM = tuned_num("radius_km", float(os.environ.get("RADIUS_KM", "200")))
MAX_PRICE = tuned_num("max_price", float(os.environ.get("MAX_PRICE", "15000")))
# Sur-Ron / Talaria / E Ride Pro class starts around CA$3.5k used. Anything under this
# is a scooter, a kids' toy or a fat-tire commuter wearing "dirt bike" in its title.
# The API's own min_price leaks items well below it, so it is re-checked client-side.
MIN_PRICE = tuned_num("min_price", float(os.environ.get("MIN_PRICE", "2500")))
# No pitch text: alerts carry the link and the trade status only. Writing the message
# yourself, per seller, is also what keeps it from looking like a bot.

# Brand/model allowlist. The API keyword-matches loosely, so "ventus" returns golf
# shafts and "rawrr" returns sweaters — an allowlist kills that permanently where a
# blocklist would be endless whack-a-mole. Title must hit one of these.
REQUIRE = [
    w.strip().lower()
    for w in os.environ.get(
        "REQUIRE",
        # the brands you named. Bare "apollo" is out — it matched a Rick Riordan novel
        # and Apollo's gas RFZ line; "rfn" is the electric one and is specific enough.
        "surron,sur-ron,sur ron,talaria,e ride pro,eride pro,e-ride pro,eride,"
        "altis motors,rawrr,79bike,79 bike,ventus,rfn,"
        # Segway sells far more scooters than dirt bikes, so only the dirt models.
        "segway x1,segway x2,segway dirt,"
        # their models. Bare "mantis" is out: it matches Arc'teryx packs and a Marvel
        # figure. Bare "3x" matched clothing sized 3XL. Bare "mx3" matched "Rumi Mx350",
        # and Talaria's MX models are already covered by "talaria".
        "light bee,lightbee,ultra bee,storm bee,lbx,falcon,warthog,komodo,"
        # comparable premium makes only. Deliberately NOT here: razor mx, ridstar,
        # valtinsu, rooder, kukirin, weped, heybike, emmo, gt54, zoombike, phatmoto,
        # sozo, m2s, ebroh — those are scooters, kids' toys and fat-tire commuters.
        # "electric motion" is out despite being a real premium brand: it matched
        # "2023 Electric Motion Sport", a red Chinese e-street-bike. Their model name
        # is the precise token.
        "kuberg,onyx rcr,volcon,stark varg,delfast,dust moto,kalk,epure,"
        "stealth electric,tromox,bultaco,strike shadow,shadow sx,gt73,evoque,"
        "caofen,lynx,"
        # generic phrasing, for makes not listed above. Bare "dirt bike" and "pit bike"
        # are deliberately absent — they let gas bikes and CA$300 toys straight through.
        "electric dirt bike,electric dirtbike,e dirt bike,dirt ebike,dirt e-bike,"
        "electric motocross,electric enduro,electric pit bike,emx",
    ).split(",")
    if w.strip()
]

# Unambiguous "this is a part, not a bike" phrases only. Kept deliberately short —
# REQUIRE does the heavy lifting, and a broad blocklist would drop real listings
# like "Surron Light Bee X + spare battery".
EXCLUDE = [
    w.strip().lower()
    for w in os.environ.get(
        "EXCLUDE",
        "for parts,parts only,parts,battery only,charger,key switch,switch,"
        "sprocket,fender,decal,sticker,kickstand,helmet,cover,seat,shock,fork,"
        "plastics only,shaft,sweater,for rent,rental,"
        # gas machines — they reach the search via generic phrasing
        "cc ,50cc,70cc,110cc,125cc,140cc,150cc,160cc,200cc,250cc,450cc,"
        "2 stroke,4 stroke,two stroke,four stroke,gas powered,gasoline,petrol,"
        "yamaha,honda,kawasaki,suzuki,ktm,husqvarna,gas dirt,pit pro,"
        # scooters and commuters that keep turning up under dirt-bike phrasing
        "scooter,e-scooter,escooter,moped,fat tire,fat bike,pedal assist,mountain bike,"
        # street/sport bikes keep arriving under electric-moto phrasing
        "sport bike,sportbike,street bike,streetbike,sportster,cruiser,ninja",
    ).split(",")
    if w.strip()
]

# Seller has ruled a trade out — don't waste a message on them.
NO_TRADE = [
    p.strip().lower()
    for p in os.environ.get(
        "NO_TRADE",
        "no trades,no trade,not trade,not trading,no swap,no swaps,no trades please,"
        "cash only,cash on pickup only,cash and pickup only,not interested in trade,"
        "cash in hand,cash deal only,no trade offers,trades not accepted",
    ).split(",")
    if p.strip()
]

# Commercial sellers. Hard skip, checked before the trade rules — a dealer that takes
# trade-ins is still a dealer. Title and description both, since they advertise in both.
DEALER = [
    p.strip().lower()
    for p in os.environ.get(
        "DEALER",
        "financing,finance available,no credit check,lease to own,plus tax,plus taxes,"
        "+tax,+ tax,plus hst,hst extra,taxes extra,tax included,in stock,we carry,"
        "call us,text us for,visit us,our showroom,showroom,dealer,dealership,omvic,"
        "msrp,extended warranty,demo model,demonstration model,wholesale,distributor,"
        "authorized,delivery available,financing available,shop now,other models",
    ).split(",")
    if p.strip()
]

# Seller has invited a trade — send these first.
TRADE_OK = [
    p.strip().lower()
    for p in os.environ.get(
        "TRADE_OK",
        "accepting trades,trades welcome,trade welcome,trades accepted,open to trade,"
        "open to trades,willing to trade,will trade,would trade,trade for,trades ok,"
        "trade ok,interested in trade,open to swap,swap,partial trade,trade in",
    ).split(",")
    if p.strip()
]

SEND_DELAY = float(os.environ.get("SEND_DELAY", "1.2"))  # Telegram allows ~1 msg/sec per chat

# Queries that run every single time, no matter the rotation — the brands worth
# knowing about within the hour rather than within three.
PRIORITY_QUERIES = [
    q.strip()
    for q in os.environ.get("PRIORITY_QUERIES", "surron,talaria").split(",")
    if q.strip()
]

# Total queries per run, priority ones first and the remainder rotated in. Searching
# every brand every hour costs credits for no benefit — new bikes in this class appear
# a few times a week. 0 means run everything every time.
QUERIES_PER_RUN = int(os.environ.get("QUERIES_PER_RUN", "5"))

# Send a listing if it is NEW or if it MENTIONS A TRADE. Never if it refuses trades —
# that check runs first and wins outright. "New" is read from the detail call's
# creation_time, which we are paying for anyway to apply the refusal rule.
NEW_HOURS = float(os.environ.get("NEW_HOURS", "48"))

# Descriptions read per run, 1 credit each. This is the real spend knob: trade intent
# only lives in the description, so a listing has to be read to be judged. Separate
# from MAX_ALERTS because under TRADE_ONLY most reads end in a discard.
MAX_CHECKS = int(os.environ.get("MAX_CHECKS", "8"))

# Hard cap on messages per run, so a lucky batch can't dump ten at once.
MAX_ALERTS = int(os.environ.get("MAX_ALERTS", "6"))

# Pages to read per query, 1 credit each. Page 1 is the newest listings, so 1 is
# enough for ongoing watching. Raise it temporarily to sweep up old standing stock:
# surron alone goes from 24 listings at 1 page to 79 at 4.
PAGES_PER_QUERY = int(os.environ.get("PAGES_PER_QUERY", "1"))
DATE_LISTED = os.environ.get("DATE_LISTED", "all")

# Everything above is the baseline; rules.json — written by your Telegram replies —
# gets the final say. Applied here in one place so no setting is accidentally
# un-tunable, and so `--show` can print exactly what is in force.
REQUIRE = tuned("require", REQUIRE)
EXCLUDE = tuned("exclude", EXCLUDE)
NO_TRADE = tuned("no_trade", NO_TRADE)
DEALER = tuned("dealer", DEALER)
TRADE_OK = tuned("trade_ok", TRADE_OK)
PRIORITY_QUERIES = tuned("priority_queries", PRIORITY_QUERIES)
NEW_HOURS = tuned_num("new_hours", NEW_HOURS)
MAX_CHECKS = tuned_num("max_checks", MAX_CHECKS)
MAX_ALERTS = tuned_num("max_alerts", MAX_ALERTS)
QUERIES_PER_RUN = tuned_num("queries_per_run", QUERIES_PER_RUN)

def apply_brands(queries, priority, require, exclude, rules=None):
    """Blocking a brand must stop it being SEARCHED as well as sent, so the term is
    stripped from the queries and the allowlist and added to the blocklist. "No more
    segways" then costs nothing per run instead of one wasted credit."""
    rules = _RULES if rules is None else rules
    queries, priority = list(queries), list(priority)
    require, exclude = list(require), list(exclude)

    for brand in rules.get("brands_blocked", []):
        b = str(brand).strip().lower()
        if not b:
            continue
        queries = [q for q in queries if b not in q.lower()]
        priority = [q for q in priority if b not in q.lower()]
        require = [w for w in require if b not in w.lower()]
        if b not in exclude:
            exclude.append(b)

    for brand in rules.get("brands_added", []):
        b = str(brand).strip().lower()
        if not b:
            continue
        if b not in queries:
            queries.append(b)
        if b not in require:
            require.append(b)
    return queries, priority, require, exclude


QUERIES, PRIORITY_QUERIES, REQUIRE, EXCLUDE = apply_brands(
    QUERIES, PRIORITY_QUERIES, REQUIRE, EXCLUDE
)


def call(url, params, tries=3):
    """Retries transient failures.

    Observed live: this API returns a 404 for an item that exists, and the retry
    then succeeds — so 404 is retried too. Only auth failures are treated as
    permanent, since no amount of retrying fixes a bad key. An explicit
    success:false raises straight away.
    """
    for attempt in range(tries):
        try:
            r = requests.get(
                url,
                headers={"x-api-key": os.environ["SCRAPECREATORS_KEY"]},
                params=params,
                timeout=60,
            )
            if r.status_code in (401, 403):
                raise RuntimeError(f"auth rejected ({r.status_code}) — check the API key")
            r.raise_for_status()
            payload = r.json()
        except (requests.RequestException, ValueError) as exc:
            if attempt == tries - 1:
                raise
            wait = 2 ** attempt
            print(f"    retrying in {wait}s after {exc}")
            time.sleep(wait)
            continue
        if isinstance(payload, dict) and payload.get("success") is False:
            raise RuntimeError(f"api call failed: {payload}")  # else it reads as "nothing found"
        return payload


def query_slice(hour=None):
    """The queries to run this hour, cycling so every query comes round regularly.

    Strides across the list rather than taking a contiguous block: neighbouring
    queries tend to be similar, so a block can spend a whole run on one weak
    corner of the list while a stride always samples a spread.
    """
    if QUERIES_PER_RUN <= 0 or QUERIES_PER_RUN >= len(QUERIES):
        return list(QUERIES)

    priority = [q for q in PRIORITY_QUERIES if q][:QUERIES_PER_RUN]
    rest = [q for q in QUERIES if q not in priority]
    slots = QUERIES_PER_RUN - len(priority)
    if slots <= 0 or not rest:
        return priority

    if hour is None:
        hour = int(time.time() // 3600)
    n = len(rest)
    step = max(1, n // slots)
    picked, out = set(), []
    i = hour % n
    while len(out) < slots and len(picked) < n:
        if i % n not in picked:
            picked.add(i % n)
            out.append(rest[i % n])
        i += step
    return priority + out


def pick_listings(payload):
    """Response wrapper key is undocumented, so take the first list-of-dicts in it."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for value in payload.values():
        if isinstance(value, list) and (not value or isinstance(value[0], dict)):
            return value
    return []


def search():
    """This run's queries, deduped by listing id. One credit per query."""
    queries = query_slice()
    found, credits, failed = {}, None, 0
    for query in queries:
        cursor, pages, got = None, 0, 0
        while pages < max(1, PAGES_PER_QUERY):
            params = {
                "query": query,
                "lat": LAT,
                "lng": LNG,
                "radius_km": RADIUS_KM,
                "max_price": MAX_PRICE,
                "min_price": MIN_PRICE,
                "sort_by": "creation_time_descend",
                "date_listed": DATE_LISTED,
                "availability": "available",
            }
            if cursor:
                params["cursor"] = cursor
            try:
                payload = call(SEARCH_API, params)
            except Exception as exc:
                # One sick query must not cost us the other queries' results.
                print(f"  query {query!r} page {pages + 1} FAILED: {exc}")
                if pages == 0:
                    failed += 1
                break
            listings = pick_listings(payload)
            got += len(listings)
            pages += 1
            for listing in listings:
                if listing.get("id"):
                    found.setdefault(str(listing["id"]), listing)
            if not isinstance(payload, dict):
                break
            credits = payload.get("credits_remaining", credits)
            cursor = payload.get("cursor")
            if not payload.get("has_next_page") or not cursor:
                break
        print(f"  query {query!r}: {got} over {pages} page(s)")
    if queries and failed == len(queries):
        raise RuntimeError(f"all {failed} queries failed")
    print(f"{len(found)} unique from {queries}, credits left: {credits}")
    return list(found.values())


def detail(listing_id):
    """Full description, needed to read trade intent. Costs 1 credit."""
    try:
        got = call(ITEM_API, {"id": listing_id})
        return got if isinstance(got, dict) else {}
    except Exception as exc:  # a dud detail call shouldn't kill the whole run
        print(f"detail failed for {listing_id}: {exc}")
        return {}


def rejected(title):
    """Why this title is out, or None to keep it.

    Title only, deliberately. Real listings mention "battery" and "tire" in their
    descriptions constantly, so judging on description would drop genuine bikes.
    """
    blob = (title or "").lower()
    hit = next((w for w in EXCLUDE if w in blob), None)
    if hit:
        return f"part:{hit}"
    if not any(w in blob for w in REQUIRE):
        return "not a known model"
    return None


def price_of(listing):
    try:
        return float((listing.get("price") or {}).get("amount"))
    except (TypeError, ValueError):
        return None  # unknown price: let it through rather than miss a real bike


def dealer_signal(*texts):
    """The commercial phrase that marks this as a dealer listing, or None."""
    blob = " ".join(t for t in texts if t).lower()
    return next((p for p in DEALER if p in blob), None)


# Any surviving mention of trading counts. Safe as a catch-all precisely because
# NO_TRADE is checked first and wins, so "no trades" can never reach this. Word
# boundaries, so "trademark" doesn't count. This is what catches phrasings no
# hand-written list would, e.g. "will take trades for Seadoos, Spark, Trixxs".
TRADE_WORD = re.compile(r"\b(trade|trades|traded|trading|swap|swaps|swapping)\b")


def trade_signal(*texts):
    """('no'|'yes'|None, matched phrase) — read from title AND description.

    Negatives are checked first because they contain the positives: "no trades"
    would otherwise match "trade" and get promoted instead of dropped.
    """
    blob = " ".join(t for t in texts if t).lower()
    for phrase in NO_TRADE:
        if phrase in blob:
            return "no", phrase
    for phrase in TRADE_OK:          # specific phrases first, they read better in the alert
        if phrase in blob:
            return "yes", phrase
    m = TRADE_WORD.search(blob)
    if m:
        return "yes", m.group(0)
    return None, None


_UNITS = {"minute": 1 / 60, "hour": 1, "day": 24, "week": 168, "month": 730, "year": 8760}


def age_hours(info, now=None):
    """How long ago this was listed, or None if the API didn't say.

    Prefers creation_time (exact). Falls back to parsing "Listed 3 weeks ago",
    which is all some listings carry.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    stamp = (info or {}).get("creation_time")
    if stamp:
        try:
            when = datetime.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=datetime.timezone.utc)
            return (now - when).total_seconds() / 3600
        except ValueError:
            pass
    text = ((info or {}).get("listing_date_text") or "").lower()
    m = re.search(r"(\d+)\s*(minute|hour|day|week|month|year)", text)
    if m:
        return int(m.group(1)) * _UNITS[m.group(2)]
    if "just now" in text or "moments ago" in text:
        return 0.0
    return None


def snippet(text, phrase, width=70):
    """The matched phrase plus a little context, collapsed to one line."""
    flat = " ".join((text or "").split())
    i = flat.lower().find((phrase or "").lower())
    if not phrase or i < 0:
        return phrase or ""
    pad = max(0, (width - len(phrase)) // 2)
    start, end = max(0, i - pad), min(len(flat), i + len(phrase) + pad)
    return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")


def card(listing, info=None, hit=None):
    """Exactly three lines: trade status, the facts, the link."""
    info = info or {}
    esc = lambda s: html.escape(str(s))

    head = f"<b>{esc(listing.get('title') or '(no title)')}</b>"
    head += f" — {esc((listing.get('price') or {}).get('formatted_amount', '?'))}"
    where = info.get("location_text") or (listing.get("location") or {}).get("display_name")
    if where:
        head += f" · {esc(where)}"

    age = age_hours(info)
    if age is not None:
        head += f" · {age:.0f}h ago" if age < 48 else f" · {age / 24:.0f}d ago"

    # Trade status is always stated, so the first glance sorts the list.
    if hit:
        status = f"⚡ <b>WANTS TRADE</b> · <i>{esc(snippet(info.get('description') or listing.get('title'), hit))}</i>"
    else:
        status = "◽ no trade mention"

    return "\n".join([status, head, esc(listing.get("url") or "")])


def notify(text, tries=4):
    """Telegram caps sustained sends to one chat at roughly 1/sec, so honour 429s."""
    for attempt in range(tries):
        r = requests.post(
            f"https://api.telegram.org/bot{os.environ['TG_TOKEN']}/sendMessage",
            json={"chat_id": os.environ["TG_CHAT"], "text": text, "parse_mode": "HTML"},
            timeout=30,
        )
        if r.status_code == 429:
            try:
                wait = int(r.json()["parameters"]["retry_after"])
            except (ValueError, KeyError, TypeError):
                wait = 5 * (attempt + 1)
            print(f"telegram rate limited, waiting {wait}s")
            time.sleep(wait + 1)
            continue
        r.raise_for_status()
        try:
            return r.json()["result"]["message_id"]   # so a reply can be tied to it
        except (ValueError, KeyError, TypeError):
            return None
    raise RuntimeError("telegram rate limited on every attempt")


def new_ids(listings, seen):
    """Listings not in seen, deduped. Pure: seen is only updated once a send succeeds."""
    out, batch = [], set()
    for listing in listings:
        lid = str(listing.get("id") or "")
        if lid and lid not in seen and lid not in batch:
            batch.add(lid)
            out.append(listing)
    return out


def shortlist(fresh):
    """Free title-only pass: (queue, skips). Costs no credits.

    Anything decided by the title alone is decided here, before a single paid
    detail call. Survivors are ordered so titles that already advertise a trade
    get read first, which is what the per-run cap then takes from.
    """
    queue, skips = [], []
    for listing in fresh:
        title = listing.get("title")
        nope = rejected(title)
        if nope:
            skips.append((listing, nope))
            continue
        verdict, phrase = trade_signal(title)
        if verdict == "no":
            skips.append((listing, f"refuses:{phrase}"))
            continue
        shop = dealer_signal(title)
        if shop:
            skips.append((listing, f"dealer:{shop}"))
            continue
        price = price_of(listing)
        if price is not None and price < MIN_PRICE:
            # Price is in the search result, so this costs nothing.
            skips.append((listing, f"under {MIN_PRICE:g} (CA${price:g})"))
            continue
        queue.append((verdict != "yes", listing))   # False sorts first
    queue.sort(key=lambda q: q[0])
    return [listing for _, listing in queue], skips


def triage(listings, get_detail=None):
    """(alerts sorted trade-first, skips). Titles must already be vetted by shortlist."""
    get_detail = get_detail or detail   # resolved here, not at def time, so it's swappable
    alerts, skips = [], []
    for listing in listings:
        info = get_detail(listing["id"])   # the one paid call per listing
        title, desc = listing.get("title"), info.get("description")
        verdict, phrase = trade_signal(title, desc)
        if verdict == "no":
            # HARD RULE, checked first and unconditional.
            skips.append((listing, f"refuses:{phrase}"))
            continue
        shop = dealer_signal(title, desc)
        if shop:
            # Also hard: a dealer that accepts trade-ins is still a dealer.
            skips.append((listing, f"dealer:{shop}"))
            continue
        age = age_hours(info)
        fresh = age is not None and age <= NEW_HOURS
        wants = verdict == "yes"
        if not (fresh or wants):
            # Read, judged, discarded — but recorded, so the credit is never respent.
            aged = "age unknown" if age is None else f"{age / 24:.0f}d old"
            skips.append((listing, f"not new ({aged}), no trade mention"))
            continue
        alerts.append((wants, fresh, listing, info, phrase if wants else None))
    alerts.sort(key=lambda a: (not a[0], not a[1]))   # trades first, then new
    return alerts, skips


def slim(listing):
    """Only what card() and triage() read. The raw dict is mostly photo CDN URLs, and
    this file is rewritten and committed every run."""
    return {
        "id": str(listing.get("id") or ""),
        "title": listing.get("title"),
        "url": listing.get("url"),
        # amount as well as the formatted string: price_of() reads amount, and dropping
        # it silently disabled the MIN_PRICE floor for everything already queued.
        "price": {
            "formatted_amount": (listing.get("price") or {}).get("formatted_amount"),
            "amount": (listing.get("price") or {}).get("amount"),
        },
        "location": {"display_name": (listing.get("location") or {}).get("display_name")},
    }


def remember(message_id, listing):
    """Keep message_id -> listing so a reply can name what it is about."""
    state = load_json(STATE, {})
    msgs = state.setdefault("messages", {})
    msgs[str(message_id)] = {
        "title": listing.get("title"),
        "price": (listing.get("price") or {}).get("formatted_amount"),
        "url": listing.get("url"),
    }
    # ponytail: keep the last 300; older alerts aren't going to get replied to
    if len(msgs) > 300:
        for key in sorted(msgs, key=int)[:-300]:
            del msgs[key]
    STATE.write_text(json.dumps(state))


def from_alert_text(text):
    """Recover title/price from an alert's own text, for replies we have no record of.

    Alerts are three lines: status, "<title> — <price> · <where> · <age>", link.
    """
    if not text:
        return {"title": "(no specific listing)", "price": ""}
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    facts = next((l for l in lines if "—" in l), None)
    if not facts:
        return {"title": lines[0] if lines else "(no specific listing)", "price": ""}
    title, _, rest = facts.partition("—")
    return {"title": title.strip(), "price": rest.split("·")[0].strip()}


RULE_TOOL = {
    "name": "adjust",
    "description": "Apply one adjustment to the bike-watcher's filters, based on the user's "
                   "feedback about a specific listing. Choose 'none' if the feedback is "
                   "praise, a question, or too vague to act on safely.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "block_brand",     # stop searching for and stop sending this make
                    "add_brand",       # start searching for and allow this make
                    "block_word",      # reject listings whose title contains this
                    "mark_dealer",     # treat this phrase as a commercial seller
                    "mark_no_trade",   # treat this phrase as refusing trades
                    "set_min_price",
                    "set_max_price",
                    "set_radius_km",
                    "set_new_hours",
                    "set_max_alerts",
                    "none",
                ],
            },
            "value": {
                "type": "string",
                "description": "The brand, phrase, or number. Lowercase for text. Keep text "
                               "as short as possible while still being specific — one or two "
                               "words. Never a whole listing title.",
            },
            "reply": {
                "type": "string",
                "description": "One short sentence, addressed to the user, confirming what "
                               "changed. Plain language, no jargon.",
            },
        },
        "required": ["action", "value", "reply"],
    },
}


def interpret(feedback, listing):
    """Turn a free-text reply into one filter change. Returns the tool input dict."""
    context = (
        f"Current settings:\n"
        f"- searching for: {', '.join(QUERIES)}\n"
        f"- price range: CA${MIN_PRICE:g} to CA${MAX_PRICE:g}\n"
        f"- radius: {RADIUS_KM:g} km from North York, Toronto\n"
        f"- counts as new: listed within {NEW_HOURS:g} hours\n"
        f"- max alerts per run: {MAX_ALERTS}\n"
    )
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": os.environ.get("FEEDBACK_MODEL", "claude-sonnet-5"),
            "max_tokens": 400,
            "system": (
                "You tune a bot that watches Facebook Marketplace for high-performance "
                "electric dirt bikes (Sur-Ron, Talaria, E Ride Pro, 79Bike and similar, "
                "roughly CA$2500-15000) that the owner wants to trade a go-kart for.\n\n"
                "He does NOT want: scooters, kids' toys, fat-tire commuter e-bikes, "
                "electric street/sport bikes, gas bikes, parts, or dealer listings.\n\n"
                "Turn his feedback into exactly ONE filter change via the adjust tool. "
                "Prefer the narrowest change that fixes the complaint. Use block_word for "
                "a product category or descriptor, block_brand for a make he wants gone "
                "entirely. If the feedback is vague, positive, or you would have to guess "
                "at a number, use action 'none' and say what you'd need to know.\n\n"
                "CRITICAL: for mark_dealer and mark_no_trade the value must be the "
                "commercial or refusal PHRASE that identifies the type of seller — "
                "'financing', 'plus taxes', 'in stock', 'no trades'. NEVER a brand name, "
                "model name or listing title: those are what he is shopping for, and "
                "blocking one would hide every bike of that make. Same for block_word.\n\n"
                + context
            ),
            "tools": [RULE_TOOL],
            "tool_choice": {"type": "tool", "name": "adjust"},
            "messages": [{
                "role": "user",
                "content": (
                    f"Listing he is replying to:\n"
                    f"  title: {listing.get('title')}\n"
                    f"  price: {listing.get('price')}\n\n"
                    f"His reply: {feedback}"
                ),
            }],
        },
        timeout=60,
    )
    r.raise_for_status()
    for block in r.json().get("content", []):
        if block.get("type") == "tool_use":
            return block["input"]
    return None


def apply_rule(rule):
    """Write one adjustment into rules.json. Returns a human description, or None."""
    action, value = rule.get("action"), str(rule.get("value", "")).strip()
    if action in (None, "none") or not value:
        return None

    data = load_json(RULES, {})
    lists = {
        "block_brand": "brands_blocked",
        "add_brand": "brands_added",
        "block_word": "exclude_add",
        "mark_dealer": "dealer_add",
        "mark_no_trade": "no_trade_add",
    }
    numbers = {
        "set_min_price": "min_price",
        "set_max_price": "max_price",
        "set_radius_km": "radius_km",
        "set_new_hours": "new_hours",
        "set_max_alerts": "max_alerts",
    }

    # Refuse any blocking rule whose text collides with a brand we are hunting.
    # Observed live: "this guy is obviously a dealer" produced
    # mark_dealer="talaria mx5 pro 72v" — the listing title. Had it produced
    # "talaria", every Talaria would have been silently filtered out forever.
    if action in ("block_word", "mark_dealer", "mark_no_trade"):
        low = value.lower()
        clash = next((w for w in REQUIRE if w in low or (len(w) > 4 and low in w)), None)
        if clash:
            print(f"refusing {action}={value!r}: would block the wanted brand {clash!r}")
            return None

    if action in lists:
        key = lists[action]
        bucket = data.setdefault(key, [])
        if value.lower() in [str(x).lower() for x in bucket]:
            return None                       # already set, don't churn the file
        bucket.append(value.lower())
    elif action in numbers:
        try:
            data[numbers[action]] = float(value.replace(",", "").replace("km", "").strip())
        except ValueError:
            return None
    else:
        return None

    RULES.write_text(json.dumps(data, indent=2, sort_keys=True))
    return f"{action} = {value}"


def feedback():
    """Read Telegram replies, convert each to a filter change, confirm back."""
    state = load_json(STATE, {})
    params = {"timeout": 0, "allowed_updates": '["message"]'}
    if state.get("tg_offset"):
        params["offset"] = state["tg_offset"]
    r = requests.get(
        f"https://api.telegram.org/bot{os.environ['TG_TOKEN']}/getUpdates",
        params=params, timeout=45,
    )
    r.raise_for_status()
    updates = r.json().get("result", [])
    if not updates:
        print("no replies")
        return

    msgs = state.get("messages", {})
    applied = 0
    for upd in updates:
        state["tg_offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        replied_to = (msg.get("reply_to_message") or {}).get("message_id")
        if not text or text.startswith("/"):
            continue
        listing = msgs.get(str(replied_to), {}) if replied_to else {}
        if not listing:
            # Telegram echoes the original message back inside reply_to_message, so the
            # alert text itself is a fallback when state.json has no record of it —
            # e.g. alerts sent before this feature existed.
            listing = from_alert_text((msg.get("reply_to_message") or {}).get("text"))
        try:
            rule = interpret(text, listing)
        except Exception as exc:
            print(f"interpret failed: {exc}")
            continue
        what = apply_rule(rule) if rule else None
        note = (rule or {}).get("reply") or "Nothing to change."
        print(f"reply {text!r} -> {what or 'no change'}")
        notify(("✅ " if what else "🤔 ") + html.escape(note))
        applied += bool(what)

    STATE.write_text(json.dumps(state))
    print(f"{len(updates)} update(s), {applied} rule(s) applied")


def show():
    """Print what is actually in force, after rules.json overrides."""
    print(f"searching   : {', '.join(QUERIES)}")
    print(f"always      : {', '.join(PRIORITY_QUERIES)}  ({QUERIES_PER_RUN}/run)")
    print(f"price       : CA${MIN_PRICE:g} - CA${MAX_PRICE:g}")
    print(f"radius      : {RADIUS_KM:g} km")
    print(f"new within  : {NEW_HOURS:g} h")
    print(f"per run     : {MAX_CHECKS} checks, {MAX_ALERTS} alerts")
    print(f"blocked     : {len(EXCLUDE)} words, {len(DEALER)} dealer phrases")
    print(f"rules.json  : {json.dumps(_RULES, sort_keys=True) if _RULES else '(none yet)'}")


def main():
    seen = set(load_json(SEEN, []))
    # Already vetted on an earlier run; anything since alerted is dropped.
    pending, dedupe = [], set()
    for l in load_json(PENDING, []):
        lid = str(l.get("id") or "")
        if lid and lid not in seen and lid not in dedupe:
            dedupe.add(lid)
            pending.append(l)
    known = seen | {str(l["id"]) for l in pending}

    unseen = [l for l in search() if str(l.get("id") or "") and str(l["id"]) not in known]
    queue, skips = shortlist(unseen)

    # Re-rank the whole backlog each run, so a bike that advertises a trade jumps
    # ahead of stock that has merely been waiting longer.
    pool = pending + queue
    pool.sort(key=lambda l: trade_signal(l.get("title"))[0] != "yes")
    batch = pool[:MAX_CHECKS] if MAX_CHECKS > 0 else pool
    held = pool[len(batch):]

    alerts, more_skips = triage(batch)
    if MAX_ALERTS > 0 and len(alerts) > MAX_ALERTS:
        # Spillover returns to the queue. It will be re-read next run, costing the
        # credit again, so keep MAX_ALERTS >= MAX_CHECKS unless you want that.
        held = [a[2] for a in alerts[MAX_ALERTS:]] + held
        alerts = alerts[:MAX_ALERTS]
    for listing, why in skips + more_skips:
        print(f"skip {listing['id']} ({why}): {listing.get('title')}")
        seen.add(str(listing["id"]))

    sent = hot = new = 0
    try:
        for wants, fresh, listing, info, hit in alerts:
            mid = notify(card(listing, info, hit))
            if mid:
                remember(mid, listing)
            # Recorded only after the send lands, so a rate-limit crash re-queues the
            # stragglers instead of burying them as already-seen.
            seen.add(str(listing["id"]))
            sent += 1
            hot += bool(wants)
            new += bool(fresh)
            time.sleep(SEND_DELAY)
    finally:
        SEEN.write_text(json.dumps(sorted(seen)))
        # Anything neither sent nor skipped goes back in the queue, including the
        # tail of a batch that died mid-send. Deduped by id: MAX_ALERTS spillover is
        # still in `batch`, so a naive concat would queue it twice and both re-read
        # and re-send it.
        leftover, queued_ids = [], set()
        for l in batch + held:
            lid = str(l.get("id") or "")
            if not lid or lid in seen or lid in queued_ids:
                continue
            queued_ids.add(lid)
            leftover.append(slim(l))
        PENDING.write_text(json.dumps(leftover))

    print(
        f"{len(unseen)} unseen, {len(batch)} read, {sent} sent "
        f"({hot} want trades, {new} new), "
        f"{len(skips) + len(more_skips)} skipped, {len(leftover)} queued"
    )


def selftest():
    assert pick_listings({"ok": True, "listings": [{"id": "1"}]}) == [{"id": "1"}]
    assert pick_listings([{"id": "1"}]) == [{"id": "1"}]
    assert pick_listings({"ok": True, "tags": ["a", "b"]}) == []
    assert pick_listings("nope") == []

    seen = {"1"}
    # "2" and 2 are the same listing; seen must not be mutated here
    assert [l["id"] for l in new_ids([{"id": "1"}, {"id": "2"}, {"id": 2}, {}], seen)] == ["2"]
    assert seen == {"1"}

    # allowlist keeps the real models, drops the search pollution
    assert rejected("2024 Sur-Ron Ultra Bee") is None
    assert rejected("Talaria 3x 2023") is None
    assert rejected("79Bike Falcon GT - Demonstration Model NEW") is None
    assert rejected("Surron LBX (2 batteries)") is None          # "batteries" != "battery only"
    assert rejected("2025 Fujikura Ventus FW 6-S Shaft") == "part:shaft"
    assert rejected("Brand New Tommy Hilfiger Crewneck Sweater") == "part:sweater"
    assert rejected("Used book - a novel") == "not a known model"
    assert rejected("100% altis helmet kids") == "part:helmet"
    assert rejected("Key switch for surron and mx3") == "part:key switch"
    assert rejected("Talaria and surron parts") == "part:parts"
    assert rejected("Jetson ebike") == "not a known model"       # commuter e-bike, not wanted
    assert rejected("Ron White sneakers") == "not a known model"  # the 'rawrr' query returns these
    assert rejected("2025 Strike Shadow SX 60V") is None
    assert rejected("Gt73Pro *MODDED* (check description)") is None
    # dropped from the allowlist on purpose: fat-tire commuters and cheap imports
    assert rejected("HeyBike Villain TRADE") == "not a known model"
    assert rejected("Dirt Bikes Available Yozma heybike jasion") == "not a known model"
    assert rejected(None) == "not a known model"

    # negatives must beat positives: these all contain the word "trade"
    assert trade_signal("Ebike NO TRADES")[0] == "no"
    assert trade_signal("nice ebike", "not trading, cash only")[0] == "no"
    assert trade_signal("36V e-bike CASH ONLY (willing to negotiate)")[0] == "no"
    assert trade_signal("ACCEPTING TRADES 48V 500W ebike") == ("yes", "accepting trades")
    assert trade_signal("ebike", "open to trades for a dirt bike")[0] == "yes"
    assert trade_signal("plain ebike", "good condition") == (None, None)
    assert trade_signal(None, None) == (None, None)

    # phrasings no hand-written list would cover — the real listing that exposed this
    # said "will take trades for Seadoos, Spark, Trixxs", and "trades for" is not a
    # substring of "trade for"
    john = "Talaria X3 Pro, we'll take trades for Seadoos, Spark, Trixxs. Can add cash on top."
    assert trade_signal("Talaria x3 pro 2025", john)[0] == "yes", trade_signal("t", john)
    for phrasing in [
        "open to any trades", "swapping for a quad?", "would consider a swap",
        "traded my last one", "trades considered", "TRADE?", "looking to trade up",
    ]:
        assert trade_signal("Surron LBX", phrasing)[0] == "yes", phrasing
    # but a refusal still wins, and word boundaries hold
    assert trade_signal("Surron", "no trades, will take trades for nothing")[0] == "no"
    assert trade_signal("Surron", "registered trademark of Segway")[0] is None
    # the quote shown gives the useful context, not just the bare word
    _, hit = trade_signal("Talaria x3 pro", john)
    assert "Seadoos" in snippet(john, hit), snippet(john, hit)

    # real listing 1357502152470801, verbatim — the refusal is in the last line,
    # after 500 characters of enthusiastic build notes
    real = (
        "Ready to ride. Zero mechanical issues and the battery health is perfect. It is "
        "sitting on upgraded rims and supermoto wheels, making it the perfect street setup."
        "Tons of money into this build. It has a custom heavy-duty bash guard, an upgraded "
        "foot lock, a custom foot brake setup, and a clean aftermarket ignition placement. "
        "All the brakes are fresh and grab hard. Also completely wired up with custom rock "
        "lights, a fender light, and wheelie lights that look crazy at night.Runs "
        "flawlessly and needs absolutely nothing.\n\nAnd the forks alone r 2.1k\nAnd rear "
        "shock is 1.7k\n\nNo trades or lowball offers. Serious buyers only. Cash in hand "
        "if you want to test ride it."
    )
    assert trade_signal("2024 Sur-Ron Ultra Bee", real) == ("no", "no trades")
    # and it must still refuse if the seller only says the cash part
    assert trade_signal("Ultra Bee", "Cash in hand if you want to test ride it.")[0] == "no"

    # free title pass drops junk and refusers, and promotes advertised trades
    fresh = [
        {"id": "n", "title": "2024 Talaria Sting"},
        {"id": "x", "title": "Fujikura Ventus shaft"},
        {"id": "y", "title": "Surron Light Bee X", "price": {"formatted_amount": "CA$6,099"}},
        {"id": "r", "title": "Sur-Ron Ultra Bee cash only"},
        {"id": "t", "title": "79Bike Falcon - open to trades"},
    ]
    queue, skips = shortlist(fresh)
    assert [l["id"] for l in queue] == ["t", "n", "y"], queue   # advertised trade first
    assert sorted(s[1].split(":")[0] for s in skips) == ["part", "refuses"]

    # age parsing, from both sources
    now = datetime.datetime(2026, 7, 30, 12, 0, tzinfo=datetime.timezone.utc)
    assert age_hours({"creation_time": "2026-07-30T06:00:00.000Z"}, now) == 6.0
    assert age_hours({"creation_time": "2026-07-28T12:00:00.000Z"}, now) == 48.0
    assert age_hours({"listing_date_text": "Listed 3 weeks ago"}) == 504.0
    assert age_hours({"listing_date_text": "Listed 2 hours ago"}) == 2.0
    assert age_hours({"listing_date_text": "Listed just now"}) == 0.0
    assert age_hours({"creation_time": "garbage", "listing_date_text": "Listed 1 day ago"}) == 24.0
    assert age_hours({}) is None

    # the OR rule: new alone qualifies, trade alone qualifies, neither does not,
    # and a refusal beats both
    recent = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(hours=3)).isoformat().replace("+00:00", "Z")
    bikes = [
        {"id": "new", "title": "Surron Light Bee X"},
        {"id": "trade", "title": "Talaria Sting"},
        {"id": "stale", "title": "79Bike Falcon"},
        {"id": "both", "title": "Sur-Ron Ultra Bee"},
        {"id": "refuse", "title": "Surron LBX"},
    ]
    details = {
        "new": {"creation_time": recent, "description": "clean bike"},
        "trade": {"listing_date_text": "Listed 5 weeks ago", "description": "open to trades"},
        "stale": {"listing_date_text": "Listed 5 weeks ago", "description": "clean bike"},
        "both": {"creation_time": recent, "description": "will trade for a quad"},
        "refuse": {"creation_time": recent, "description": "no trades, cash only"},
    }
    alerts, more = triage(bikes, get_detail=lambda i: details[i])
    assert [a[2]["id"] for a in alerts] == ["both", "trade", "new"], [a[2]["id"] for a in alerts]
    assert [(a[0], a[1]) for a in alerts] == [(True, True), (True, False), (False, True)]
    # a trade-wanter must never be the one deferred by MAX_ALERTS: sorted first, so
    # spillover can only ever come off the tail, which is the non-trade end
    assert all(a[0] for a in alerts[:2]) and not alerts[-1][0]
    dropped = {l["id"]: why for l, why in more}
    assert set(dropped) == {"stale", "refuse"}, dropped
    assert dropped["refuse"].startswith("refuses:")      # hard rule, regardless of being new
    assert "not new" in dropped["stale"]

    # dealers are dropped even when new AND offering trade-ins
    assert dealer_signal("Talaria X3", "Financing available, plus taxes") == "financing"
    assert dealer_signal("79Bike Falcon GT - Demonstration Model NEW") == "demonstration model"
    assert dealer_signal("Surron LBX, private sale, no rust") is None
    shop_bikes = [{"id": "shop", "title": "Talaria Sting"}]
    shop_alerts, shop_skips = triage(
        shop_bikes,
        get_detail=lambda i: {"creation_time": recent,
                              "description": "Trade-ins welcome! Financing available, plus taxes."},
    )
    assert shop_alerts == [] and shop_skips[0][1] == "dealer:financing", (shop_alerts, shop_skips)
    # and caught for free from the title alone, before any paid call
    free_q, free_s = shortlist([{"id": "d", "title": "ELECTRIC DIRT BIKE SALE - financing available"}])
    assert free_q == [] and free_s[0][1].startswith("dealer:"), (free_q, free_s)

    # a new post with no trade mention still says so, and shows its age
    only_new = card(bikes[0], details["new"])
    assert only_new.startswith("◽ no trade mention") and "ago" in only_new
    assert card(bikes[3], details["both"], "will trade").count("\n") == 2

    # the junk-query brands are gone from QUERIES but still recognised in REQUIRE
    assert "rawrr" not in QUERIES and rejected("Rawrr Mantis 2024") is None
    assert rejected("Arc'teryx MANTIS 1 WAIST PACK") == "not a known model"
    assert rejected("Marvel Legends Mantis action figure") == "not a known model"
    assert rejected("Mens hoodie size 3XL") == "not a known model"

    # every card is exactly 3 lines, states trade status, and carries no pitch text
    hot = card(
        fresh[2],
        {"description": "Selling my Light Bee. Trades welcome, prefer a dirt bike or quad.",
         "location_text": "Vaughan, ON", "listing_date_text": "Listed 6 hours ago"},
        "trades welcome",
    )
    assert hot.count("\n") == 2, hot
    assert hot.startswith("⚡") and "WANTS TRADE" in hot
    assert "Trades welcome, prefer a dirt bike" in hot          # the reasoning, in context
    assert "<code>" not in hot and "go-kart" not in hot         # no copy-paste message
    assert "CA$6,099" in hot and "Vaughan, ON" in hot and "6h ago" in hot

    plain = card(
        {"title": "Bike & Trailer <used>", "price": {"formatted_amount": "CA$5,350"}, "url": "u"},
        {"description": "x" * 900, "location_text": "Barrie, ON",
         "listing_date_text": "Listed 3 weeks ago"},
    )
    assert plain.count("\n") == 2, plain
    assert "&amp;" in plain and "&lt;used&gt;" in plain         # escaped, not raw HTML
    assert plain.startswith("◽ no trade mention"), plain        # status always stated
    assert "xxx" not in plain                                   # description body is gone
    assert "21d ago" in plain
    assert len(plain) < 300, len(plain)

    assert "<b>" in card({})                                   # survives an empty listing
    # snippet trims around the match and marks the elisions
    long_desc = "a" * 200 + " open to trades " + "b" * 200
    assert snippet(long_desc, "open to trades").startswith("…")
    assert snippet(long_desc, "open to trades").endswith("…")
    assert "open to trades" in snippet(long_desc, "open to trades")
    assert snippet("no match here", "trade") == "trade"
    assert snippet(None, None) == ""

    # the junk that was actually getting through, all rejected on the title alone
    for junk in [
        "Razor MX650 dirt bike",                 # kids' toy brand, no longer allowlisted
        "Ridstar Q20 fat tire ebike",
        "2003 Yamaha YZ250F",                    # gas
        "2023 Honda CRF 150cc",
        "Drift hero mini bike 212cc",
        "Kukirin G4 electric scooter",
        "HeyBike Villain fat tire",
        "Freezway Z10 Zero 10x E SCOOTER",
        "36V e-bike pedal assist",
        # these actually reached the queue before the allowlist was tightened
        "The Trials of Apollo",                  # bare "apollo" matched a novel
        "Apollo RFZ dirtbike",                   # Apollo's gas line, not the electric RFN
        "2020 Rumi Mx350",                       # bare "mx3" matched a scooter
        "2025 Segway muse",                      # Segway scooter, not a Segway dirt bike
        "Segway Ninebot MAX G3 Electric Scooter",
        "79 bike charger",
        "2022 Kawasaki KLX 230R Dirt Bike",
        "Fastace rear shock for surron talaria",  # parts that carry a real brand name
        "Talaria/Surron Seat",
    ]:
        assert rejected(junk) is not None, junk

    # and the real machines still pass
    for good in [
        "2024 Sur-Ron Ultra Bee", "Talaria x3 2025", "E Ride Pro SS 2.0",
        "79Bike Falcon GT", "2025 Strike Shadow SX 60V", "Gt73Pro *MODDED*",
        "Talaria Komodo", "Apollo RFN Rally",
        "Talaria x3 pro 2025", "Talaria mx5 pro", "Caofen fx electric dirtbike",
        "79BIKE LYNX | 13Kw | 40ah",
        # Segway is deliberately absent: it is a brand the owner can block by reply,
        # and asserting on it here would make the suite fail on his preference rather
        # than on a code defect.
    ]:
        assert rejected(good) is None, good

    # a red electric street bike that slipped in via the brand "Electric Motion"
    assert rejected("2023 Electric Motion Sport") == "not a known model"
    assert rejected("2020 Kawasaki ninja 400 abs") is not None
    assert rejected("2002 Harley-Davidson Sportster 883") is not None

    # slim() must preserve the numeric amount or the price floor silently stops working
    slimmed = slim({"id": "1", "title": "t", "url": "u",
                    "price": {"amount": 4200, "formatted_amount": "CA$4,200"},
                    "location": {"display_name": "x"}})
    assert price_of(slimmed) == 4200.0, slimmed
    assert shortlist([dict(slimmed, title="Surron LBX")])[0], "slimmed listing must survive"

    # price floor runs free, off the search result
    q, s = shortlist([
        {"id": "cheap", "title": "Surron Light Bee", "price": {"amount": 300}},
        {"id": "ok", "title": "Surron Light Bee", "price": {"amount": 4200}},
        {"id": "noprice", "title": "Talaria Sting"},
    ])
    assert [l["id"] for l in q] == ["ok", "noprice"], q          # unknown price passes
    assert s[0][1].startswith("under 2500"), s

    # a generic title is allowed, but a CA$1,400 one still dies on price
    assert rejected("Electric Dirt Bike") is None
    gq, gs = shortlist([{"id": "g", "title": "Electric Dirt Bike", "price": {"amount": 1400}}])
    assert gq == [] and "under 2500" in gs[0][1], (gq, gs)

    retry_policy_check()

    # a reply must be understandable from the alert's own text, with no state.json
    # record — alerts sent before this feature existed have no stored mapping
    alert = ("◽ no trade mention\n"
             "2023 Electric Motion Sport — CA$2,500 · Innisfil, ON · 20h ago\n"
             "https://www.facebook.com/marketplace/item/990267120720477/")
    got = from_alert_text(alert)
    assert got["title"] == "2023 Electric Motion Sport", got
    assert got["price"] == "CA$2,500", got
    hot = "⚡ WANTS TRADE · …trades for a quad…\nTalaria x3 — CA$5,500\nhttp://x"
    assert from_alert_text(hot)["title"] == "Talaria x3"
    assert from_alert_text(None)["title"] == "(no specific listing)"
    assert from_alert_text("just some text")["title"] == "just some text"

    priority_query_check()
    crash_safety_check()
    cap_defer_check()
    rules_check()
    print("ok")


def rules_check():
    """rules.json must override the baseline, and a blocked brand must stop being
    searched as well as sent — otherwise "no more segways" still costs a credit."""
    import tempfile

    global RULES, _RULES
    keep = (RULES, _RULES)
    try:
        RULES = pathlib.Path(tempfile.mkdtemp()) / "rules.json"

        assert apply_rule({"action": "block_brand", "value": "Segway"}) is not None
        assert apply_rule({"action": "block_brand", "value": "segway"}) is None   # no churn
        assert apply_rule({"action": "set_radius_km", "value": "50 km"}) is not None
        assert apply_rule({"action": "set_min_price", "value": "3,000"}) is not None
        assert apply_rule({"action": "block_word", "value": "supermoto"}) is not None
        assert apply_rule({"action": "none", "value": ""}) is None
        assert apply_rule({"action": "set_min_price", "value": "cheap"}) is None  # unparseable
        assert apply_rule({"action": "nonsense", "value": "x"}) is None

        saved = json.loads(RULES.read_text())
        assert saved["brands_blocked"] == ["segway"], saved
        assert saved["radius_km"] == 50.0 and saved["min_price"] == 3000.0, saved
        assert saved["exclude_add"] == ["supermoto"], saved

        # a blocking rule must never be allowed to hide a brand we are hunting.
        # "this guy is obviously a dealer" produced mark_dealer="talaria mx5 pro 72v"
        # live; had it said "talaria", every Talaria would have vanished silently.
        for bad in ["talaria", "Talaria MX5 Pro 72V", "surron", "sur-ron", "light bee"]:
            assert apply_rule({"action": "mark_dealer", "value": bad}) is None, bad
            assert apply_rule({"action": "block_word", "value": bad}) is None, bad
            assert apply_rule({"action": "mark_no_trade", "value": bad}) is None, bad
        # genuine commercial phrases still go through
        assert apply_rule({"action": "mark_dealer", "value": "trade-in special"}) is not None
        # and deliberately dropping a brand is still allowed, via block_brand
        assert apply_rule({"action": "block_brand", "value": "talaria"}) is not None

        # the override helpers read it back correctly
        _RULES = saved
        assert tuned_num("radius_km", 200.0) == 50.0
        assert tuned_num("new_hours", 48.0) == 48.0                  # untouched stays
        assert "supermoto" in tuned("exclude", ["parts"])

        # a blocked brand leaves the queries, the allowlist AND enters the blocklist
        qs, pri, req, exc = apply_brands(
            ["surron", "segway dirt bike"], ["surron", "segway dirt bike"],
            ["surron", "segway x1", "segway dirt"], ["parts"], rules=saved,
        )
        assert qs == ["surron"] and pri == ["surron"], (qs, pri)
        assert req == ["surron"], req
        assert "segway" in exc, exc

        # adding a brand puts it in both places so it is searched and allowed
        qs2, _, req2, _ = apply_brands(
            ["surron"], ["surron"], ["surron"], [], rules={"brands_added": ["Kuberg"]},
        )
        assert qs2 == ["surron", "kuberg"] and req2 == ["surron", "kuberg"], (qs2, req2)
    finally:
        RULES, _RULES = keep


def retry_policy_check():
    """404 and 500 retry (both proved transient live); 401 gives up at once."""
    import unittest.mock as mock

    class Resp:
        def __init__(self, code):
            self.status_code = code

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(f"{self.status_code}")

        def json(self):
            return {"success": True, "listings": []}

    os.environ.setdefault("SCRAPECREATORS_KEY", "test")
    for code in (404, 500):
        seq = [Resp(code), Resp(200)]
        with mock.patch.object(requests, "get", side_effect=seq), \
             mock.patch.object(time, "sleep", lambda _s: None):
            assert call("u", {}) == {"success": True, "listings": []}, code

    for code in (401, 403):
        with mock.patch.object(requests, "get", return_value=Resp(code)) as g, \
             mock.patch.object(time, "sleep", lambda _s: None):
            try:
                call("u", {})
                raise AssertionError(f"{code} should not be retried")
            except RuntimeError as exc:
                assert "auth rejected" in str(exc), exc
            assert g.call_count == 1, g.call_count   # no retries burned on a bad key


class _sandbox:
    """Point the state files at a temp dir and stub the network for one main() run."""

    def __init__(self, listings, notify_fn, max_alerts=6):
        self.listings, self.notify_fn, self.max_alerts = listings, notify_fn, max_alerts

    def __enter__(self):
        import tempfile

        global SEEN, PENDING, SEND_DELAY, MAX_ALERTS
        self.keep = (SEEN, PENDING, SEND_DELAY, MAX_ALERTS,
                     globals()["search"], globals()["detail"], globals()["notify"])
        tmp = pathlib.Path(tempfile.mkdtemp())
        SEEN, PENDING = tmp / "seen.json", tmp / "pending.json"
        SEND_DELAY, MAX_ALERTS = 0, self.max_alerts
        globals()["search"] = lambda: self.listings
        # Recent, so these qualify as NEW and actually reach the send path.
        recent = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        globals()["detail"] = lambda _id: {"creation_time": recent}
        globals()["notify"] = self.notify_fn
        return self

    def state(self):
        return (set(load_json(SEEN, [])),
                {str(l["id"]) for l in load_json(PENDING, [])})

    def __exit__(self, *exc):
        global SEEN, PENDING, SEND_DELAY, MAX_ALERTS
        SEEN, PENDING, SEND_DELAY, MAX_ALERTS = self.keep[:4]
        globals()["search"], globals()["detail"], globals()["notify"] = self.keep[4:]
        return False


def cap_defer_check():
    """Listings over the per-run cap must be queued, not dropped."""
    bikes = [
        {"id": "a", "title": "Surron Light Bee X"},
        {"id": "b", "title": "Talaria Sting"},
        {"id": "c", "title": "79Bike Falcon"},
    ]
    with _sandbox(bikes, lambda text, tries=4: None, max_alerts=1) as box:
        main()
        seen, pending = box.state()
        assert len(seen) == 1, seen
        assert len(pending) == 2, pending          # queued, not lost
        assert not (seen & pending)                # and never in both
        raw = load_json(PENDING, [])
        assert len(raw) == len({str(l["id"]) for l in raw}), raw   # no duplicates queued

        # next run sends the next one and re-queues the rest
        main()
        seen2, pending2 = box.state()
        assert len(seen2) == 2 and len(pending2) == 1, (seen2, pending2)
        # and again, until the queue is empty — never growing
        main()
        seen3, pending3 = box.state()
        assert len(seen3) == 3 and pending3 == set(), (seen3, pending3)


def crash_safety_check():
    """A send that fails must leave that listing queued, not marked seen."""
    bikes = [
        {"id": "a", "title": "Surron Light Bee X"},
        {"id": "b", "title": "Talaria Sting"},
    ]
    sends = []

    def flaky(text, tries=4):
        sends.append(text)
        if len(sends) == 2:
            raise RuntimeError("telegram down")

    with _sandbox(bikes, flaky) as box:
        try:
            main()
        except RuntimeError:
            pass
        seen, pending = box.state()
        assert seen == {"a"}, seen                 # only the delivered one
        assert pending == {"b"}, pending           # the failed one waits its turn


def priority_query_check():
    """Priority brands run every hour; the rest still all come round."""
    for h in range(24):
        picked = query_slice(h)
        assert len(picked) == len(set(picked))
        for q in PRIORITY_QUERIES:
            assert q in picked, (h, picked)
    covered = set()
    for h in range(24):
        covered |= set(query_slice(h))
    assert covered == set(QUERIES) | set(PRIORITY_QUERIES), covered


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--show" in sys.argv:
        show()
    elif "--feedback" in sys.argv:
        feedback()
    else:
        try:
            main()
        except Exception as exc:
            try:
                notify(f"⚠️ <b>watcher failed</b>\n{html.escape(str(exc)[:400])}")
            except Exception:
                pass          # Telegram itself may be what broke; the red run still tells him
            raise
