# Fixtures

`*_synthetic.json` are **hand-built**, not captured from goofish. Every probe from
the development machine was blocked by risk control before a result list was ever
returned (see `.trellis/tasks/*/research/mtop-access-probe.md`), so upstream key
names in `app/collector/base.py` are candidates rather than verified facts.

What the synthetic fixtures DO prove: the envelope classification, the price and
timestamp parsers, the missing-field bookkeeping, and the filter matrix — all of
which are our own logic and are exercised faithfully.

What they DO NOT prove: that the real payload uses these key names.

To replace them with real data, run on an unchallenged network:

    uv run python -m scripts.capture_fixture --keyword "iPhone 15"

It writes `search_real.json` and reports which mapped fields were absent.

## `seller_listing_synthetic.json`

Also hand-built, but from a **live observation of the response shape**: the
2026-09-08 seller-listing probe watched a real seller page and recorded the key
names, their types, and four values — `totalCount: 0` beside a `cardList` of 4,
`nextPage: false`, `itemId: "1080376910394"`, `itemStatus: 0`, `price: 750`.
The first card reproduces those exactly; the other three vary the price shape
and drop optional keys, which is our own parsing being exercised, not a claim
about upstream.

`totalCount: 0` with a non-empty `cardList` is the whole point of the file, and
it is measured, not invented: the page displayed 「在售 4」, so 4 is the truth
and the field is a lie. `detailParams.postInfo` carries a human label here
because the probe recorded the key and never its contents — see
`mtop.flatten_seller_card`.

`cardData.id` carries a deliberately DIFFERENT value from
`detailParams.itemId` here. The probe recorded that key and never its contents,
so any value would be synthetic — and a distinguishable one is what lets a test
prove the collector reads the id it verified instead of the one it did not. Two
equal values would have made that test pass either way.
