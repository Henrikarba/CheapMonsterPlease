#!/usr/bin/env python3
"""
Monster index - collects Monster Energy prices from Estonian grocery e-shops.

Usage:
    python scrape.py                 # scrape all stores, write DB + docs/data.json
    python scrape.py --only rimi     # single store, prints results, writes nothing
    python scrape.py --export        # re-export docs/data.json from existing DB

Every adapter returns a list of Product. Adapters are allowed to fail; one
broken store must never take down the run.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
DB_PATH = ROOT / "prices.sqlite"
OUT_PATH = ROOT / "docs" / "data.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TIMEOUT = 20
DELAY = 1.5  # seconds between requests, be polite


# --------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------

@dataclass
class Product:
    store: str
    name: str
    price: float                      # shelf price, EUR
    url: str = ""
    image: str = ""
    loyalty_price: float | None = None  # price with store card, if different
    volume_l: float | None = None
    ext_id: str = ""
    flavour: str = ""
    zero_sugar: bool = False

    def __post_init__(self):
        if self.volume_l is None:
            self.volume_l = parse_volume(self.name)
        if not self.flavour:
            self.flavour = parse_flavour(self.name)
            self.zero_sugar = is_zero_sugar(self.name)

    @property
    def best_price(self) -> float:
        return min(p for p in (self.price, self.loyalty_price) if p)

    @property
    def per_litre(self) -> float | None:
        if not self.volume_l:
            return None
        return round(self.best_price / self.volume_l, 3)


VOLUME_RE = re.compile(r"(\d+[.,]?\d*)\s*(l|ml|cl)\b", re.I)
MULTIPACK_RE = re.compile(r"(\d+)\s*[x×]\s*(\d+[.,]?\d*)\s*(l|ml|cl)\b", re.I)


def _litres(value: float, unit: str) -> float:
    return {"l": value, "cl": value / 100, "ml": value / 1000}[unit]


def parse_volume(name: str) -> float | None:
    """'Monster Energy Mango Loco 0,5 l' -> 0.5, '... 10x200ml' -> 2.0

    Multipacks are matched first. VOLUME_RE alone takes the 200 ml out of
    '10x200ml' and reports a per-litre price ten times too high, which puts the
    pack at the expensive end of the ladder instead of the cheap end.
    """
    m = MULTIPACK_RE.search(name)
    if m:
        count = int(m.group(1))
        value = float(m.group(2).replace(",", "."))
        return round(count * _litres(value, m.group(3).lower()), 4)
    m = VOLUME_RE.search(name)
    if not m:
        return None
    return _litres(float(m.group(1).replace(",", ".")), m.group(2).lower())


# Searching "monster" in a grocery e-shop also returns Monster High dolls, Hot
# Wheels Monster Trucks, Lego Monster Jam sets and Capri Sun's "Monster Alarm"
# juice. None of them are Monster Energy.
NOT_A_DRINK_RE = re.compile(
    r"nukk|m[äa]nguauto|hot\s*wheels|lego|monster\s*high|monster\s*jam|"
    r"truck|rada|konstr|pusle|kost[üu]{1,2}m|capri\s*sun|monstera",
    re.I,
)
DRINK_RE = re.compile(r"energiajook|en\.?\s*j\w*\.?|energy|jook|drink", re.I)


def is_monster(name: str) -> bool:
    """True only for Monster Energy drinks.

    The toys never carry a drink volume in the title, so a parsed volume is a
    good positive signal; Capri Sun Monster Alarm does carry one, so the brand
    still needs an explicit exclusion.
    """
    low = name.lower()
    if "monster" not in low:
        return False
    if NOT_A_DRINK_RE.search(low):
        return False
    return bool(DRINK_RE.search(low)) or parse_volume(name) is not None


# --------------------------------------------------------------------------
# flavour parsing
#
# Shops put the variant in the product title, so the name is the only signal
# we get. Ordered most specific first - the first regex that matches wins.
# --------------------------------------------------------------------------

FLAVOURS: list[tuple[str, str]] = [
    # Juice line
    ("Mango Loco",            r"mango\s*loco"),
    ("Pipeline Punch",        r"pipeline"),
    ("Pacific Punch",         r"pacific"),
    ("Aussie Lemonade",       r"aussie|lemonade\s*style"),
    ("Khaotic",               r"khaotic|chaotic"),
    ("Monarch",               r"monarch"),
    ("Papillon",              r"papillon"),
    # Ultra line (all zero sugar)
    ("Ultra Paradise",        r"ultra\s*paradise|paradise"),
    ("Ultra Fiesta Mango",    r"ultra\s*fiesta|fiesta\s*mango"),
    ("Ultra Peachy Keen",     r"peachy\s*keen"),
    # Rimi abbreviates to "Ultra Strawb. Dreams", so match the stem
    ("Ultra Strawberry",      r"ultra\s*strawb|strawb\w*\.?\s*dreams"),
    ("Ultra Watermelon",      r"ultra\s*watermelon"),
    ("Ultra Sunrise",         r"ultra\s*sunrise|sunrise"),
    ("Ultra Violet",          r"ultra\s*violet|violet"),
    ("Ultra Rosá",            r"ultra\s*ros|ros[aá]"),
    ("Ultra Gold",            r"ultra\s*gold|gold\b"),
    ("Ultra Blue",            r"ultra\s*blue"),
    ("Ultra Black",           r"ultra\s*black"),
    ("Ultra Red",             r"ultra\s*red"),
    ("Ultra Zero",            r"ultra\s*(white|zero)|zero\s*ultra"),
    ("Ultra Vice Guava",      r"vice\s*guava|ultra\s*vice"),
    # Driver editions must be tested before the bare "ultra" catch-all below:
    # these ship as Ultra variants ("Monster Ultra Lando Norris"), so the
    # catch-all would otherwise swallow them and report a plain "Ultra".
    ("Lando Norris",          r"lando|norris"),
    ("Lewis Hamilton 44",     r"hamilton|\blh\s*44\b"),
    ("The Doctor",            r"the\s*doctor|doctor\s*46|\bvr46\b"),
    ("Ultra",                 r"\bultra\b"),
    # Coffee
    ("Espresso Vanilla",      r"(espresso|java).*vanilla|vanilla.*espresso"),
    ("Salted Caramel",        r"salted\s*caramel"),
    ("Loca Moca",             r"loca\s*moca"),
    ("Mean Bean",             r"mean\s*bean"),
    ("Irish Blend",           r"irish"),
    # Nitro / Rehab / other lines
    ("Nitro Super Dry",       r"super\s*dry"),
    ("Nitro Cosmic Peach",    r"cosmic\s*peach"),
    ("Rehab Tea + Lemonade",  r"rehab.*(tea|lemonade)|tea\s*\+\s*lemonade"),
    ("Rehab Peach",           r"rehab.*peach"),
    ("Rehab",                 r"rehab"),
    ("Bad Apple",             r"bad\s*apple"),
    ("Ripper",                r"ripper"),
    ("Assault",               r"assault"),
    ("Dragon Tea",            r"dragon"),
    ("Punch",                 r"\bpunch\b"),
    ("Zero Sugar",            r"zero\s*sugar|absolutely\s*zero|\bzero\b"),
    ("Original",              r"\b(green|original|classic)\b"),
]

ZERO_SUGAR_RE = re.compile(r"ultra|zero|absolutely|suhkruvaba|sugar\s*free", re.I)

# words that carry no flavour information, stripped before the fallback guess
NOISE_RE = re.compile(
    r"\b(energiajook|energiajoogid|energy\s*drink|energy|monster|drink|jook|"
    r"karboniseeritud|gaseeritud|purk|purgis|can|pakend|kmpl|tk|import|"
    r"\d*\s*-?\s*pakk|mega)\b",
    re.I,
)


def parse_flavour(name: str) -> str:
    low = name.lower()
    for label, pattern in FLAVOURS:
        if re.search(pattern, low):
            return label
    # fallback: whatever is left after removing brand, volume and packaging noise
    rest = VOLUME_RE.sub(" ", name)
    rest = NOISE_RE.sub(" ", rest)
    rest = re.sub(r"[^\w\s\-+ÀÁÂÄÅÕÖÜŠŽàáâäåõöüšž]", " ", rest)
    # leftover pack counts ("4-", "10") are not flavour names
    rest = " ".join(w for w in rest.split() if not w.strip("-+").isdigit())
    rest = rest.strip(" -+")
    return rest.title() if rest else "Original"


def is_zero_sugar(name: str) -> bool:
    return bool(ZERO_SUGAR_RE.search(name))


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept-Language": "et-EE,et;q=0.9,en;q=0.8",
    })
    return s


def money(value) -> float | None:
    """Accepts 3.49, '3,49', '3.49 €', 349 (cents-ish ints are NOT assumed)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    txt = str(value).replace("\xa0", " ").replace("€", "").strip().replace(",", ".")
    m = re.search(r"\d+\.?\d*", txt)
    return round(float(m.group()), 2) if m else None


# --------------------------------------------------------------------------
# adapters
#
# Endpoints below were derived from the public sites and WILL drift. To
# re-verify: open the shop, search "monster", DevTools > Network > Fetch/XHR,
# right click the request > Copy as cURL. If nothing shows up there, the page
# is server-rendered and you parse HTML instead.
# --------------------------------------------------------------------------

def scrape_rimi() -> list[Product]:
    """Rimi ePood is server-rendered. Product cards carry a GTM data attribute
    holding a JSON blob with name and price, which is far more stable than
    scraping the price spans."""
    s = session()
    out: list[Product] = []
    page = 1
    while page <= 5:
        r = s.get("https://www.rimi.ee/epood/ee/otsing",
                  params={"query": "monster", "currentPage": page, "pageSize": 80},
                  timeout=TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        cards = soup.select("[data-gtm-eec-product]")
        if not cards:
            cards = soup.select(".product-grid__item")
        if not cards:
            break

        for card in cards:
            raw = card.get("data-gtm-eec-product")
            name = price = None
            if raw:
                try:
                    d = json.loads(raw)
                    name = d.get("name")
                    price = money(d.get("price"))
                except json.JSONDecodeError:
                    pass
            if not name:
                el = card.select_one(".card__name, .card__url")
                name = el.get_text(strip=True) if el else None
            if not name or not is_monster(name):
                continue
            if price is None:
                eur = card.select_one(".price-tag .major, .price__integer")
                cent = card.select_one(".price-tag sup, .price__decimal")
                if eur:
                    price = money(f"{eur.get_text(strip=True)}."
                                  f"{cent.get_text(strip=True) if cent else '00'}")
            if price is None:
                continue

            link = card.select_one("a[href]")
            img = card.select_one("img")
            out.append(Product(
                store="Rimi",
                name=name,
                price=price,
                url=("https://www.rimi.ee" + link["href"]) if link and link["href"].startswith("/") else (link["href"] if link else ""),
                image=(img.get("src") or img.get("data-src") or "") if img else "",
            ))
        page += 1
        time.sleep(DELAY)
    return out


def scrape_selver() -> list[Product]:
    """Selver runs a Vue Storefront front end backed by an Elasticsearch proxy,
    so the catalogue is queryable as JSON. Two traps live in this index:

    - `name` is not analysed for full text. {"match": {"name": "monster"}}
      returns 200 OK with zero hits, which looks exactly like "Selver stocks no
      Monster". query_string across all fields is what actually matches.
    - every bare price field is ex-VAT: `price` reads 1.3629 where the shelf
      says 1.69. Only the *_incl_tax fields are comparable to the other shops,
      and using the wrong one hands Selver the ladder on a VAT artefact.

    If this ever 404s, fall back to parsing https://www.selver.ee/search?q=monster.
    """
    s = session()
    body = {
        "query": {"query_string": {"query": "monster"}},
        "size": 100,
    }
    r = s.post(
        "https://www.selver.ee/api/catalog/vue_storefront_catalog_et/product/_search",
        json=body, timeout=TIMEOUT,
    )
    r.raise_for_status()
    hits = r.json().get("hits", {}).get("hits", [])

    out: list[Product] = []
    for h in hits:
        d = h.get("_source", {})
        name = d.get("name") or ""
        if not is_monster(name):
            continue
        # shelf price, then the current campaign price if one is running
        base = money(d.get("original_price_incl_tax")) or money(d.get("regular_price"))
        final = money(d.get("final_price_incl_tax"))
        if base is None and final is None:
            continue
        out.append(Product(
            store="Selver",
            name=name,
            price=base if base is not None else final,
            loyalty_price=final if (final and base and final < base) else None,
            url=f"https://www.selver.ee/{d.get('url_key', '')}",
            image=d.get("image", ""),
            ext_id=str(d.get("sku", "")),
        ))
    return out


def scrape_prisma() -> list[Product]:
    """Prisma's assortment is per-store. Find the store id in the cookie the
    site sets after you pick a shop, then look for the search XHR in DevTools
    and fill this in."""
    raise NotImplementedError("prisma: endpoint not wired up yet")


def scrape_coop() -> list[Product]:
    """ecoop.ee is regional - prices differ per Coop unit, so this adapter
    needs a store selector before it means anything."""
    raise NotImplementedError("coop: endpoint not wired up yet")


ADAPTERS = {
    "rimi": scrape_rimi,
    "selver": scrape_selver,
    "prisma": scrape_prisma,
    "coop": scrape_coop,
}

# Display name per adapter. Needed for the run report: an adapter that returns
# nothing leaves no Product to read the store name off.
STORE_NAMES = {
    "rimi": "Rimi",
    "selver": "Selver",
    "prisma": "Prisma",
    "coop": "Coop",
}


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS observation (
    id            INTEGER PRIMARY KEY,
    seen_at       TEXT NOT NULL,
    store         TEXT NOT NULL,
    name          TEXT NOT NULL,
    flavour       TEXT,
    zero_sugar    INTEGER,
    ext_id        TEXT,
    price         REAL NOT NULL,
    loyalty_price REAL,
    volume_l      REAL,
    per_litre     REAL,
    url           TEXT,
    image         TEXT
);
CREATE INDEX IF NOT EXISTS idx_obs_lookup ON observation (store, name, seen_at);

-- One row per adapter per run. Without this a store that returns nothing is
-- indistinguishable from a store that was never asked, both here and on the
-- page: it simply has no observations and vanishes.
CREATE TABLE IF NOT EXISTS source_status (
    id       INTEGER PRIMARY KEY,
    seen_at  TEXT NOT NULL,
    adapter  TEXT NOT NULL,
    store    TEXT NOT NULL,
    status   TEXT NOT NULL,   -- ok | empty | failed | stub
    count    INTEGER NOT NULL,
    detail   TEXT
);
CREATE INDEX IF NOT EXISTS idx_src_lookup ON source_status (adapter, seen_at);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    return conn


def save(conn: sqlite3.Connection, products: list[Product]) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.executemany(
        """INSERT INTO observation
           (seen_at, store, name, flavour, zero_sugar, ext_id,
            price, loyalty_price, volume_l, per_litre, url, image)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(now, p.store, p.name, p.flavour, int(p.zero_sugar), p.ext_id,
          p.price, p.loyalty_price, p.volume_l, p.per_litre, p.url, p.image)
         for p in products],
    )
    conn.commit()


def save_status(conn: sqlite3.Connection, report: list[dict]) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.executemany(
        """INSERT INTO source_status (seen_at, adapter, store, status, count, detail)
           VALUES (?,?,?,?,?,?)""",
        [(now, r["adapter"], r["store"], r["status"], r["count"], r.get("detail"))
         for r in report],
    )
    conn.commit()


def latest_status(conn: sqlite3.Connection) -> list[dict]:
    """Most recent outcome per adapter, in ADAPTERS order."""
    rows = conn.execute("""
        SELECT adapter, store, status, count, detail, MAX(seen_at) AS seen_at
        FROM source_status GROUP BY adapter
    """).fetchall()
    cols = ["adapter", "store", "status", "count", "detail", "seen_at"]
    by_adapter = {r[0]: dict(zip(cols, r)) for r in rows}
    order = list(ADAPTERS)
    return sorted(by_adapter.values(),
                  key=lambda r: order.index(r["adapter"]) if r["adapter"] in order else 99)


def export(conn: sqlite3.Connection) -> dict:
    """Latest snapshot per (store, name) plus a 90-day price history."""
    rows = conn.execute("""
        SELECT store, name, flavour, zero_sugar, ext_id, price, loyalty_price,
               volume_l, per_litre, url, image, MAX(seen_at) AS seen_at
        FROM observation
        GROUP BY store, name
        ORDER BY per_litre IS NULL, per_litre
    """).fetchall()

    cols = ["store", "name", "flavour", "zero_sugar", "ext_id", "price",
            "loyalty_price", "volume_l", "per_litre", "url", "image", "seen_at"]
    items = [dict(zip(cols, r)) for r in rows]
    for it in items:
        it["zero_sugar"] = bool(it["zero_sugar"])

    history = {}
    for it in items:
        key = f"{it['store']}|{it['name']}"
        h = conn.execute("""
            SELECT DATE(seen_at) AS d, MIN(COALESCE(loyalty_price, price)) AS p
            FROM observation
            WHERE store = ? AND name = ? AND seen_at >= DATE('now', '-90 days')
            GROUP BY d ORDER BY d
        """, (it["store"], it["name"])).fetchall()
        if len(h) > 1:
            history[key] = [{"date": d, "price": p} for d, p in h]

    payload = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": latest_status(conn),
        "items": items,
        "history": history,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # explicit encoding: write_text() would otherwise use the locale codepage,
    # which on Windows is cp1252 and turns "Ultra Rosá" into mojibake once the
    # browser reads data.json back as UTF-8
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    return payload


# --------------------------------------------------------------------------

def run(only: str | None) -> tuple[list[Product], list[dict]]:
    """Returns the products plus one status row per adapter.

    A wired adapter that returns zero is reported as "empty", not as success:
    Selver once did exactly that for a broken query and nothing anywhere said so.
    """
    names = [only] if only else list(ADAPTERS)
    found: list[Product] = []
    report: list[dict] = []
    for name in names:
        fn = ADAPTERS.get(name)
        if fn is None:
            print(f"unknown store: {name}", file=sys.stderr)
            continue
        store = STORE_NAMES.get(name, name.title())
        try:
            items = fn()
            found.extend(items)
            if items:
                print(f"{name:8s} {len(items):3d} products")
                report.append({"adapter": name, "store": store,
                               "status": "ok", "count": len(items)})
            else:
                print(f"{name:8s}   0 products - adapter is wired, so this is "
                      f"probably drift", file=sys.stderr)
                report.append({"adapter": name, "store": store,
                               "status": "empty", "count": 0,
                               "detail": "adapter returned no products"})
        except NotImplementedError as e:
            print(f"{name:8s} skipped ({e})")
            report.append({"adapter": name, "store": store,
                           "status": "stub", "count": 0, "detail": str(e)})
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"
            print(f"{name:8s} FAILED: {detail}", file=sys.stderr)
            report.append({"adapter": name, "store": store,
                           "status": "failed", "count": 0, "detail": detail})
        time.sleep(DELAY)
    return found, report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="run a single store adapter, do not save")
    ap.add_argument("--export", action="store_true",
                    help="rebuild docs/data.json from the existing database")
    args = ap.parse_args()

    if args.export:
        payload = export(connect())
        print(f"exported {len(payload['items'])} products")
        return 0

    products, report = run(args.only)

    if args.only:
        for p in sorted(products, key=lambda x: (x.per_litre is None, x.per_litre)):
            print(f"  {str(p.per_litre or '-'):>6}  {p.best_price:>5.2f}  "
                  f"{p.flavour:<22}{'0' if p.zero_sugar else ' '}  {p.name}")
        return 0

    if not products:
        print("nothing collected, leaving the database alone", file=sys.stderr)
        return 1

    conn = connect()
    save(conn, products)
    save_status(conn, report)
    export(conn)
    print(f"saved {len(products)} observations")

    # Deliberately still exit 0 when a store is down: the other stores' prices
    # are worth publishing, and failing here would stop the workflow before it
    # commits them. The degraded source is carried in data.json and shown on
    # the page instead.
    broken = [r for r in report if r["status"] in ("empty", "failed")]
    if broken:
        print("degraded: " + ", ".join(f"{r['adapter']} ({r['status']})"
                                       for r in broken), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
