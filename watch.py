"""Watch Facebook Marketplace for matching listings; Telegram me each new one with a ready-to-paste pitch."""
import html
import json
import os
import pathlib
import sys

import requests

API = "https://api.scrapecreators.com/v1/facebook/marketplace/search"
SEEN = pathlib.Path(__file__).with_name("seen.json")

QUERY = os.environ.get("QUERY", "ebike")
LAT = os.environ.get("LAT", "43.7615")   # North York, Toronto
LNG = os.environ.get("LNG", "-79.4111")
RADIUS_KM = os.environ.get("RADIUS_KM", "60")
MAX_PRICE = os.environ.get("MAX_PRICE", "1500")
PITCH = os.environ.get(
    "PITCH",
    "Hey! Any interest in trading your e-bike for my go-kart? "
    "Runs great, can send pics and video of it going.",
)


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
    r = requests.get(
        API,
        headers={"x-api-key": os.environ["SCRAPECREATORS_KEY"]},
        params={
            "query": QUERY,
            "lat": LAT,
            "lng": LNG,
            "radius_km": RADIUS_KM,
            "max_price": MAX_PRICE,
            "sort_by": "creation_time_descend",
            "date_listed": "last_7_days",
            "availability": "available",
            "count": 30,
        },
        timeout=60,
    )
    r.raise_for_status()
    payload = r.json()
    if isinstance(payload, dict):
        if payload.get("success") is False:
            raise RuntimeError(f"search failed: {payload}")  # else it fails silently as "no listings"
        print(f"credits left: {payload.get('credits_remaining', '?')}")
    return pick_listings(payload)


def card(listing):
    esc = lambda s: html.escape(str(s))
    price = (listing.get("price") or {}).get("formatted_amount", "?")
    where = (listing.get("location") or {}).get("display_name", "?")
    return (
        f"<b>{esc(listing.get('title') or '(no title)')}</b> — {esc(price)}\n"
        f"{esc(where)}\n"
        f"{esc(listing.get('url') or '')}\n\n"
        f"<code>{esc(PITCH)}</code>"
    )


def notify(text):
    r = requests.post(
        f"https://api.telegram.org/bot{os.environ['TG_TOKEN']}/sendMessage",
        json={"chat_id": os.environ["TG_CHAT"], "text": text, "parse_mode": "HTML"},
        timeout=30,
    )
    r.raise_for_status()


def new_ids(listings, seen):
    out = []
    for listing in listings:
        lid = str(listing.get("id") or "")
        if lid and lid not in seen:
            out.append(listing)
            seen.add(lid)
    return out


def main():
    first_run = not SEEN.exists()
    seen = set(json.loads(SEEN.read_text())) if not first_run else set()

    fresh = new_ids(search(), seen)
    SEEN.write_text(json.dumps(sorted(seen)))

    if first_run:
        # ponytail: seed silently so run #1 doesn't dump every standing listing at you
        print(f"seeded {len(fresh)} existing listings, no alerts sent")
        return

    for listing in fresh:
        notify(card(listing))
    print(f"{len(fresh)} new")


def selftest():
    assert pick_listings({"ok": True, "listings": [{"id": "1"}]}) == [{"id": "1"}]
    assert pick_listings([{"id": "1"}]) == [{"id": "1"}]
    assert pick_listings({"ok": True, "tags": ["a", "b"]}) == []
    assert pick_listings({"data": []}) == []
    assert pick_listings("nope") == []

    seen = {"1"}
    assert [l["id"] for l in new_ids([{"id": "1"}, {"id": "2"}, {"id": 2}, {}], seen)] == ["2"]
    assert seen == {"1", "2"}

    assert "&amp;" in card({"title": "Bike & Trailer <used>"})
    assert "<b>" in card({})
    print("ok")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
