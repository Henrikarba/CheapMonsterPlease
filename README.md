# Monster index

Daily Monster Energy prices from Estonian grocery e-shops, published as a static page.

    pip install -r requirements.txt
    python scrape.py --only rimi     # try one store, prints, saves nothing
    python scrape.py                 # all stores -> prices.sqlite + docs/data.json

Publish: repo settings -> Pages -> deploy from branch `main`, folder `/docs`.

`index.html` lives in `docs/` next to `data.json`, which it fetches by relative
path. Opening it from anywhere else - or by double-clicking the file, since
`file://` blocks the fetch - leaves the page with no data. To view it locally:

    python -m http.server 8000 --directory docs

A page that cannot load `data.json` says so and shows nothing. There is no
placeholder dataset on purpose: it rendered invented prices that were
indistinguishable from real ones.

## Stores

| adapter  | shop            | method                          | status  |
|----------|-----------------|---------------------------------|---------|
| rimi     | rimi.ee/epood   | HTML, `data-gtm-eec-product`    | wired   |
| selver   | selver.ee       | Vue Storefront `_search` JSON   | wired   |
| prisma   | prismamarket.ee | needs store id + XHR discovery  | stub    |
| coop     | ecoop.ee        | regional, needs store selector  | stub    |

Maxima/barbora.ee was removed: `/api/eshop/v1/cart/products` is gone (404) and
search moved to Constructor.io, whose index carries no prices — only per-warehouse
stock flags — so each product would need a second request.

Lidl has no adapter and cannot have one. Their search API works
(`/q/api/search?q=…&assortment=EE&locale=et_EE&version=2.0.0`, needs `Accept: */*`)
but only indexes the current weekly offer leaflet, about 37 rotating items. The
standing grocery range is not online at all — `coca-cola` returns zero hits too.

## Knowing when an adapter has broken

Every run writes one row per adapter to `source_status` (ok / empty / failed /
stub), and `docs/data.json` carries the latest of each as `sources`. The page
turns that into the count behind each shop chip.

This exists because a wired adapter returning zero products is invisible
otherwise: it writes no observations, so the shop just disappears from the page
and reads as "that shop has no Monster". A shop showing `0` in amber is a broken
adapter; `–` in grey is a stub that was never wired.

The run still exits 0 when a shop is down, on purpose - the remaining shops'
prices are worth publishing, and a non-zero exit would stop the workflow before
it commits them. Check stderr for the `degraded:` line.

## Refreshing the data

The daily cron in `update.yml` runs the scraper on GitHub's runners and commits
the result - no server of your own is involved. `workflow_dispatch` is also on,
so a run can be started by hand: Actions -> Update prices -> Run workflow.

Set `REPO` near the top of the script block in `docs/index.html` to
`"kasutaja/repo"` and the page grows a "Korje" row: a link to that workflow page,
plus the last run's outcome read from the public GitHub API (unauthenticated,
so public repos only, 60 requests/hour per IP). Left empty the row stays hidden.

It is a link rather than a button on purpose. Triggering a workflow needs a
token with `Actions: write`, and a static page has nowhere to hide one - anyone
opening view-source could read it and run jobs on the account. A real in-page
button needs something server-side holding the token (a Cloudflare Worker or
similar); the link costs one extra click and no published credential.

The page also flags staleness on its own: past `STALE_AFTER_H` (26h, one cron
period plus slack) the timestamp turns amber and the warning bar says how old
the data is. That catches a cron that stopped firing, which no adapter status
would reveal.

## Re-finding an endpoint when an adapter breaks

Open the shop, search `monster`, DevTools -> Network -> Fetch/XHR, right click the
request -> Copy as cURL. No XHR means the page is server-rendered; parse the HTML
instead and prefer stable data attributes over price CSS classes.

## Notes

- Loyalty price (Partnerkaart / Säästukaart / Rimi kaart) is stored separately from
  the shelf price; the ladder sorts on whichever is lower.
- Coop and Prisma assortments are per store, so those numbers are only meaningful
  once a specific shop is pinned.
- One request every 1.5 s, once a day. Keep it that way.

## Flavours

`parse_flavour()` in scrape.py reads the variant out of the product title
(`Energiajook Monster Mango Loco 0,5 l` -> `Mango Loco`) using an ordered regex
list, most specific first. Anything unmatched falls back to the title with brand,
volume and packaging words stripped, so a new variant shows up under a readable
name rather than vanishing.

Run `python scrape.py --only rimi` after any shop starts renaming things: the
parsed flavour is printed next to each product, so mis-parses are visible
immediately. To fix one, add a row near the top of `FLAVOURS`.

`zero_sugar` is a separate flag (Ultra / Zero / suhkruvaba) and drives the
suhkruvaba filter on the page.
