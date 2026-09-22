"""Probe whether either venue lets a LIVE subscription set be mutated.

The daily match refresh wants to add newly-listed markets and drop settled ones
without restarting the collector, because a restart loses `last_append` — and
`was_viable` in particular, which is the only thing that pins a window's
`closed_at`. Whether that can be done incrementally, or only by reconnecting a
shard with a new list, depends on two venue behaviours that are not documented
anywhere in this repo:

  Polymarket: is `unsubscribe` honoured, and does it FREE A CAP SLOT?
              (1,900/conn here, hard venue limit 2,000.)
  Kalshi:     can tickers be added/removed on a live subscription, and does the
              `seq` counter survive it? A seq gap costs fresh snapshots for
              every ticker on the socket (see kalshi.py stream_books).

WHY THE CAP TEST IS THE ONLY ONE THAT COUNTS (Poly): the 2,001st subscription is
refused WITHOUT failing the socket, and a reader watching only `marketData`
frames cannot tell a refused subscription from an idle market — that is exactly
how a 75% subscription loss once ran healthy-looking for two hours. So an
`unsubscribe` that acks politely and frees nothing looks identical to success on
every test except "can I now subscribe to something new at the cap".

Absence of frames proves nothing on its own either: most markets are idle. The
cessation test therefore only uses slugs that were demonstrably ACTIVE in phase
one, and the cap test reads the `error` frame as its primary signal.

Read-only: subscribes, listens, unsubscribes. Places no orders and writes
nothing. Runs on its own socket, so it does not disturb a live collector (Poly's
cap is per-connection; Kalshi permits a second authenticated socket).

Run:
    .venv/bin/python scripts/probe_subscriptions.py poly
    .venv/bin/python scripts/probe_subscriptions.py kalshi
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import Counter

import aiohttp
import websockets

# Module privates, imported deliberately: the point of this probe is to speak
# exactly the wire dialect the feeds speak, so copying the URLs and the batch
# size would let the probe and the collector drift apart silently.
from pmarb.credentials import KalshiCredentials, PolymarketUSCredentials
from pmarb.feeds.auth import kalshi_headers, polymarket_us_headers
from pmarb.feeds.kalshi import _WS_PATH as K_WS_PATH
from pmarb.feeds.kalshi import _WS_URL as K_WS_URL
from pmarb.feeds.kalshi import KalshiFeed
from pmarb.feeds.polymarket import _UA
from pmarb.feeds.polymarket import _WS_PATH as P_WS_PATH
from pmarb.feeds.polymarket import _WS_URL as P_WS_URL
from pmarb.feeds.polymarket import PolymarketUSFeed

CAP = 2_000          # the venue's hard limit; subscribing exactly this is legal
BATCH = 200          # _SUB_BATCH: max marketSlugs per subscribe message
LISTEN_SUB = 45.0    # long enough for a few hundred active books to speak
LISTEN_CAP = 60.0    # the cap phase gets longer: new slugs may simply be quiet

# Kalshi's `seq` is one counter for the whole subscription, so continuity has to
# be tracked ACROSS phases — a mutation that resets it is the thing worth
# knowing, and a per-phase counter would miss exactly that.
seqs: dict = {}


def _banner(text: str) -> None:
    print(f"\n{'=' * 72}\n{text}\n{'=' * 72}", flush=True)


async def _listen(ws, seconds: float, *, label: str) -> tuple[Counter, list]:
    """Drain the socket for `seconds`, bucketing data frames by market.

    Returns (frames-per-market, control frames). A control frame is anything
    that is not a book update — acks, errors, whatever the venue volunteers.
    Those are the interesting output: this probe exists to find out what the
    venue says, so nothing is swallowed.
    """
    data: Counter = Counter()
    control: list = []
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except TimeoutError:
            break
        msg = json.loads(raw)
        md = msg.get("marketData")
        if md is not None:
            slug = md.get("marketSlug")
            if slug:
                data[slug] += 1
            continue
        if msg.get("type") in ("orderbook_snapshot", "orderbook_delta"):
            body = msg.get("msg") or {}
            ticker = body.get("market_ticker")
            if ticker:
                data[ticker] += 1
            # A gap in Kalshi's per-subscription `seq` is the expensive outcome:
            # the collector responds by reconnecting and resnapshotting every
            # ticker, so a mutation that gaps the sequence saves almost nothing
            # over a plain reconnect. Surfaced as a synthetic control frame so
            # it lands in the same output the venue's own replies do.
            seq = msg.get("seq")
            if seq is not None:
                last = seqs.get("last")
                if last is not None and seq != last + 1:
                    control.append({"_probe": "seq gap", "from": last, "to": seq})
                seqs["last"] = seq
            continue
        control.append(msg)
    print(f"  [{label}] {sum(data.values())} data frames across "
          f"{len(data)} markets; {len(control)} control frames", flush=True)
    for c in control[:12]:
        print(f"      {json.dumps(c)[:300]}", flush=True)
    if len(control) > 12:
        print(f"      ... {len(control) - 12} more", flush=True)
    return data, control


# --------------------------------------------------------------------------- #
# Polymarket
# --------------------------------------------------------------------------- #

async def probe_poly() -> None:
    creds = PolymarketUSCredentials.from_env()
    async with aiohttp.ClientSession() as session:
        markets = await PolymarketUSFeed(session, creds).fetch_markets()

    slugs: list[str] = []
    seen = set()
    for m in markets:
        slug = m.raw.get("market", {}).get("slug")
        if slug and slug not in seen:
            seen.add(slug)
            slugs.append(slug)
    print(f"catalog: {len(markets)} markets, {len(slugs)} distinct slugs")
    if len(slugs) < CAP + BATCH:
        raise SystemExit(
            f"need {CAP + BATCH} slugs to test the cap, catalog has {len(slugs)}")

    at_cap, spare = slugs[:CAP], slugs[CAP:CAP + BATCH]
    headers = {**polymarket_us_headers(creds, "GET", P_WS_PATH),
               "User-Agent": _UA}

    async with websockets.connect(P_WS_URL, additional_headers=headers) as ws:
        # ---- Phase 1: fill the connection to the cap ---------------------- #
        _banner(f"PHASE 1  subscribe {len(at_cap)} slugs (cap is {CAP})")
        for i in range(0, len(at_cap), BATCH):
            await ws.send(json.dumps({"subscribe": {
                "requestId": f"probe-sub{i}",
                "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
                "marketSlugs": at_cap[i:i + BATCH],
            }}))
        before, ctrl1 = await _listen(ws, LISTEN_SUB, label="phase1")
        if not before:
            raise SystemExit("no data at all in phase 1 — auth or subscribe "
                             "shape is wrong; nothing below would mean anything")

        # Unsubscribe is keyed by requestId ALONE (docs.polymarket.us websocket
        # overview): it tears down a whole original subscribe batch, and there is
        # no documented way to drop individual slugs. So the removable unit is
        # whatever a single subscribe call covered — here batch 0's 200 slugs —
        # not a set we get to choose now. An earlier attempt that also sent
        # `subscriptionType`/`marketSlugs` was rejected with `invalid_message`,
        # because the JSON->proto unmarshal refuses fields the schema lacks.
        target = at_cap[:BATCH]
        active_target = [s for s in target if before.get(s)]
        print(f"  {len(before)} of {len(at_cap)} subscribed slugs produced data; "
              f"dropping batch 0 ({len(target)} slugs, {len(active_target)} of "
              f"them active)")

        # ---- Phase 2: unsubscribe that batch ------------------------------ #
        _banner(f"PHASE 2  unsubscribe requestId probe-sub0 ({len(target)} slugs)")
        await ws.send(json.dumps({"unsubscribe": {"requestId": "probe-sub0"}}))
        after, ctrl2 = await _listen(ws, LISTEN_SUB, label="phase2")

        # Only the slugs that were DEMONSTRABLY talking in phase 1 can testify:
        # an idle market falls silent whether or not the unsubscribe landed.
        still_talking = [s for s in active_target if after.get(s)]
        print(f"\n  of {len(active_target)} unsubscribed slugs that were active "
              f"in phase 1, {len(still_talking)} still sent data")
        honoured = not still_talking

        # ---- Phase 3: the only test that settles it ----------------------- #
        _banner(f"PHASE 3  subscribe {len(spare)} NEW slugs while at the cap")
        await ws.send(json.dumps({"subscribe": {
            "requestId": "probe-new",
            "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
            "marketSlugs": spare,
        }}))
        new, ctrl3 = await _listen(ws, LISTEN_CAP, label="phase3")
        new_talking = [s for s in spare if new.get(s)]
        cap_errors = [c for c in ctrl3 if "error" in c]
        print(f"\n  {len(new_talking)} of {len(spare)} new slugs sent data; "
              f"{len(cap_errors)} error frames")

    _banner("VERDICT — Polymarket")
    print(f"  frames stopped for unsubscribed markets : "
          f"{'YES' if honoured else 'NO'}")
    print(f"  new subscriptions accepted at cap       : "
          f"{'YES' if new_talking and not cap_errors else 'NO'}")
    print(f"  error frames during phase 3             : {len(cap_errors)}")
    for c in cap_errors[:5]:
        print(f"      {json.dumps(c)[:300]}")
    print()
    if honoured and new_talking and not cap_errors:
        print("  => unsubscribe frees cap slots. Incremental removal is safe;\n"
              "     the refresh can drop settled markets with no reconnect and\n"
              "     no gap in any pair's window tracking.")
    elif honoured:
        print("  => frames stop but the slot is NOT freed: the subscription is\n"
              "     still counted. Removal must be a rolling shard recycle, or\n"
              "     the cap silently strands you as markets turn over.")
    else:
        print("  => unsubscribe is not honoured. Removal is a rolling shard\n"
              "     recycle: reconnect one shard at a time with the pruned\n"
              "     list, staggered, in the pre-slate quiet hour.")


# --------------------------------------------------------------------------- #
# Kalshi
# --------------------------------------------------------------------------- #

async def probe_kalshi() -> None:
    """Kalshi has no cap pressure here (one socket carries 7.4k tickers), so the
    question is different: can tickers be added and dropped live, and does `seq`
    survive it? A seq gap forces resnapshotting EVERY ticker on the socket, so a
    mutation that gaps the sequence is barely cheaper than a reconnect.

    The param names are unknown, so several plausible shapes are tried in turn
    and whatever the venue answers is printed verbatim.
    """
    creds = KalshiCredentials.from_env()
    async with aiohttp.ClientSession() as session:
        markets = await KalshiFeed(session).fetch_markets()
    tickers = [m.raw["market"]["ticker"] for m in markets
               if m.raw.get("market", {}).get("ticker")]
    held, spare = tickers[:50], tickers[50:55]
    print(f"catalog: {len(tickers)} tickers; holding {len(held)}, "
          f"{len(spare)} held back to test adding")

    headers = kalshi_headers(creds, "GET", K_WS_PATH)
    async with websockets.connect(K_WS_URL, additional_headers=headers) as ws:
        _banner(f"PHASE 1  subscribe {len(held)} tickers")
        await ws.send(json.dumps({
            "id": 1, "cmd": "subscribe",
            "params": {"channels": ["orderbook_delta"],
                       "market_tickers": held},
        }))
        before, ctrl1 = await _listen(ws, 30.0, label="phase1")
        sid = None
        for c in ctrl1:
            if c.get("type") == "subscribed":
                sid = (c.get("msg") or {}).get("sid")
        print(f"  subscription id (sid): {sid}")
        if not before:
            raise SystemExit("no book frames in phase 1 — nothing below is "
                             "interpretable")

        drop = [s for s, _ in before.most_common(5)]

        # Candidate dialects, most specific first. Kalshi acks with an `id`
        # echo, so each attempt is identifiable in the reply stream.
        attempts = [
            ("update_subscription / delete_markets", {
                "id": 2, "cmd": "update_subscription",
                "params": {"sids": [sid], "market_tickers": drop,
                           "action": "delete_markets"}}),
            ("update_subscription / unsubscribe", {
                "id": 3, "cmd": "update_subscription",
                "params": {"sid": sid, "market_tickers": drop,
                           "action": "unsubscribe"}}),
            ("unsubscribe / market_tickers", {
                "id": 4, "cmd": "unsubscribe",
                "params": {"sids": [sid], "market_tickers": drop}}),
        ]
        for label, payload in attempts:
            _banner(f"PHASE 2  {label}")
            print(f"  -> {json.dumps(payload)}")
            await ws.send(json.dumps(payload))
            _, ctrl = await _listen(ws, 8.0, label=label)
            if any(c.get("type") not in (None, "error") and c.get("id")
                   == payload["id"] for c in ctrl):
                print("  accepted — stopping here")
                break

        _banner(f"PHASE 3  add {len(spare)} NEW tickers live")
        add = {"id": 5, "cmd": "update_subscription",
               "params": {"sids": [sid], "market_tickers": spare,
                          "action": "add_markets"}}
        print(f"  -> {json.dumps(add)}")
        await ws.send(json.dumps(add))
        after, ctrl3 = await _listen(ws, 30.0, label="phase3")
        added = [t for t in spare if after.get(t)]
        dropped_quiet = [t for t in drop if not after.get(t)]

    _banner("VERDICT — Kalshi")
    print(f"  sid obtained                    : {sid}")
    print(f"  dropped tickers went quiet      : "
          f"{len(dropped_quiet)}/{len(drop)}")
    print(f"  newly added tickers sent data   : {len(added)}/{len(spare)}")
    print("\n  Read the control frames above for the authoritative answer: an\n"
          "  `error` reply to every attempted dialect means the subscription is\n"
          "  immutable and a reconnect is the only way to change the set.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("venue", choices=("poly", "kalshi"))
    args = ap.parse_args()
    asyncio.run(probe_poly() if args.venue == "poly" else probe_kalshi())


if __name__ == "__main__":
    main()
