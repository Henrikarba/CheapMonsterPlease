#!/usr/bin/env python3
"""
Monster index - collects Monster Energy prices from Estonian grocery e-shops.

Usage:
    python scrape.py                 # scrape all stores, write docs/data.json
    python scrape.py --only rimi     # single store, prints results, writes nothing

Every adapter returns a list of Product. Adapters are allowed to fail; one
broken store must never take down the run.

Each run publishes a fresh snapshot and keeps nothing from the previous one.
There is no database and no price history: a product a shop stops listing is
gone from the page on the next run rather than lingering at a price nobody
charges any more.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
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
MULTIPACK_RE = re.compile(r"(\d+)\s*[x×*]\s*(\d+[.,]?\d*)\s*(l|ml|cl)\b", re.I)


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
    r"truck|rada|konstr|pusle|kost[üu]{1,2}m|capri[\s\-]*sun|monstera|"
    r"viltpliiats|pliiats|colorpeps",
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
    r"karboniseeritud|karb[.\-]?tud|gaseeritud|purk|purgis|prk|can|pakend|"
    r"kmpl|tk|import|"
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


def _minor_units(value, scale: int) -> float | None:
    """WooCommerce sends prices as integer minor units: "169" is 1,69 EUR.

    money() deliberately will not guess this - an int there is taken at face
    value - so the conversion has to be explicit and driven by the
    currency_minor_unit the API reports rather than an assumed 100.
    """
    if value in (None, ""):
        return None
    try:
        return round(int(value) / scale, 2)
    except (TypeError, ValueError):
        return None


# The only Coop cooperative running its own e-shop. Tallinn and Pärnu sell
# through Wolt and Tartu through Bolt Food, which are marketplace storefronts
# rather than a Coop-run catalogue.
COOP_SHOP = "coophaapsalu.ee"


def scrape_coop() -> list[Product]:
    """Haapsalu eCoop. It runs WooCommerce, so the public Store API answers the
    whole search in one request, no key and no store selector.

    These are west-Estonian prices. Every Coop unit prices independently, so
    this row is honest only as "Coop Haapsalu" and not as "Coop".
    """
    s = session()
    r = s.get(f"https://{COOP_SHOP}/wp-json/wc/store/v1/products",
              params={"search": "monster", "per_page": 100}, timeout=TIMEOUT)
    r.raise_for_status()

    out: list[Product] = []
    for d in r.json():
        name = d.get("name") or ""
        if not is_monster(name):
            continue
        pr = d.get("prices") or {}
        scale = 10 ** int(pr.get("currency_minor_unit") or 2)
        shelf = _minor_units(pr.get("regular_price"), scale)
        now = _minor_units(pr.get("price"), scale)
        if shelf is None and now is None:
            continue
        out.append(Product(
            store="Coop Haapsalu",
            name=name,
            price=shelf if shelf is not None else now,
            loyalty_price=now if (now and shelf and now < shelf) else None,
            url=d.get("permalink", ""),
            image=(d.get("images") or [{}])[0].get("src", ""),
            ext_id=str(d.get("sku", "")),
        ))
    return out


ADAPTERS = {
    "rimi": scrape_rimi,
    "selver": scrape_selver,
    "coop": scrape_coop,
}

# Display name per adapter. Needed for the run report: an adapter that returns
# nothing leaves no Product to read the store name off.
STORE_NAMES = {
    "rimi": "Rimi",
    "selver": "Selver",
    "coop": "Coop Haapsalu",
}


# --------------------------------------------------------------------------
# output
#
# There is no database. Every run publishes a complete, fresh snapshot and
# nothing carries over from the last one, so a product a shop stops listing is
# simply absent next time. That is the behaviour we want, and storage was only
# ever a way of failing to achieve it.
# --------------------------------------------------------------------------

def export(products: list[Product], report: list[dict]) -> dict:
    """Write docs/data.json from this run's results."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # a paginated shop search can return the same product twice; keep the cheaper
    best: dict[tuple[str, str], Product] = {}
    for p in products:
        key = (p.store, p.name)
        if key not in best or p.best_price < best[key].best_price:
            best[key] = p

    items = []
    for p in sorted(best.values(), key=lambda x: (x.per_litre is None, x.per_litre or 0)):
        d = asdict(p)
        d["per_litre"] = p.per_litre   # a property, so asdict() misses it
        d["seen_at"] = now
        items.append(d)

    payload = {
        "updated": now,
        "sources": [{"adapter": r["adapter"], "store": r["store"],
                     "status": r["status"], "count": r["count"],
                     "detail": r.get("detail"), "seen_at": now}
                    for r in report],
        "items": items,
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
    ap.add_argument("--only", help="run a single store adapter, write nothing")
    args = ap.parse_args()

    products, report = run(args.only)

    if args.only:
        for p in sorted(products, key=lambda x: (x.per_litre is None, x.per_litre)):
            print(f"  {str(p.per_litre or '-'):>6}  {p.best_price:>5.2f}  "
                  f"{p.flavour:<22}{'0' if p.zero_sugar else ' '}  {p.name}")
        return 0

    if not products:
        # leave the previous data.json in place rather than publishing an empty
        # page; its timestamp stops advancing, which is what makes the staleness
        # warning on the page fire
        print("nothing collected, leaving the last data.json alone", file=sys.stderr)
        return 1

    export(products, report)
    print(f"published {len(products)} products")

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
