"""Watch Facebook Marketplace for e-bikes whose sellers look open to a trade, and Telegram them to me.

Ranking, not just filtering: sellers who say they'll trade go out first and flagged,
sellers who say cash-only or no-trades are dropped entirely.
"""
import html
import json
import os
import pathlib
import sys
import time

import requests

SEARCH_API = "https://api.scrapecreators.com/v1/facebook/marketplace/search"
ITEM_API = "https://api.scrapecreators.com/v1/facebook/marketplace/item"
SEEN = pathlib.Path(__file__).with_name("seen.json")

# Each query returns a different rotating slice, so several widen coverage a lot.
QUERIES = [
    q.strip()
    for q in os.environ.get(
        "QUERIES",
        "surron,talaria,e ride pro,79bike,rfn apollo,segway dirt bike,"
        "electric dirt bike,altis motors,rawrr,ventus",
    ).split(",")
    if q.strip()
]
LAT = os.environ.get("LAT", "43.7615")   # North York, Toronto
LNG = os.environ.get("LNG", "-79.4111")
RADIUS_KM = os.environ.get("RADIUS_KM", "100")
MAX_PRICE = os.environ.get("MAX_PRICE", "15000")   # Sur-Ron/Talaria class runs CA$3.5k-10k
PITCH = os.environ.get(
    "PITCH",
    "Hey! Any interest in trading your e-bike for my go-kart? "
    "Runs great, can send pics and video of it going.",
)

# Brand/model allowlist. The API keyword-matches loosely, so "ventus" returns golf
# shafts and "rawrr" returns sweaters — an allowlist kills that permanently where a
# blocklist would be endless whack-a-mole. Title must hit one of these.
REQUIRE = [
    w.strip().lower()
    for w in os.environ.get(
        "REQUIRE",
        # the brands you named
        "surron,sur-ron,sur ron,talaria,e ride pro,eride pro,e-ride pro,eride,"
        "altis,rawrr,79bike,79 bike,ventus,rfn,apollo,segway,"
        # their models
        "light bee,lightbee,ultra bee,storm bee,lbx,sting,mantis,mx3,mx4,mx5,"
        "x3,3x,falcon,warthog,xxx,"
        # other electric dirt bike makes that show up on Marketplace
        "kuberg,onyx,volcon,stark varg,delfast,dust moto,kalk,razor mx,ridstar,"
        "valtinsu,gt54,gt73,gt7,emmo,rooder,evoque,arctic leopard,sozo,m2s,zoombike,"
        "electric motion,bultaco,flux,phatmoto,venom,ebroh,nitro,tromox,"
        "strike,shadow sx,heybike,villain,yozma,kukirin,weped,emoto,e-moto,e moto,"
        # generic phrasing, for makes not listed above
        "electric dirt bike,electric dirtbike,e dirt bike,edirt,dirt ebike,"
        "dirt e-bike,electric motocross,electric enduro,electric pit bike,"
        "emx,electric moto,ebike dirt,e-bike dirt,dirt bike,dirtbike,pit bike",
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
        "for parts,parts only,parts,battery only,charger only,key switch,switch,"
        "sprocket,fender,decal,sticker,kickstand,helmet,cover,seat only,"
        "plastics only,shaft,sweater,for rent,rental",
    ).split(",")
    if w.strip()
]

# Seller has ruled a trade out — don't waste a message on them.
NO_TRADE = [
    p.strip().lower()
    for p in os.environ.get(
        "NO_TRADE",
        "no trades,no trade,not trade,not trading,no swap,no swaps,no trades please,"
        "cash only,cash on pickup only,cash and pickup only,not interested in trade",
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

DESC_CHARS = int(os.environ.get("DESC_CHARS", "400"))
SEND_DELAY = float(os.environ.get("SEND_DELAY", "1.2"))  # Telegram allows ~1 msg/sec per chat


def call(url, params):
    r = requests.get(
        url,
        headers={"x-api-key": os.environ["SCRAPECREATORS_KEY"]},
        params=params,
        timeout=60,
    )
    r.raise_for_status()
    payload = r.json()
    if isinstance(payload, dict) and payload.get("success") is False:
        raise RuntimeError(f"api call failed: {payload}")  # else it reads as "nothing found"
    return payload


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
    """Every query, deduped by listing id. One credit per query."""
    found, credits = {}, None
    for query in QUERIES:
        payload = call(
            SEARCH_API,
            {
                "query": query,
                "lat": LAT,
                "lng": LNG,
                "radius_km": RADIUS_KM,
                "max_price": MAX_PRICE,
                "sort_by": "creation_time_descend",
                "date_listed": "last_7_days",
                "availability": "available",
            },
        )
        listings = pick_listings(payload)
        if isinstance(payload, dict):
            credits = payload.get("credits_remaining", credits)
        for listing in listings:
            if listing.get("id"):
                found.setdefault(str(listing["id"]), listing)
        print(f"  query {query!r}: {len(listings)}")
    print(f"{len(found)} unique across {len(QUERIES)} queries, credits left: {credits}")
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


def trade_signal(*texts):
    """('no'|'yes'|None, matched phrase) — read from title AND description.

    Negatives are checked first because they contain the positives: "no trades"
    would otherwise match "trade" and get promoted instead of dropped.
    """
    blob = " ".join(t for t in texts if t).lower()
    for phrase in NO_TRADE:
        if phrase in blob:
            return "no", phrase
    for phrase in TRADE_OK:
        if phrase in blob:
            return "yes", phrase
    return None, None


def card(listing, info=None, hit=None):
    info = info or {}
    esc = lambda s: html.escape(str(s))

    price = (listing.get("price") or {}).get("formatted_amount", "?")
    was = (info.get("strikethrough_price") or {}).get("formattedAmountWithoutDecimals")
    price_line = f"{esc(price)} <s>{esc(was)}</s>" if was else esc(price)

    bits = [
        info.get("location_text") or (listing.get("location") or {}).get("display_name"),
        next(
            (
                a.get("label")
                for a in info.get("attributes") or []
                if a.get("attribute_name") == "Condition"
            ),
            None,
        ),
        info.get("listing_date_text"),
    ]
    meta = " · ".join(esc(b) for b in bits if b)

    desc = (info.get("description") or "").strip()
    if len(desc) > DESC_CHARS:
        desc = desc[:DESC_CHARS].rstrip() + "…"

    lines = []
    if hit:
        lines.append(f"🔥 <b>OPEN TO TRADES</b> — said {esc(hit)!s}")
    lines.append(f"<b>{esc(listing.get('title') or '(no title)')}</b> — {price_line}")
    if meta:
        lines.append(meta)
    lines.append(esc(listing.get("url") or ""))
    if desc:
        lines.append(f"\n{esc(desc)}")
    lines.append(f"\n<code>{esc(PITCH)}</code>")
    return "\n".join(lines)


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
        return
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


def triage(fresh, get_detail=None):
    """(sorted alerts, skip log). Trade-friendly first; explicit refusers dropped."""
    get_detail = get_detail or detail   # resolved here, not at def time, so it's swappable
    alerts, skips = [], []
    for listing in fresh:
        title = listing.get("title")
        nope = rejected(title)
        if nope:
            skips.append((listing, nope))
            continue
        info = get_detail(listing["id"])  # credit spent only past the title filter
        verdict, phrase = trade_signal(title, info.get("description"))
        if verdict == "no":
            skips.append((listing, f"refuses:{phrase}"))
            continue
        alerts.append((verdict == "yes", listing, info, phrase if verdict == "yes" else None))
    alerts.sort(key=lambda a: not a[0])  # trade-friendly first
    return alerts, skips


def save(seen):
    SEEN.write_text(json.dumps(sorted(seen)))


def main():
    first_run = not SEEN.exists()
    seen = set() if first_run else set(json.loads(SEEN.read_text()))

    fresh = new_ids(search(), seen)

    if first_run:
        # ponytail: seed silently so run #1 doesn't dump every standing listing at you
        save(seen | {str(l["id"]) for l in fresh})
        print(f"seeded {len(fresh)} existing listings, no alerts sent")
        return

    alerts, skips = triage(fresh)
    for listing, why in skips:
        print(f"skip {listing['id']} ({why}): {listing.get('title')}")
        seen.add(str(listing["id"]))

    sent = hot = 0
    try:
        for is_hot, listing, info, hit in alerts:
            notify(card(listing, info, hit))
            # Recorded only after the send lands, so a rate-limit crash re-alerts the
            # stragglers next run instead of burying them as already-seen.
            seen.add(str(listing["id"]))
            sent += 1
            hot += bool(is_hot)
            time.sleep(SEND_DELAY)
    finally:
        save(seen)
    print(f"{len(fresh)} new, {sent} sent ({hot} trade-friendly), {len(skips)} skipped")


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
    # real bikes the first, narrower allowlist was throwing away
    assert rejected("2025 Strike Shadow SX 60V") is None
    assert rejected("HeyBike Villain TRADE") is None
    assert rejected("Gt73Pro *MODDED* (check description)") is None
    assert rejected("Dirt Bikes Available Yozma heybike jasion") is None
    assert rejected(None) == "not a known model"

    # negatives must beat positives: these all contain the word "trade"
    assert trade_signal("Ebike NO TRADES")[0] == "no"
    assert trade_signal("nice ebike", "not trading, cash only")[0] == "no"
    assert trade_signal("36V e-bike CASH ONLY (willing to negotiate)")[0] == "no"
    assert trade_signal("ACCEPTING TRADES 48V 500W ebike") == ("yes", "accepting trades")
    assert trade_signal("ebike", "open to trades for a dirt bike")[0] == "yes"
    assert trade_signal("plain ebike", "good condition") == (None, None)
    assert trade_signal(None, None) == (None, None)

    # trade-friendly sorts ahead of neutral, refusers and junk never appear
    fresh = [
        {"id": "n", "title": "2024 Talaria Sting"},
        {"id": "x", "title": "Fujikura Ventus shaft"},
        {"id": "y", "title": "Surron Light Bee X", "price": {"formatted_amount": "CA$6,099"}},
        {"id": "r", "title": "Sur-Ron Ultra Bee cash only"},
    ]
    details = {"n": {}, "y": {"description": "trades welcome"}, "r": {}}
    alerts, skips = triage(fresh, get_detail=lambda i: details.get(i, {}))
    assert [a[1]["id"] for a in alerts] == ["y", "n"], alerts
    assert sorted(s[1].split(":")[0] for s in skips) == ["part", "refuses"]

    hot = card(fresh[2], details["y"], "trades welcome")
    assert hot.startswith("🔥") and "OPEN TO TRADES" in hot
    assert "<code>" in hot and "CA$6,099" in hot

    full = card(
        {"title": "Bike & Trailer <used>", "price": {"formatted_amount": "CA$350"}, "url": "u"},
        {
            "description": "throttle doesn't work",
            "location_text": "Vaughan, ON",
            "listing_date_text": "Listed 3 weeks ago",
            "strikethrough_price": {"formattedAmountWithoutDecimals": "CA$400"},
            "attributes": [{"attribute_name": "Condition", "label": "Used - Good"}],
        },
    )
    assert "&amp;" in full and "&lt;used&gt;" in full          # escaped, not raw HTML
    assert "<s>CA$400</s>" in full
    assert "Vaughan, ON · Used - Good · Listed 3 weeks ago" in full
    assert not full.startswith("🔥")

    assert "…" in card({"title": "t"}, {"description": "x" * (DESC_CHARS + 50)})
    assert "<b>" in card({})                                   # survives an empty listing

    crash_safety_check()
    print("ok")


def crash_safety_check():
    """A send that fails must leave that listing unseen, so the next run retries it."""
    import tempfile

    global SEEN, SEND_DELAY
    keep = (SEEN, SEND_DELAY, globals()["search"], globals()["detail"], globals()["notify"])
    try:
        SEEN = pathlib.Path(tempfile.mkdtemp()) / "seen.json"
        SEEN.write_text("[]")          # exists, so not a seeding run
        SEND_DELAY = 0
        globals()["search"] = lambda: [
            {"id": "a", "title": "Surron Light Bee X"},
            {"id": "b", "title": "Talaria Sting"},
        ]
        globals()["detail"] = lambda _id: {}

        sends = []

        def flaky(text, tries=4):
            sends.append(text)
            if len(sends) == 2:
                raise RuntimeError("telegram down")

        globals()["notify"] = flaky
        try:
            main()
        except RuntimeError:
            pass
        saved = set(json.loads(SEEN.read_text()))
        assert saved == {"a"}, saved    # "b" never sent, so it must not be recorded
    finally:
        SEEN, SEND_DELAY = keep[0], keep[1]
        globals()["search"], globals()["detail"], globals()["notify"] = keep[2], keep[3], keep[4]


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
