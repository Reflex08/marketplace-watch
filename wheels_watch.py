"""Watch Facebook Marketplace for aftermarket wheels and alert on the ones worth buying.

Separate purpose from watch.py (go-kart/e-bike trades): no trade logic here, no
dealer rule - a listing either looks like a real deal for its brand/size/condition
or it doesn't. That judgment isn't a regex problem the way "no trades" is, so unlike
watch.py this pays for one cheap LLM call per survivor to render a verdict, and only
alerts on GOOD_DEAL. Shares watch.py's HTTP/Telegram plumbing and the same
ScrapeCreators/Telegram/Anthropic credentials - it draws from the same credit
balance, so it is deliberately narrow (few queries, hard price floor, LLM gate)
rather than a wide net.
"""
import html
import json
import os
import pathlib
import sys

import requests

from watch import _STATE_DIR, call, load_json, notify, price_of

esc = lambda s: html.escape(str(s))

SEARCH_API = "https://api.scrapecreators.com/v1/facebook/marketplace/search"
ITEM_API = "https://api.scrapecreators.com/v1/facebook/marketplace/item"

SEEN = _STATE_DIR / "wheels_seen.json"
LAT = os.environ.get("LAT", "43.7615")   # North York, Toronto
LNG = os.environ.get("LNG", "-79.4111")
RADIUS_KM = float(os.environ.get("WHEELS_RADIUS_KM", "200"))

# One query per brand rather than a generic "wheels" search - "wheels" alone returns
# strollers, wagons, office chairs and toy cars in roughly that volume.
QUERIES = [
    q.strip() for q in os.environ.get(
        "WHEELS_QUERIES",
        "bmw wheels,mercedes wheels,audi wheels,vossen wheels,niche wheels,"
        "hre wheels,rotiform wheels,bbs wheels,work wheels,advan wheels,"
        "forgestar wheels,enkei wheels,rays wheels,volk wheels,"
        "staggered wheels,forged wheels",
    ).split(",") if q.strip()
]
QUERIES_PER_RUN = int(os.environ.get("WHEELS_QUERIES_PER_RUN", "4"))

# Car-brand words (bmw, mercedes...) are not enough alone - "2019 BMW 328i for sale"
# would pass and burn a credit and an LLM call on a whole car. A wheel/rim word must
# ALSO be present; the wheel-brand names (vossen, hre...) don't have that ambiguity
# and qualify by themselves.
CAR_BRANDS = "bmw,mercedes,audi".split(",")
# Distinctive names qualify bare. "work", "fuel", "method" and "rays" are common
# English words on their own (work boots, jerry can fuel, rays of sun) - those are
# qualified with "wheels"/"wheel" the same way the bike bot learned to write
# "onyx rcr" instead of bare "onyx".
WHEEL_BRANDS = ("vossen,niche,hre,rotiform,bbs,advan,forgestar,"
                "enkei,volk,konig,xxr,ace alloy,motegi,asanti,"
                "work wheels,work wheel,fuel wheels,fuel wheel,"
                "method wheels,method wheel,rays wheels,rays wheel").split(",")
WHEEL_WORDS = "wheels,rims,wheel,rim,staggered,forged".split(",")
# Unambiguous "not a wheel set" phrases. Wheel LOCKS and CAPS are parts, not wheels;
# a stroller/wagon/office-chair "wheels" title is the noise this list exists for.
EXCLUDE = [
    "tires only,tire only,tpms only,lug nut,lug nuts,wheel lock,center cap,"
    "hub cap,hubcap,spacer,spacers,adapter,adapters,valve stem,"
    "stroller,wagon,office chair,shopping cart,skateboard,rollerblade,"
    "bike wheel,bicycle wheel,toy car,rc car,for parts,parts only"
][0].split(",")

# Hard floor/ceiling, checked free off the search result. Not the deal judgment
# itself - just a sanity band so a $40 hubcap or a $60k car with "BMW" in the
# title never reaches a paid description read.
MIN_PRICE = float(os.environ.get("WHEELS_MIN_PRICE", "300"))
MAX_PRICE = float(os.environ.get("WHEELS_MAX_PRICE", "6000"))

MAX_CHECKS = int(os.environ.get("WHEELS_MAX_CHECKS", "6"))   # paid description reads/run
MAX_ALERTS = int(os.environ.get("WHEELS_MAX_ALERTS", "4"))


def rejected(title):
    """Title-only, free. Same allowlist-then-blocklist shape as watch.py."""
    blob = (title or "").lower()
    has_wheel_word = any(w in blob for w in WHEEL_WORDS)
    qualifies = any(w in blob for w in WHEEL_BRANDS) or (
        has_wheel_word and any(w in blob for w in CAR_BRANDS)
    )
    if not qualifies:
        return "not a known wheel listing"
    hit = next((w for w in EXCLUDE if w in blob), None)
    if hit:
        return f"excluded:{hit}"
    return None


def shortlist(fresh):
    """Free pass: (queue, skips). No credits spent here."""
    queue, skips = [], []
    for listing in fresh:
        nope = rejected(listing.get("title"))
        if nope:
            skips.append((listing, nope))
            continue
        price = price_of(listing)
        if price is not None and price < MIN_PRICE:
            skips.append((listing, f"under {MIN_PRICE:g} (CA${price:g})"))
            continue
        if price is not None and price > MAX_PRICE:
            skips.append((listing, f"over {MAX_PRICE:g} (CA${price:g})"))
            continue
        queue.append(listing)
    return queue, skips


DEAL_TOOL = {
    "name": "judge",
    "description": "Judge whether a wheel listing is a good deal for its brand, size and condition.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["good_deal", "fair", "overpriced", "unclear"]},
            "reason": {"type": "string", "description": "One short sentence: why. "
                                                          "Name the brand/size if known."},
        },
        "required": ["verdict", "reason"],
    },
}


def judge_deal(title, price, description):
    """Ask the model whether this is a good deal. Costs Anthropic tokens, not
    ScrapeCreators credits - only called for listings that already passed the free
    filter and the paid description read, so it's the last and rarest expense."""
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": os.environ.get("DEAL_MODEL", "claude-haiku-4-5-20251001"),
            "max_tokens": 300,
            "system": (
                "You price used aftermarket/OEM wheel sets from Facebook Marketplace "
                "listings (BMW, Mercedes, Audi, Vossen, Niche, HRE, Rotiform, BBS, and "
                "similar). Given a title, asking price in CAD, and description, judge "
                "whether the price is a good deal against typical resale value for that "
                "brand, size, and stated condition. Consider: fitment/size mentioned, "
                "number of wheels (a set of 4 vs a single), curb rash or damage noted, "
                "whether tires are included. If the brand or size isn't identifiable, "
                "say verdict 'unclear' rather than guessing."
            ),
            "messages": [{"role": "user", "content": (
                f"Title: {title}\nAsking price: CA${price:g}\n"
                f"Description: {(description or '(none given)')[:1500]}"
            )}],
            "tools": [DEAL_TOOL],
            "tool_choice": {"type": "tool", "name": "judge"},
        },
        timeout=30,
    )
    r.raise_for_status()
    for block in r.json()["content"]:
        if block.get("type") == "tool_use":
            return block["input"]["verdict"], block["input"]["reason"]
    return "unclear", "model gave no verdict"


def card(listing, verdict, reason):
    price = (listing.get("price") or {}).get("formatted_amount", "?")
    where = (listing.get("location") or {}).get("display_name")
    head = f"<b>{esc(listing.get('title') or '(no title)')}</b> - {esc(price)}"
    if where:
        head += f" | {esc(where)}"
    tag = "GOOD DEAL" if verdict == "good_deal" else verdict.upper()
    return "\n".join([
        f"\U0001FA78 <b>{tag}</b> - {esc(reason)}",
        head,
        esc(listing.get("url") or ""),
    ])


def search():
    queries = QUERIES[:QUERIES_PER_RUN] if QUERIES_PER_RUN else QUERIES
    found, credits = {}, None
    for query in queries:
        try:
            payload = call(SEARCH_API, {
                "query": query, "lat": LAT, "lng": LNG, "radius_km": RADIUS_KM,
                "max_price": MAX_PRICE, "min_price": MIN_PRICE,
                "sort_by": "creation_time_descend", "date_listed": "all",
                "availability": "available",
            })
        except Exception as exc:
            print(f"  query {query!r} FAILED: {exc}")
            continue
        listings = payload.get("listings") or payload.get("results") or []
        if not isinstance(listings, list):
            listings = []
        for listing in listings:
            if listing.get("id"):
                found.setdefault(str(listing["id"]), listing)
        credits = payload.get("credits_remaining", credits)
        print(f"  query {query!r}: {len(listings)}")
    print(f"{len(found)} unique from {queries}, credits left: {credits}")
    return list(found.values())


def detail(listing_id):
    try:
        got = call(ITEM_API, {"id": listing_id})
        return got if isinstance(got, dict) else {}
    except Exception as exc:
        print(f"detail failed for {listing_id}: {exc}")
        return {}


def main():
    seen = load_json(SEEN, {})
    fresh = [l for l in search() if str(l.get("id")) not in seen]
    queue, skips = shortlist(fresh)
    batch = queue[:MAX_CHECKS]

    sent = 0
    try:
        for listing in batch:
            lid = str(listing["id"])
            info = detail(lid)
            desc = info.get("description")
            price = price_of(listing) or 0
            verdict, reason = judge_deal(listing.get("title"), price, desc)
            if verdict == "good_deal":
                if sent < MAX_ALERTS:
                    notify(card(listing, verdict, reason))
                    seen[lid] = "sent"
                    sent += 1
                # else: a genuine deal that just missed the cap. Left OUT of seen
                # on purpose - marking it "good_deal" here would bury it forever
                # next to real rejects. Leaving it unseen means next run re-reads
                # and re-judges it (one more paid call), but it still reaches you.
            else:
                seen[lid] = verdict
    finally:
        SEEN.write_text(json.dumps(seen))

    for listing, why in skips:
        seen.setdefault(str(listing["id"]), why)
    SEEN.write_text(json.dumps(seen))
    print(f"{len(fresh)} unseen, {len(batch)} read, {sent} sent, "
          f"{len(skips)} skipped free")


def selftest():
    """Offline logic check, no network, no credits."""
    assert rejected("2020 BMW M3 19in wheels staggered set") is None
    assert rejected("Vossen HF5 20x9 wheels") is None
    assert rejected("Baby stroller with wheels, great condition") == "not a known wheel listing"
    assert rejected("BMW wheel locks, set of 4") == "excluded:wheel lock"
    assert rejected("Office chair wheels x5") == "not a known wheel listing"
    assert rejected("2019 BMW 328i for sale, 60k km") == "not a known wheel listing"
    # bare brand words that are also common English words must not qualify alone
    assert rejected("Looking for work, will do any job") == "not a known wheel listing"
    assert rejected("Jerry can, 5 gallon, for fuel") == "not a known wheel listing"
    assert rejected("New method for organizing your garage") == "not a known wheel listing"
    assert rejected("Work wheels 18in staggered") is None
    assert rejected("Fuel wheels off-road 20in") is None

    q, s = shortlist([
        {"id": "a", "title": "Vossen wheels", "price": {"amount": 150}},
        {"id": "b", "title": "HRE wheels", "price": {"amount": 4200}},
        {"id": "c", "title": "BMW wheels", "price": {"amount": 9000}},
    ])
    assert [l["id"] for l in q] == ["b"], q
    assert s[0][1].startswith("under 300") and s[1][1].startswith("over 6000"), s

    def fake_card():
        return card(
            {"title": "Vossen CV3 19in staggered", "url": "http://x",
             "price": {"formatted_amount": "CA$1,200"},
             "location": {"display_name": "Vaughan, ON"}},
            "good_deal", "Vossen forged set well under typical CA$2k resale",
        )
    rendered = fake_card()
    assert rendered.count("\n") == 2 and "GOOD DEAL" in rendered, rendered

    cap_spillover_check()
    print("ok")


def cap_spillover_check():
    """A good_deal beyond MAX_ALERTS must NOT be marked seen - it has to survive
    to the next run, not vanish next to real rejects. Regression test for a bug
    caught on the first live run: 5 good_deal verdicts, cap 4, and the 5th got
    permanently buried before this fix."""
    import tempfile

    global SEEN, MAX_ALERTS, MAX_CHECKS
    keep_seen, keep_alerts, keep_checks = SEEN, MAX_ALERTS, MAX_CHECKS
    keep_search, keep_detail, keep_judge, keep_notify = (
        globals()["search"], globals()["detail"],
        globals()["judge_deal"], globals()["notify"],
    )
    try:
        SEEN = pathlib.Path(tempfile.mkdtemp()) / "wheels_seen.json"
        MAX_ALERTS, MAX_CHECKS = 1, 5
        listings = [{"id": str(i), "title": f"Vossen wheels {i}",
                     "price": {"amount": 1000}} for i in range(3)]
        globals()["search"] = lambda: listings
        globals()["detail"] = lambda _id: {"description": "great condition"}
        globals()["judge_deal"] = lambda *a: ("good_deal", "under market")
        globals()["notify"] = lambda text, tries=4: None
        main()
        seen = json.loads(SEEN.read_text())
        assert sum(1 for v in seen.values() if v == "sent") == 1, seen
        # the spillover ids must be ABSENT, not recorded as "good_deal"
        assert "good_deal" not in seen.values(), seen
        assert len(seen) == 1, seen   # only the sent one is recorded at all
    finally:
        SEEN, MAX_ALERTS, MAX_CHECKS = keep_seen, keep_alerts, keep_checks
        globals()["search"], globals()["detail"] = keep_search, keep_detail
        globals()["judge_deal"], globals()["notify"] = keep_judge, keep_notify


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        try:
            main()
        except Exception as exc:
            try:
                notify(f"⚠ <b>wheels watcher failed</b>\n{html.escape(str(exc)[:400])}")
            except Exception:
                pass
            raise
