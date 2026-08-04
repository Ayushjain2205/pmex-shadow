"""Shaping of the Data API's wallet views into what the target page renders.

Stubbed rather than hitting the live API: what's under test is the assembly — which
endpoint's failure degrades what, how "is there another page" is decided without a
total count, and the ordering that keeps a wallet's live positions above its
settled-to-zero ones. The payload shapes below are trimmed copies of real responses
for 0x48ac…4a7e (including a REDEEM row's empty `asset`/`side`, which is what the
activity feed actually returns and what the token lookup has to tolerate).
"""

from __future__ import annotations

import functools

import httpx
import pytest

from pmex_shadow.targets import profile


def _positions(n=3):
    return [
        # Deliberately worst-first, so a test that passes can only do so if the
        # function sorted: this is the order the API itself returned them in.
        {"asset": "aaa", "title": "Bitcoin Up or Down", "outcome": "Up", "outcomeIndex": 0,
         "size": 3200.01, "avgPrice": 0.99, "curPrice": 0.0, "currentValue": 0.0,
         "cashPnl": -3168.01, "percentPnl": -100.0, "redeemable": True, "slug": "btc-updown-5m-1"},
        {"asset": "bbb", "title": "XRP Up or Down", "outcome": "Up", "outcomeIndex": 0,
         "size": 42.0, "avgPrice": 0.474, "curPrice": 0.91, "currentValue": 38.53,
         "cashPnl": 18.45, "percentPnl": 92.0, "redeemable": False, "slug": "xrp-updown-15m-1"},
        {"asset": "ccc", "title": "Solana Up or Down", "outcome": "Down", "outcomeIndex": 1,
         "size": 64.0, "avgPrice": 0.65, "curPrice": 0.005, "currentValue": 0.32,
         "cashPnl": -41.28, "percentPnl": -99.0, "redeemable": True, "slug": "sol-updown-5m-1"},
    ][:n]


def _activity(n):
    rows = []
    for i in range(n):
        if i % 4 == 3:
            # A redemption: no asset, no side, no price. The route filters on `asset`
            # precisely because of these.
            rows.append({"timestamp": 1785853871 - i, "type": "REDEEM", "asset": "", "side": "",
                         "price": 0, "size": 42.63, "usdcSize": 42.63, "outcome": "Up",
                         "conditionId": f"0xcond{i}", "transactionHash": f"0xtx{i}",
                         "title": f"XRP Up or Down {i}", "slug": f"xrp-{i}"})
        else:
            rows.append({"timestamp": 1785853871 - i, "type": "TRADE", "asset": f"tok{i}", "side": "BUY",
                         "price": 0.18, "size": 256.0, "usdcSize": 36.46, "outcome": "Down",
                         "conditionId": f"0xcond{i}", "transactionHash": f"0xtx{i}",
                         "title": f"Bitcoin Up or Down {i}", "slug": f"btc-{i}"})
    return rows


def _install(monkeypatch, handler):
    """Point profile.fetch_profile's internal AsyncClient at a MockTransport."""
    real = httpx.AsyncClient
    monkeypatch.setattr(
        profile.httpx, "AsyncClient",
        functools.partial(real, transport=httpx.MockTransport(handler)),
    )


def _responder(activity_n=5, fail: set[str] = frozenset()):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        name = path.rsplit("/", 1)[-1]
        if name in fail:
            return httpx.Response(500, json={"error": "boom"})
        if name == "activity":
            return httpx.Response(200, json=_activity(activity_n))
        if name == "positions":
            return httpx.Response(200, json=_positions())
        if name == "value":
            return httpx.Response(200, json=[{"user": "0xabc", "value": 574.1}])
        if name == "trades":
            return httpx.Response(200, json=[{"name": "BoneOhio", "pseudonym": "Warlike-Fallingout",
                                              "profileImage": "", "bio": "hi"}])
        return httpx.Response(404, json={})
    return handler


async def test_assembles_every_panel(monkeypatch):
    _install(monkeypatch, _responder())
    p = await profile.fetch_profile("https://data-api.test", "0xABC")

    assert p["portfolio_value"] == 574.1
    assert (p["name"], p["pseudonym"], p["bio"]) == ("BoneOhio", "Warlike-Fallingout", "hi")
    # An empty profileImage is absence, not a URL to render an <img> for.
    assert p["profile_image"] is None
    assert p["errors"] == []


async def test_positions_sorted_by_live_value(monkeypatch):
    """A wallet trading 5-minute markets accumulates hundreds of settled-to-zero rows;
    unsorted, they bury everything it currently holds."""
    _install(monkeypatch, _responder())
    p = await profile.fetch_profile("https://data-api.test", "0xABC")
    assert [x["currentValue"] for x in p["positions"]] == [38.53, 0.32, 0.0]


async def test_next_page_probed_by_overfetching(monkeypatch):
    """These endpoints return no total count, so the only way to know whether to offer
    a Next link is to request one row beyond the page and see if it comes back."""
    _install(monkeypatch, _responder(activity_n=profile.ACTIVITY_PAGE_SIZE + 1))
    p = await profile.fetch_profile("https://data-api.test", "0xABC")
    assert p["activity_has_next"] is True
    # The probe row must not leak into the rendered page.
    assert len(p["activity"]) == profile.ACTIVITY_PAGE_SIZE

    _install(monkeypatch, _responder(activity_n=profile.ACTIVITY_PAGE_SIZE))
    p = await profile.fetch_profile("https://data-api.test", "0xABC")
    assert p["activity_has_next"] is False
    assert len(p["activity"]) == profile.ACTIVITY_PAGE_SIZE


async def test_offset_follows_page(monkeypatch):
    """/activity is hit by two different readers — the paged display feed and the
    wider ledger window — distinguished by page size, not by offset (the window pages
    too)."""
    calls = []

    def handler(request):
        if request.url.path.endswith("/activity"):
            calls.append((request.url.params.get("limit"), request.url.params.get("offset")))
        return _responder()(request)

    _install(monkeypatch, handler)
    await profile.fetch_profile("https://data-api.test", "0xABC", activity_page=3)

    display = [c for c in calls if c[0] == str(profile.ACTIVITY_PAGE_SIZE + 1)]
    window = sorted(c for c in calls if c[0] == str(profile.LEDGER_PAGE))
    assert display == [(str(profile.ACTIVITY_PAGE_SIZE + 1), str(2 * profile.ACTIVITY_PAGE_SIZE))]
    # The window is fetched as consecutive offset pages, all concurrently.
    assert [c[1] for c in window] == sorted(
        str(i * profile.LEDGER_PAGE) for i in range(profile.LEDGER_PAGES)
    )


async def test_one_dead_endpoint_does_not_take_the_page_down(monkeypatch):
    """The Postgres half of this page (our own decisions) doesn't depend on any of
    this, so a failed Data API call has to degrade to an empty panel and a named
    error — not an exception that costs the operator the whole page."""
    _install(monkeypatch, _responder(fail={"positions", "value"}))
    p = await profile.fetch_profile("https://data-api.test", "0xABC")

    assert sorted(p["errors"]) == ["positions", "value"]
    assert p["positions"] == []
    assert p["portfolio_value"] is None
    # Everything that did answer is still there.
    assert len(p["activity"]) == 5
    assert p["ledger"]["rows"]


def test_series_key_strips_the_recurrence_but_not_a_year():
    """`btc-updown-5m-1785853200` is one recurrence of a series; `...-2026` is not."""
    assert profile._series_key("btc-updown-5m-1785853200") == "btc-updown-5m"
    assert profile._series_key("presidential-election-2026") == "presidential-election-2026"
    assert profile._series_key(None) is None


def test_behaviour_groups_thousands_of_markets_into_a_few_series():
    """The whole point of the panel: a wallet trading 5-minute markets touches a new
    token every five minutes, and a per-market list of that is unreadable."""
    activity = (
        [{"type": "TRADE", "slug": f"btc-updown-5m-{1785853200 + i}", "title": "Bitcoin Up or Down - Aug 4",
          "side": "BUY", "usdcSize": 100.0, "timestamp": 1785853200 + i} for i in range(6)]
        + [{"type": "TRADE", "slug": f"eth-updown-5m-{1785853200 + i}", "title": "Ethereum Up or Down - Aug 4",
            "side": "BUY", "usdcSize": 50.0, "timestamp": 1785853200 + i} for i in range(2)]
    )
    b = profile.summarise_behaviour(activity)

    assert b["distinct_series"] == 2
    assert [s["series"] for s in b["series"]] == ["btc-updown-5m", "eth-updown-5m"]
    top = b["series"][0]
    assert (top["label"], top["trades"], top["notional"]) == ("Bitcoin Up or Down", 6, 600.0)
    assert top["share"] == 0.75
    assert top["avg_size"] == 100.0


def test_redemptions_stay_out_of_size_statistics():
    """A redemption's usdcSize is a payout, not a position size the wallet chose;
    blending them makes a wallet look like it sizes far larger than it does."""
    activity = [
        {"type": "TRADE", "slug": "btc-updown-5m-1785853200", "title": "Bitcoin Up or Down - Aug 4",
         "side": "BUY", "usdcSize": 10.0, "timestamp": 1785853200},
        {"type": "REDEEM", "slug": "btc-updown-5m-1785853100", "title": "Bitcoin Up or Down - Aug 4",
         "side": "", "usdcSize": 5000.0, "timestamp": 1785853100},
    ]
    b = profile.summarise_behaviour(activity)

    assert (b["trades"], b["redeems"]) == (1, 1)
    assert b["max_size"] == 10.0 and b["avg_size"] == 10.0
    # The redemption is still counted in the sample, just not in the sizing.
    assert b["sampled"] == 2
    assert b["distinct_series"] == 1


def test_behaviour_of_an_empty_feed_is_not_a_crash():
    b = profile.summarise_behaviour([])
    assert b["series"] == [] and b["trades"] == 0
    assert b["median_size"] is None and b["trades_per_hour"] is None


def _act(ts, type_, cid, usd, side="BUY", slug="btc-updown-5m-1785853200"):
    return {"timestamp": ts, "type": type_, "conditionId": cid, "usdcSize": usd, "side": side,
            "slug": slug, "title": "Bitcoin Up or Down - Aug 4", "outcome": "Up",
            "transactionHash": f"0x{ts}{cid}"}


def test_ledger_nets_buys_against_the_redemption_that_settles_them():
    """The core arithmetic: paid in vs came back out, per market. Reconciled against
    real data — 524 settled markets summed to $201,091 staked and $205,129 returned."""
    base = 1785000000
    activity = [
        # Anchors the start of the window well before the markets under test, so
        # neither trips the edge-clip guard (which by design suspects whatever sits
        # at the very beginning of the fetched range).
        _act(base, "TRADE", "0xearlier", 10.0),
        # A winner: staked 100, redeemed 130.
        _act(base + 500, "TRADE", "0xwin", 60.0),
        _act(base + 520, "TRADE", "0xwin", 40.0),
        _act(base + 900, "REDEEM", "0xwin", 130.0, side=""),
        # A partial loss: staked 50, only 5 came back.
        _act(base + 600, "TRADE", "0xlose", 50.0),
        _act(base + 950, "REDEEM", "0xlose", 5.0, side=""),
    ]
    led = profile.build_ledger(activity)

    by_id = {r["condition_id"]: r for r in led["rows"]}
    assert by_id["0xwin"]["cost"] == 100.0
    assert by_id["0xwin"]["redeemed"] == 130.0
    assert by_id["0xwin"]["pnl"] == 30.0
    assert by_id["0xwin"]["roi"] == pytest.approx(0.30)
    assert by_id["0xwin"]["trades"] == 2
    assert by_id["0xlose"]["pnl"] == -45.0

    assert led["settled"] == 2
    assert led["wins"] == 1
    assert led["total_cost"] == 150.0 and led["total_payout"] == 135.0
    assert led["total_pnl"] == -15.0
    assert led["win_rate"] == 0.5


def test_edge_clipped_markets_are_shown_but_not_counted():
    """A redemption whose buys fall outside the fetched window would read as pure
    profit and poison the totals."""
    base = 1785000000
    activity = [
        # Sits at the very start of the window: its buys are off-screen.
        _act(base, "REDEEM", "0xclipped", 500.0, side=""),
        # Comfortably inside.
        _act(base + 5000, "TRADE", "0xclean", 100.0),
        _act(base + 6000, "REDEEM", "0xclean", 110.0, side=""),
    ]
    led = profile.build_ledger(activity)

    by_id = {r["condition_id"]: r for r in led["rows"]}
    assert by_id["0xclipped"]["clipped"] is True
    assert by_id["0xclean"]["clipped"] is False
    # The clipped row is still rendered, just kept out of the arithmetic.
    assert len(led["rows"]) == 2 and led["clipped"] == 1
    assert led["settled"] == 1
    assert led["total_pnl"] == 10.0


def test_a_redemption_with_no_observed_buying_is_not_profit():
    """The failure this actually shipped with: a wallet whose window spans weeks has
    markets whose buys predate it entirely. Counting those redemptions against zero
    cost reported a 100% win rate and double the money — $36,613 staked, $73,242
    returned, on a wallet that had done nothing of the sort."""
    base = 1785000000
    activity = [
        _act(base, "TRADE", "0xanchor", 5.0),
        # Redemption only: whatever paid for this is older than the window.
        _act(base + 4000, "REDEEM", "0xorphan", 900.0, side=""),
        # A complete one, for contrast.
        _act(base + 5000, "TRADE", "0xclean", 100.0),
        _act(base + 6000, "REDEEM", "0xclean", 110.0, side=""),
    ]
    led = profile.build_ledger(activity)

    by_id = {r["condition_id"]: r for r in led["rows"]}
    assert by_id["0xorphan"]["clipped"] is True
    assert led["settled"] == 1
    assert led["total_pnl"] == 10.0
    assert led["win_rate"] == 1.0  # the one market we can actually account for
    # The orphan is still visible, just not counted.
    assert by_id["0xorphan"]["redeemed"] == 900.0


def test_still_held_markets_are_marked_to_the_live_position():
    base = 1785000000
    activity = [_act(base + 500, "TRADE", "0xopen", 80.0)]
    led = profile.build_ledger(activity, open_values={"0xopen": 95.0})

    row = led["rows"][0]
    assert row["status"] == "open"
    assert row["value"] == 95.0
    assert row["pnl"] == 15.0
    # Unrealized, so it stays out of the settled totals.
    assert led["settled"] == 0 and led["total_pnl"] == 0


def test_recently_bought_markets_report_no_pnl_rather_than_a_loss():
    """Bought a minute ago and not yet settled is not the same as lost — showing -$80
    for a market that is still running would be a fabricated loss."""
    led = profile.build_ledger([_act(1785000500, "TRADE", "0xpending", 80.0)])
    row = led["rows"][0]
    assert row["status"] == "pending"
    assert row["pnl"] is None and row["roi"] is None


def test_a_market_that_never_paid_out_is_a_loss_not_a_missing_row():
    """The bias that made every wallet look like a 100% winner: a losing position
    emits no REDEEM row at all, because there is nothing to redeem when your side
    settles at zero. Treating "bought, long resolved, never paid" as unresolved meant
    only winners ever reached the totals."""
    base = 1785000000
    activity = [
        _act(base, "TRADE", "0xanchor", 5.0),
        _act(base + 500, "TRADE", "0xwin", 100.0),
        _act(base + 900, "REDEEM", "0xwin", 150.0, side=""),
        # Bought, then nothing ever again — and long enough ago to be resolved.
        _act(base + 600, "TRADE", "0xlost", 100.0),
        # Establishes "now" well past the resolution cutoff.
        _act(base + 600 + profile.RESOLVED_AFTER_S * 2, "TRADE", "0xrecent", 10.0),
    ]
    led = profile.build_ledger(activity)
    by_id = {r["condition_id"]: r for r in led["rows"]}

    assert by_id["0xlost"]["status"] == "lost"
    assert by_id["0xlost"]["pnl"] == -100.0
    # Without this, win_rate would be 100%.
    assert led["win_rate"] == 0.5
    assert led["total_pnl"] == -50.0


def test_aggregate_is_withheld_when_the_window_cannot_account_for_the_markets():
    """A wallet that trades rarely has most of its markets straddling the window; one
    such wallet derived +44% ROI while holding a $0 portfolio. Better to report no
    total than a confident wrong one."""
    base = 1785000000
    activity = [_act(base, "TRADE", "0xanchor", 5.0)]
    # Nine redemptions with no observed buying — all excluded, so almost nothing is
    # accountable.
    for i in range(9):
        activity.append(_act(base + 4000 + i, "REDEEM", f"0xorphan{i}", 900.0, side=""))
    led = profile.build_ledger(activity)

    assert led["coverage"] < profile.MIN_COVERAGE
    assert led["reliable"] is False
    # The rows are still there to look at; only the headline is withheld.
    assert len(led["rows"]) == 10


def test_wallet_level_rebates_are_income_not_a_market():
    """TAKER_REBATE rows carry an empty conditionId — they can't sit in a per-market
    ledger, but dropping them would understate what the wallet earned."""
    activity = [
        _act(1785000500, "TRADE", "0xm", 100.0),
        _act(1785000900, "REDEEM", "0xm", 105.0, side=""),
        {"timestamp": 1785000950, "type": "TAKER_REBATE", "conditionId": "", "asset": "",
         "side": "", "usdcSize": 14.12, "title": "", "slug": "", "transactionHash": "0xr"},
    ]
    led = profile.build_ledger(activity)
    assert led["income"] == 14.12
    assert len(led["rows"]) == 1


async def test_window_dedupes_overlapping_offset_pages(monkeypatch):
    """Offset paging over a live feed genuinely overlaps — 2,000 fetched rows came
    back as ~1,978 unique against the real API. Here all four pages return the same
    rows, which dedupe must collapse rather than counting four times over."""
    _install(monkeypatch, _responder(activity_n=10))
    p = await profile.fetch_profile("https://data-api.test", "0xABC")
    assert p["behaviour"]["sampled"] == 10


async def test_redemption_rows_survive_timestamp_conversion(monkeypatch):
    """REDEEM rows carry an empty `asset`/`side` and price 0; they still need a
    localisable timestamp like every other row."""
    _install(monkeypatch, _responder(activity_n=4))
    p = await profile.fetch_profile("https://data-api.test", "0xABC")

    redeem = [a for a in p["activity"] if a["type"] == "REDEEM"]
    assert redeem and redeem[0]["asset"] == ""
    assert all(a["at"] is not None and a["at"].tzinfo is not None for a in p["activity"])
