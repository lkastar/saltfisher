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
