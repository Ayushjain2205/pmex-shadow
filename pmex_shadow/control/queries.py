"""Financial and operational reads for the control plane (FR-C-1): PnL, positions,
fills, and equity data come straight from Postgres — never sampled, never through
Prometheus. This module is the only place the control app touches the database.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from decimal import Decimal

import asyncpg
import httpx


WALLET_PILL_COLOURS = 6


def assign_wallet_colours(aliases) -> dict[str, int]:
    """Colour slot per wallet pill. The only requirement is that two wallets visible
    together never share a colour, so slots are handed out by enumeration over the
    sorted aliases — nothing cleverer earns its keep here.

    Hashing the alias was the obvious first move and is worse: crc32 maps both
    btc_test2 and btc_test3 into the same slot, so the two wallets accounting for
    nearly every row would have rendered identically — precisely what the pills exist
    to tell apart. Hashing only buys a colour that survives across pages and restarts,
    which nothing depends on.

    Past WALLET_PILL_COLOURS wallets the slots wrap and colours repeat; the pill text
    still names each wallet.
    """
    return {alias: i % WALLET_PILL_COLOURS for i, alias in enumerate(sorted(set(aliases)))}


def _jsonb(value):
    """asyncpg returns JSONB columns as raw text, not a decoded object, without an
    explicit type codec registered on the connection — found the hard way (a live
    AttributeError in the fleet view) rather than assumed. Every JSONB read in this
    module goes through this rather than trusting the driver to have decoded it.
    """
    return value if isinstance(value, dict) else json.loads(value)


# (human label, category) per PRD §6 skip reason. Category drives color in the UI —
# Seven categories, not three — three collapsed slippage_guard/stale_fill/
# volatility_guard (the reasons that actually dominate real skip volume) into one
# identical color, which looked exactly as monochrome as no categorization at all.
# Each category below maps to its own hue in base.html's .pill-cat-* — capital
# (red), position slots (teal), operational halt (slate), timing (yellow),
# price slipped (orange), market chaos (rose), everything filtered by a
# selector/sizing rule by design (violet), and data_gap (sky) — added alongside
# no_orderbook/no_market_meta/target_not_registered: these aren't a decide() verdict
# at all, they're consumer.py giving up *before* decide() runs because a lookup came
# back empty. Previously that was a bare `return` with nothing recorded anywhere;
# now it's a real row like everything else, so it needed its own category rather
# than being misfiled under "filtered" (which is specifically selector/sizing rules).
_SKIP_REASON_META: dict[str, tuple[str, str]] = {
    "envelope_exhausted": ("Capital exhausted", "capital"),
    "global_exposure_cap": ("Fleet exposure cap hit", "capital"),
    "max_concurrent_positions": ("Position limit reached", "position_limit"),
    "bot_halted": ("Bot halted", "halted"),
    "target_paused": ("Target paused", "halted"),
    "stale_fill": ("Fill too old", "timing"),
    "slippage_guard": ("Price moved too far", "slippage"),
    "volatility_guard": ("Market too volatile", "chaos"),
    "selector_category": ("Category filtered", "filtered"),
    "selector_deny": ("Excluded by deny rule", "filtered"),
    "selector_allow": ("Outside allowlist", "filtered"),
    "selector_liquidity": ("Book too thin", "filtered"),
    "selector_notional": ("Target trade too small", "filtered"),
    "selector_resolution_window": ("Resolves too far out", "filtered"),
    "unknown_category": ("Category unknown", "filtered"),
    "market_not_tradeable": ("Market not tradeable", "filtered"),
    "below_target_percentile": ("Below sizing threshold", "filtered"),
    "below_min_order": ("Order too small", "filtered"),
    "no_position_to_exit": ("Nothing to sell", "filtered"),
    "netted_out": ("Netted against another intent", "filtered"),
    "no_orderbook": ("No orderbook available", "data_gap"),
    "no_market_meta": ("Market metadata unavailable", "data_gap"),
    "target_not_registered": ("Target not registered yet", "data_gap"),
}


def _skip_label(reason: str) -> str:
    return _SKIP_REASON_META.get(reason, (reason, "filtered"))[0]


def _skip_category(reason: str) -> str:
    return _SKIP_REASON_META.get(reason, (reason, "filtered"))[1]


def _rule_str(rule: dict[str, str]) -> str:
    """A deny/allow rule as written: `asset=sol`, `tag=crypto-prices duration=5m`."""
    return " ".join(f"{k}={v}" for k, v in rule.items())


# Most-identifying first. A market carries a dozen tags and a slug nobody reads, but
# one of these usually answers "which market is this" on its own.
_IDENTITY_KEYS = ("asset", "series", "category", "tag", "slug")


def _market_identity(attrs: dict[str, list[str]]) -> str:
    for key in _IDENTITY_KEYS:
        values = attrs.get(key)
        if values:
            return f"{key}={'|'.join(values[:3])}"
    return "market with no identifying attributes"


def _skip_summary(reason: str, detail: dict | None) -> str | None:
    """One human-readable line of the actual numbers behind a skip — the whole
    point of Skip.detail existing. Returns None where the label alone already says
    everything (bot_halted, no_position_to_exit, etc.) rather than padding with
    nothing.
    """
    if not detail:
        return None
    if "reason" in detail and len(detail) == 1:
        return {"no_liquidity_on_book_side": "No liquidity on that side of the book",
                "no_current_book_to_compare": "No current book to compare against",
                "no_history_in_window": "No book history in the window yet"}.get(detail["reason"], detail["reason"])
    if reason == "stale_fill":
        return f"{detail['age_s']:.0f}s old (limit {detail['max_fill_age_s']}s)"
    if reason == "slippage_guard":
        return f"{detail['adverse_ticks']:.1f} ticks adverse — target ${detail['target_price']}, best ${detail['best_price']} (limit {detail['max_slippage_ticks']} ticks)"
    if reason == "volatility_guard":
        return f"{detail['move_ticks']:.1f} ticks moved in {detail['window_s']}s (limit {detail['max_ticks']})"
    if reason == "envelope_exhausted":
        return f"${detail['available_usd']:.2f} available (envelope ${detail['envelope_usd']:.2f}, realized ${detail['realized_pnl_usd']:.2f})"
    if reason == "global_exposure_cap":
        return f"${detail['global_exposure_usd']:.2f} / ${detail['global_max_exposure_usd']:.2f} fleet-wide"
    if reason == "max_concurrent_positions":
        return f"{detail['current_open_positions']} / {detail['max_concurrent_positions']} positions open"
    if reason == "below_target_percentile":
        return f"{detail['target_percentile']:.1f}th percentile (need {detail['min_required_percentile']:.1f}th+)"
    if reason == "below_min_order":
        return f"${detail['sized_usd']:.2f} sized (min ${detail['min_order_usd']:.2f})"
    if reason == "selector_category":
        return f"{detail['market_category']!r} not in {detail['allowed_categories']}"
    if reason == "selector_liquidity":
        return f"${detail['book_liquidity_usd']:.2f} liquidity (need ${detail['min_required_usd']:.2f}+)"
    if reason == "selector_notional":
        return f"${detail['fill_notional_usd']:.2f} fill (need ${detail['min_required_usd']:.2f}+)"
    if reason == "selector_resolution_window":
        return f"{detail['resolution_days_out']}d out (limit {detail['max_allowed_days']}d)"
    if reason == "selector_deny":
        return _rule_str(detail["matched_rule"])
    if reason == "selector_allow":
        # The deny line can just name the rule that fired; this one can't, because
        # nothing fired. The actual question behind an unexpected `selector_allow` is
        # "what did the market look like, then" — so lead with its identity and let
        # the rule count carry the rest.
        rules = detail["allow_rules"]
        plural = "rule" if len(rules) == 1 else "rules"
        return f"{_market_identity(detail['market_attrs'])} matched none of {len(rules)} allow {plural}"
    if reason == "target_paused":
        return f"status: {detail['target_status']}"
    if reason in ("no_orderbook", "no_market_meta"):
        return f"token {detail['token_id'][:12]}…"
    if reason == "target_not_registered":
        return f"{detail['target']} not in target_stats"
    return " · ".join(f"{k}: {v}" for k, v in detail.items())


def _context_summary(context: dict | None) -> str | None:
    """`events.context` has no per-message format the way skip_detail has per-reason
    (events come from a handful of unrelated components, not one policy engine) — a
    flat `key: value` join is honest rather than guessing a nicer format per message."""
    if not context:
        return None
    return " · ".join(f"{k}: {v}" for k, v in context.items())


BOT_HEARTBEAT_STALE_AFTER = dt.timedelta(seconds=60)  # bots write one every 5s (watcher/heartbeat.py); 60s is generous


FLEET_STATUSES = ("running", "halted", "stale")


async def halt_state(conn: asyncpg.Connection, bot_id: str) -> tuple[bool, str | None]:
    """Whether the bot is halted, and why.

    Mirrors ledger/subaccount.py's get_ledger_state() halted check exactly (same
    events, same "most recent halt vs most recent resume" rule) — not imported from
    there since that function also pulls positions/exposure the control views don't
    need; duplicated as one query pair, not re-derived differently. Shared between
    the fleet list and the bot page so a bot can't read as halted on one and running
    on the other.
    """
    last_halt = await conn.fetchrow(
        "SELECT component, message, context, at FROM events WHERE bot_id = $1 AND level = 'CRITICAL' "
        "AND component IN ('ledger.reconcile', 'killswitch') ORDER BY at DESC LIMIT 1",
        bot_id,
    )
    last_resume = await conn.fetchrow(
        "SELECT at FROM events WHERE bot_id = $1 AND component = 'killswitch' AND message = 'resumed' ORDER BY at DESC LIMIT 1",
        bot_id,
    )
    if last_halt is None or (last_resume is not None and last_resume["at"] >= last_halt["at"]):
        return False, None
    ctx = _jsonb(last_halt["context"]) if last_halt["context"] is not None else {}
    return True, ctx.get("reason") or last_halt["message"]


async def fleet_view(
    conn: asyncpg.Connection,
    mode: str | None = None,
    status: str | None = None,
    show_archived: bool = False,
) -> dict:
    """Per bot: mode, watcher-relative lag, exposure vs envelope, today's PnL, skip
    rate, last fill — the fleet-view screen (design doc §3.8).

    Archived bots are excluded unless show_archived. `mode` filters in SQL; `status`
    can't — halted and stale are derived per row below, not columns.
    """
    # LEFT JOIN, not INNER: an unregistered bot (config seeded by an older build,
    # registry row somehow missing) has a NULL archived_at and still shows up. A
    # missing registry row must never silently hide a bot that's trading.
    bots = await conn.fetch(
        """
        SELECT bc.bot_id, bc.version, bc.config, b.archived_at, b.archived_reason
        FROM bot_config bc
        LEFT JOIN bots b ON b.bot_id = bc.bot_id
        WHERE bc.active
          AND ($1::bool OR b.archived_at IS NULL)
          AND ($2::text IS NULL OR bc.config->>'mode' = $2)
        ORDER BY bc.bot_id
        """,
        show_archived, mode or None,
    )
    archived_count = await conn.fetchval("SELECT count(*) FROM bots WHERE archived_at IS NOT NULL")
    rows = []
    for b in bots:
        bot_id = b["bot_id"]
        config = _jsonb(b["config"])

        heartbeat = await conn.fetchrow("SELECT at FROM heartbeats WHERE service = $1", f"bot:{bot_id}")
        deployed = await conn.fetchrow(
            "SELECT COALESCE(sum(cost_basis_usd), 0) AS deployed FROM positions WHERE bot_id = $1 AND lifecycle NOT IN ('redeemed', 'refunded', 'written_off')",
            bot_id,
        )
        today_pnl = await conn.fetchrow(
            "SELECT COALESCE(sum(realized_pnl_usd), 0) AS pnl FROM positions WHERE bot_id = $1 AND last_event_at >= date_trunc('day', now())",
            bot_id,
        )
        total_pnl = await conn.fetchrow(
            "SELECT COALESCE(sum(realized_pnl_usd), 0) AS pnl FROM positions WHERE bot_id = $1", bot_id,
        )
        last_fill = await conn.fetchrow(
            "SELECT max(created_at) AS at FROM intents WHERE bot_id = $1", bot_id,
        )

        heartbeat_at = heartbeat["at"] if heartbeat else None
        is_stale = heartbeat_at is None or dt.datetime.now(dt.timezone.utc) - heartbeat_at > BOT_HEARTBEAT_STALE_AFTER

        halted, halt_reason = await halt_state(conn, bot_id)

        envelope_usd = Decimal(str(config.get("risk", {}).get("envelope_usd") or 0))

        rows.append({
            "bot_id": bot_id,
            "mode": config.get("mode"),
            "version": b["version"],
            # Same "current total capital" figure bot_detail.html's "Wallet
            # (envelope + realized)" stat already shows — envelope alone is just
            # the operator's original config number, not useful at a glance next
            # to Deployed (which is live state); portfolio = what the bot's
            # actually worth right now.
            "portfolio_usd": envelope_usd + total_pnl["pnl"],
            "deployed_usd": deployed["deployed"],
            "today_pnl_usd": today_pnl["pnl"],
            "total_pnl_usd": total_pnl["pnl"],
            "halted": halted,
            "halt_reason": halt_reason,
            "last_fill_at": last_fill["at"],
            "heartbeat_at": heartbeat_at,
            "is_stale": is_stale,
            "archived_at": b["archived_at"],
            "archived_reason": b["archived_reason"],
        })

    # Chips describe the live fleet, so archived rows never inflate them — toggling
    # "show archived" changes what the table lists, not what the counts mean.
    live = [r for r in rows if not r["archived_at"]]

    if status:
        rows = [r for r in rows if _matches_status(r, status)]

    summary = {
        "count": len(live),
        "shown_count": len(rows),
        "active_count": sum(1 for r in live if not r["halted"]),
        "halted_count": sum(1 for r in live if r["halted"]),
        "stale_count": sum(1 for r in live if r["is_stale"]),
        "live_count": sum(1 for r in live if r["mode"] == "live"),
        "paper_count": sum(1 for r in live if r["mode"] == "paper"),
        "watch_count": sum(1 for r in live if r["mode"] == "watch"),
        "archived_count": archived_count,
        "total_today_pnl_usd": sum((r["today_pnl_usd"] for r in live), Decimal(0)),
    }
    return {"bots": rows, "summary": summary}


def _matches_status(row: dict, status: str) -> bool:
    """`running` means not halted and heartbeating — the fleet page's green pill
    without the stale subtitle, i.e. what an operator means by "actually up"."""
    if status == "archived":
        return row["archived_at"] is not None
    if row["archived_at"] is not None:
        return False
    if status == "halted":
        return row["halted"]
    if status == "stale":
        return row["is_stale"] and not row["halted"]
    if status == "running":
        return not row["halted"] and not row["is_stale"]
    return True


async def _fetch_market_question(client: httpx.AsyncClient, gamma_api_base_url: str, token_id: str) -> str | None:
    """Two real, mutually exclusive states to check, not one: querying
    `clob_token_ids=<id>` with no `closed` param returns only *open* markets, and
    `closed=true` returns *only* closed ones (confirmed live: an open market's token
    queried with `closed=true` comes back empty, not "includes it anyway"). A
    resolved-2-minutes-ago 5-minute crypto market genuinely needs the second call —
    found by testing against a real position the first version of this function
    still got wrong, not by reasoning about it in the abstract.
    """
    base = f"{gamma_api_base_url.rstrip('/')}/markets"
    resp = await client.get(base, params={"clob_token_ids": token_id})
    if resp.status_code == 200 and resp.json():
        return resp.json()[0].get("question")
    resp = await client.get(base, params={"clob_token_ids": token_id, "closed": "true"})
    if resp.status_code == 200 and resp.json():
        return resp.json()[0].get("question")
    return None


async def get_market_titles(gamma_api_base_url: str, token_ids: set[str]) -> dict[str, str]:
    """token_id alone (a long numeric CLOB id) means nothing to a human looking at
    the dashboard — this resolves it to the actual market question via Gamma.

    One request per token per state, not a comma-separated batch: a batch looked
    like it worked in an initial check (two of the *same* token, comma-joined,
    returned two rows) but two genuinely *different* token ids in one call returns
    `{"type": "validation error", "error": "invalid clob token ids"}` — confirmed
    live, and only caught because the dashboard was actually showing the wrong thing
    for a real position, not because the first test was thorough enough.
    """
    if not token_ids:
        return {}
    titles: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=10) as client:
        results = await asyncio.gather(*(
            _fetch_market_question(client, gamma_api_base_url, tid) for tid in token_ids
        ), return_exceptions=True)
    for tid, question in zip(token_ids, results):
        if isinstance(question, Exception) or question is None:
            continue
        titles[tid] = question
    return titles


POSITIONS_PAGE_SIZE = 20
DECISIONS_PAGE_SIZE = 25
LOGS_PAGE_SIZE = 30


def _page_info(page: int, page_size: int, total: int) -> dict:
    total_pages = max((total + page_size - 1) // page_size, 1)
    page = min(max(page, 1), total_pages)
    return {"page": page, "page_size": page_size, "total": total, "total_pages": total_pages, "offset": (page - 1) * page_size}


async def bot_detail(
    conn: asyncpg.Connection, bot_id: str, gamma_api_base_url: str,
    pos_page: int = 1, dec_page: int = 1, log_page: int = 1,
) -> dict:
    pos_total = await conn.fetchval("SELECT count(*) FROM positions WHERE bot_id = $1", bot_id)
    pos_pg = _page_info(pos_page, POSITIONS_PAGE_SIZE, pos_total)
    # Both prices are blended over the whole life of the position, and neither can be
    # read off the position row. cost_basis_usd/shares looks like the entry price but
    # isn't: basis is relieved as shares go out, so on a partially-sold position it
    # covers only the remainder while realized_pnl_usd covers every share ever held.
    # Those two divided against each other produced a negative exit price on the one
    # position here that was actually sold, and agreed with this query on the 97 that
    # never were — which is exactly the shape of bug that ships.
    #
    # exit_price counts resolution as an exit, because for a binary token it is one:
    # settling at $1.00 pays exactly what selling at $1.00 would. Sourcing it from SELL
    # fills instead left it empty on 97 of 98 rows, which reads as missing data rather
    # than as "held to resolution".
    #
    # (total bought + realized_pnl) / total bought shares is what makes a partial exit
    # come out right: once a position is closed, realized_pnl is proceeds minus the
    # full cost of every share disposed, so adding back what we paid recovers total
    # proceeds — sale money and settlement money together. The position that was partly
    # sold at $0.149 and partly written off blends to $0.126, which neither leg alone
    # would show. Rounding the resulting price (not the proceeds) absorbs the sub-cent
    # dust from re-deriving totals off a 6dp avg_fill_price — $0.000027 over 143 shares
    # was enough to render "$-0.000" — while leaving a genuinely wrong figure visible.
    positions = [dict(r) for r in await conn.fetch(
        """
        WITH buys AS (
            SELECT token_id, mode,
                   sum(filled_shares) AS shares,
                   sum(filled_shares * avg_fill_price) AS notional
            FROM orders
            WHERE bot_id = $1 AND side = 'BUY' AND filled_shares > 0 AND avg_fill_price IS NOT NULL
            GROUP BY token_id, mode
        ), copied_from AS (
            -- Which wallet(s) this position was opened off. A position has no fill_id
            -- of its own, so attribution goes back through the COPY intents on the
            -- token. Aggregated rather than picked because two targets trading the
            -- same token is rare but real (one token in the data when this was
            -- written): a plain join would silently duplicate the position row and
            -- double it in every column on the page.
            SELECT i.token_id, i.mode,
                   array_agg(DISTINCT COALESCE(ts.alias, left(f.target, 10) || '…')) AS targets
            FROM intents i
            JOIN target_fills f ON f.id = i.fill_id
            LEFT JOIN target_stats ts ON ts.target = f.target
            WHERE i.bot_id = $1 AND i.decision = 'COPY'
            GROUP BY i.token_id, i.mode
        )
        SELECT p.token_id, p.shares, p.cost_basis_usd, p.realized_pnl_usd, p.lifecycle,
               b.notional / NULLIF(b.shares, 0) AS entry_price,
               CASE WHEN p.lifecycle IN ('redeemed', 'refunded', 'written_off')
                    THEN round((b.notional + p.realized_pnl_usd) / NULLIF(b.shares, 0), 4)
               END AS exit_price,
               cf.targets
        FROM positions p
        LEFT JOIN buys b ON b.token_id = p.token_id AND b.mode = p.mode
        LEFT JOIN copied_from cf ON cf.token_id = p.token_id AND cf.mode = p.mode
        WHERE p.bot_id = $1 ORDER BY p.last_event_at DESC LIMIT $2 OFFSET $3
        """,
        bot_id, pos_pg["page_size"], pos_pg["offset"],
    )]

    # "By wallet": which targets are actually paying. Attribution is to the wallet
    # whose fill *opened* the position (earliest COPY intent on the token), not to
    # every wallet that touched it — a position's PnL is one number and splitting it
    # across contributors would need a cost allocation this page has no basis for.
    # Multi-wallet tokens are rare (one in the data when this was written), so the two
    # readings barely differ; the per-position table above still shows every one.
    by_wallet = [dict(r) for r in await conn.fetch(
        """
        WITH buys AS (
            SELECT token_id, mode,
                   sum(filled_shares) AS shares,
                   sum(filled_shares * avg_fill_price) AS notional
            FROM orders
            WHERE bot_id = $1 AND side = 'BUY' AND filled_shares > 0 AND avg_fill_price IS NOT NULL
            GROUP BY token_id, mode
        ), opened_by AS (
            SELECT DISTINCT ON (i.token_id, i.mode) i.token_id, i.mode,
                   COALESCE(ts.alias, left(f.target, 10) || '…') AS wallet
            FROM intents i
            JOIN target_fills f ON f.id = i.fill_id
            LEFT JOIN target_stats ts ON ts.target = f.target
            WHERE i.bot_id = $1 AND i.decision = 'COPY'
            ORDER BY i.token_id, i.mode, i.created_at
        ), closed AS (
            SELECT o.wallet, p.lifecycle, p.realized_pnl_usd, b.notional, b.shares
            FROM positions p
            JOIN opened_by o ON o.token_id = p.token_id AND o.mode = p.mode
            LEFT JOIN buys b ON b.token_id = p.token_id AND b.mode = p.mode
            WHERE p.bot_id = $1
        )
        SELECT wallet,
               count(*) AS positions,
               count(*) FILTER (WHERE lifecycle IN ('redeemed', 'refunded', 'written_off')) AS closed,
               count(*) FILTER (WHERE lifecycle = 'redeemed') AS wins,
               avg(notional / NULLIF(shares, 0)) AS avg_entry,
               sum(realized_pnl_usd) AS pnl,
               sum(realized_pnl_usd) / NULLIF(
                   sum(notional) FILTER (WHERE lifecycle IN ('redeemed', 'refunded', 'written_off')), 0
               ) AS roi
        FROM closed GROUP BY wallet ORDER BY sum(realized_pnl_usd)
        """,
        bot_id,
    )]
    for w in by_wallet:
        w["win_rate"] = (w["wins"] / w["closed"]) if w["closed"] else None

    dec_total = await conn.fetchval("SELECT count(*) FROM intents WHERE bot_id = $1", bot_id)
    dec_pg = _page_info(dec_page, DECISIONS_PAGE_SIZE, dec_total)
    recent_intents = [dict(r) for r in await conn.fetch(
        """
        SELECT i.decision, i.skip_reason, i.skip_detail, i.token_id, i.side, i.target_price, i.intended_price,
               i.intended_shares, i.target_percentile, i.created_at,
               COALESCE(ts.alias, left(f.target, 10) || '…') AS target_alias
        FROM intents i
        JOIN target_fills f ON f.id = i.fill_id
        LEFT JOIN target_stats ts ON ts.target = f.target
        WHERE i.bot_id = $1 ORDER BY i.created_at DESC LIMIT $2 OFFSET $3
        """,
        bot_id, dec_pg["page_size"], dec_pg["offset"],
    )]
    for i in recent_intents:
        if i["skip_detail"] is not None:
            i["skip_detail"] = _jsonb(i["skip_detail"])
        if i["skip_reason"]:
            i["skip_label"] = _skip_label(i["skip_reason"])
            i["skip_category"] = _skip_category(i["skip_reason"])
            i["skip_summary"] = _skip_summary(i["skip_reason"], i["skip_detail"])
    skips_by_reason = [dict(r) for r in await conn.fetch(
        "SELECT skip_reason, count(*) AS n FROM intents WHERE bot_id = $1 AND decision = 'SKIP' "
        "AND created_at >= now() - interval '7 days' GROUP BY skip_reason ORDER BY n DESC",
        bot_id,
    )]
    for s in skips_by_reason:
        s["skip_label"] = _skip_label(s["skip_reason"])
        s["skip_category"] = _skip_category(s["skip_reason"])

    # Assigned over every wallet named anywhere on the page rather than per table, so
    # one wallet reads the same colour in the summary card and in the rows beneath it.
    wallet_aliases: set[str] = {w["wallet"] for w in by_wallet}
    for p in positions:
        wallet_aliases.update(p["targets"] or [])
    wallet_aliases.update(i["target_alias"] for i in recent_intents if i["target_alias"])
    wallet_colours = assign_wallet_colours(wallet_aliases)
    equity_curve = await conn.fetch(
        """
        SELECT bucket, sum(pnl) OVER (ORDER BY bucket) AS cumulative_pnl
        FROM (
            SELECT date_trunc('hour', last_event_at) AS bucket, sum(realized_pnl_usd) AS pnl
            FROM positions WHERE bot_id = $1 GROUP BY bucket
        ) hourly
        ORDER BY bucket
        """,
        bot_id,
    )
    log_total = await conn.fetchval("SELECT count(*) FROM events WHERE bot_id = $1", bot_id)
    log_pg = _page_info(log_page, LOGS_PAGE_SIZE, log_total)
    logs = [dict(r) for r in await conn.fetch(
        "SELECT level, component, message, context, at FROM events WHERE bot_id = $1 ORDER BY at DESC LIMIT $2 OFFSET $3",
        bot_id, log_pg["page_size"], log_pg["offset"],
    )]
    for l in logs:
        l["context"] = _jsonb(l["context"]) if l["context"] is not None else None
        l["context_summary"] = _context_summary(l["context"])

    config_row = await conn.fetchrow("SELECT config FROM bot_config WHERE bot_id = $1 AND active", bot_id)
    config = _jsonb(config_row["config"]) if config_row else {}
    envelope_usd = config.get("risk", {}).get("envelope_usd")

    # Prev/next walk the same alphabetical order the fleet table is listed in, so
    # paging through bots from here matches the list you came from. Archived bots
    # are in the ring only when you're already looking at one — otherwise stepping
    # through the fleet would drop you on a retired bot.
    siblings = [r["bot_id"] for r in await conn.fetch(
        "SELECT bc.bot_id FROM bot_config bc LEFT JOIN bots b ON b.bot_id = bc.bot_id "
        "WHERE bc.active AND (b.archived_at IS NULL OR bc.bot_id = $1) ORDER BY bc.bot_id",
        bot_id,
    )]
    here = siblings.index(bot_id) if bot_id in siblings else None
    prev_bot = siblings[here - 1] if here else None
    next_bot = siblings[here + 1] if here is not None and here + 1 < len(siblings) else None

    summary = await conn.fetchrow(
        """
        SELECT
            COALESCE(sum(realized_pnl_usd), 0) AS total_realized_pnl,
            COALESCE(sum(cost_basis_usd) FILTER (WHERE lifecycle = 'open'), 0) AS deployed_usd,
            count(*) FILTER (WHERE lifecycle = 'open') AS open_positions,
            -- "Pending" is everything that's left the market but hasn't paid out yet:
            -- awaiting resolution, resolved-but-unredeemed, or disputed. Grouped
            -- because the operator's question is the same for all three ("how much is
            -- stuck waiting on someone else"), and each one alone is usually zero.
            count(*) FILTER (WHERE lifecycle IN ('pending_resolution', 'resolved', 'disputed')) AS pending_positions,
            count(*) FILTER (WHERE lifecycle IN ('redeemed', 'written_off', 'refunded') AND realized_pnl_usd > 0) AS wins,
            count(*) FILTER (WHERE lifecycle IN ('redeemed', 'written_off', 'refunded') AND realized_pnl_usd <= 0) AS losses
        FROM positions WHERE bot_id = $1
        """,
        bot_id,
    )
    closed_trades = summary["wins"] + summary["losses"]

    # Wagered is gross buying, not net exposure: every dollar that has gone out the
    # door, including the ones already recycled into the next position. It's the
    # denominator that makes a $15 PnL on a $50 envelope legible — $15 on $1,331
    # turned over is a different bot from $15 on $60.
    wagered = await conn.fetchrow(
        """
        SELECT COALESCE(sum(filled_shares * avg_fill_price), 0) AS wagered_usd,
               count(*) AS trades
        FROM orders
        WHERE bot_id = $1 AND side = 'BUY' AND filled_shares > 0 AND avg_fill_price IS NOT NULL
        """,
        bot_id,
    )
    # Dated from the first decision rather than the first fill: a bot that ran for a
    # day before it found anything worth copying has been running for a day.
    started_at = await conn.fetchval("SELECT min(created_at) FROM intents WHERE bot_id = $1", bot_id)
    heartbeat_at = await conn.fetchval("SELECT at FROM heartbeats WHERE service = $1", f"bot:{bot_id}")
    heartbeat_ok = heartbeat_at is not None and (
        dt.datetime.now(dt.timezone.utc) - heartbeat_at <= BOT_HEARTBEAT_STALE_AFTER
    )
    halted, halt_reason = await halt_state(conn, bot_id)

    token_ids = {p["token_id"] for p in positions} | {i["token_id"] for i in recent_intents}
    titles = await get_market_titles(gamma_api_base_url, token_ids)
    for p in positions:
        p["market_title"] = titles.get(p["token_id"])
    for i in recent_intents:
        i["market_title"] = titles.get(i["token_id"])

    return {
        "positions": positions,
        "by_wallet": by_wallet,
        "wallet_colours": wallet_colours,
        "recent_intents": recent_intents,
        "skips_by_reason": skips_by_reason,
        "equity_curve": equity_curve,
        "logs": logs,
        "envelope_usd": envelope_usd,
        "total_realized_pnl": summary["total_realized_pnl"],
        "deployed_usd": summary["deployed_usd"],
        "open_positions": summary["open_positions"],
        "pending_positions": summary["pending_positions"],
        "wins": summary["wins"],
        "losses": summary["losses"],
        "closed_trades": closed_trades,
        "win_rate": (summary["wins"] / closed_trades) if closed_trades > 0 else None,
        "avg_pnl_per_trade": (summary["total_realized_pnl"] / closed_trades) if closed_trades > 0 else None,
        "wagered_usd": wagered["wagered_usd"],
        "trade_count": wagered["trades"],
        "avg_trade_usd": (wagered["wagered_usd"] / wagered["trades"]) if wagered["trades"] else None,
        "started_at": started_at,
        "heartbeat_at": heartbeat_at,
        "heartbeat_ok": heartbeat_ok,
        "mode": config.get("mode"),
        "halted": halted,
        "halt_reason": halt_reason,
        "prev_bot": prev_bot,
        "next_bot": next_bot,
        "pos_pg": pos_pg, "dec_pg": dec_pg, "log_pg": log_pg,
        "archived": await conn.fetchrow(
            "SELECT archived_at, archived_reason FROM bots WHERE bot_id = $1 AND archived_at IS NOT NULL", bot_id,
        ),
    }


async def list_bot_ids(conn: asyncpg.Connection) -> list[str]:
    """Bots a target can be attached to — archived ones excluded, since attaching
    new work to a retired bot is never intended. This does not affect reachability
    of archived bots: /logs and /bots/<id> address them by bot_id directly rather
    than picking from this list.
    """
    rows = await conn.fetch(
        "SELECT bc.bot_id FROM bot_config bc LEFT JOIN bots b ON b.bot_id = bc.bot_id "
        "WHERE bc.active AND b.archived_at IS NULL ORDER BY bc.bot_id"
    )
    return [r["bot_id"] for r in rows]


STATS_STALE_AFTER = dt.timedelta(minutes=30)  # targets-recompute runs every 15 minutes; 2x that before flagging


async def targets_view(conn: asyncpg.Connection) -> dict:
    rows = await conn.fetch(
        "SELECT target, alias, status, size_p50, size_p80, size_p95, fills_30d, "
        "hit_rate_30d, pnl_30d_usd, reversal_rate, last_fill_at, computed_at FROM target_stats ORDER BY target"
    )
    stalest_computed_at = min((r["computed_at"] for r in rows), default=None)
    stats_stale = stalest_computed_at is not None and dt.datetime.now(dt.timezone.utc) - stalest_computed_at > STATS_STALE_AFTER
    return {"targets": rows, "stalest_computed_at": stalest_computed_at, "stats_stale": stats_stale}


async def target_detail(conn: asyncpg.Connection, target: str) -> dict | None:
    """Our side of a target's page: what we ingested from this wallet and what our
    bots decided to do about it. Returns None if the wallet isn't registered.

    The wallet's *own* trading history is not here — that comes live from the Data
    API (targets/profile.py). This half is the thing Polymarket's own profile page
    can't show: for each of their fills we saw, did we copy it or skip it and why.
    """
    row = await conn.fetchrow(
        "SELECT target, alias, status, size_p50, size_p60, size_p80, size_p95, fills_30d, "
        "hit_rate_30d, pnl_30d_usd, reversal_rate, last_fill_at, computed_at "
        "FROM target_stats WHERE target = $1",
        target,
    )
    if row is None:
        return None

    # This is the wallet's own trading, as we observed it — not a report on our
    # plumbing. `target_fills` is a record of *their* fills; the only thing that makes
    # it ours is that it starts the day we began watching, which is why first_fill is
    # surfaced alongside it rather than left implicit.
    #
    # avg_lag_s is the one genuinely operational number here, kept because a wallet
    # whose fills reach us minutes late is one we cannot actually copy, however good
    # its trading looks.
    trading = await conn.fetchrow(
        """
        SELECT count(*) AS fills,
               count(DISTINCT token_id) AS tokens,
               min(block_ts) AS first_fill,
               max(block_ts) AS last_fill,
               COALESCE(sum(notional_usd), 0) AS notional,
               avg(notional_usd) AS avg_notional,
               count(*) FILTER (WHERE side = 'BUY') AS buys,
               count(*) FILTER (WHERE side = 'SELL') AS sells,
               count(DISTINCT date_trunc('day', block_ts)) AS days_active,
               count(*) FILTER (WHERE source = 'chain') AS from_chain,
               avg(extract(epoch FROM (detected_at - block_ts))) AS avg_lag_s
        FROM target_fills WHERE target = $1
        """,
        target,
    )
    trading = dict(trading)
    trading["fills_per_day"] = (
        trading["fills"] / trading["days_active"] if trading["days_active"] else None
    )

    # One fill produces one intent per bot following this wallet, so these counts are
    # decisions, not fills — five bots skipping the same fill is five rows. Counting
    # distinct fill_id instead would hide that two bots disagreed about it, which is
    # exactly what you come to this page to see.
    decisions = [dict(r) for r in await conn.fetch(
        """
        SELECT i.decision, i.skip_reason, count(*) AS n, count(DISTINCT i.bot_id) AS bots
        FROM intents i JOIN target_fills f ON f.id = i.fill_id
        WHERE f.target = $1
        GROUP BY i.decision, i.skip_reason ORDER BY n DESC
        """,
        target,
    )]
    copies = sum(d["n"] for d in decisions if d["decision"] == "COPY")
    skips = [d for d in decisions if d["decision"] == "SKIP"]
    for s in skips:
        s["skip_label"] = _skip_label(s["skip_reason"])
        s["skip_category"] = _skip_category(s["skip_reason"])

    following_bots = [r["bot_id"] for r in await conn.fetch(
        "SELECT DISTINCT i.bot_id FROM intents i JOIN target_fills f ON f.id = i.fill_id "
        "WHERE f.target = $1 ORDER BY i.bot_id",
        target,
    )]

    # PnL our bots made following this wallet. Attribution is to the wallet whose
    # fill *opened* the position (earliest COPY intent on that token, per bot+mode) —
    # the same rule the bot page's "by wallet" table uses, so the two agree.
    pnl = await conn.fetchrow(
        """
        WITH opened_by AS (
            SELECT DISTINCT ON (i.bot_id, i.token_id, i.mode)
                   i.bot_id, i.token_id, i.mode, f.target
            FROM intents i JOIN target_fills f ON f.id = i.fill_id
            WHERE i.decision = 'COPY'
            ORDER BY i.bot_id, i.token_id, i.mode, i.created_at
        )
        SELECT count(*) AS positions,
               count(*) FILTER (WHERE p.lifecycle IN ('redeemed','refunded','written_off')) AS closed,
               count(*) FILTER (WHERE p.lifecycle IN ('redeemed','refunded','written_off') AND p.realized_pnl_usd > 0) AS wins,
               count(*) FILTER (WHERE p.lifecycle = 'open') AS open_positions,
               COALESCE(sum(p.realized_pnl_usd), 0) AS realized_pnl
        FROM positions p
        JOIN opened_by o ON o.bot_id = p.bot_id AND o.token_id = p.token_id AND o.mode = p.mode
        WHERE o.target = $1
        """,
        target,
    )

    siblings = [r["target"] for r in await conn.fetch("SELECT target FROM target_stats ORDER BY target")]
    here = siblings.index(target) if target in siblings else None
    prev_target = siblings[here - 1] if here else None
    next_target = siblings[here + 1] if here is not None and here + 1 < len(siblings) else None

    return {
        "target": dict(row),
        "trading": trading,
        "copies": copies,
        "skips": skips,
        "skip_total": sum(s["n"] for s in skips),
        "following_bots": following_bots,
        "pnl": dict(pnl),
        "win_rate": (pnl["wins"] / pnl["closed"]) if pnl["closed"] else None,
        "prev_target": prev_target,
        "next_target": next_target,
    }


async def decisions_for_tokens(conn: asyncpg.Connection, target: str, token_ids: list[str]) -> dict[str, dict]:
    """Our verdict on each market in the visible slice of a wallet's activity feed.

    Keyed by token_id and aggregated across bots and across repeat visits to the same
    market — a per-fill join isn't available, because the Data API's activity rows and
    our `target_fills` rows have no shared identifier (their `asset`+timestamp vs our
    dedupe_key). Market-level is the honest granularity, and the template labels it
    as such rather than implying it's the verdict on that one trade.

    A token absent from the result means we have no decision on record for it at all:
    usually that it predates our ingest of this wallet, not that we passed on it.
    """
    if not token_ids:
        return {}
    rows = await conn.fetch(
        """
        SELECT i.token_id,
               count(*) FILTER (WHERE i.decision = 'COPY') AS copies,
               count(*) FILTER (WHERE i.decision = 'SKIP') AS skips,
               array_remove(array_agg(DISTINCT i.skip_reason), NULL) AS reasons
        FROM intents i JOIN target_fills f ON f.id = i.fill_id
        WHERE f.target = $1 AND i.token_id = ANY($2::text[])
        GROUP BY i.token_id
        """,
        target, token_ids,
    )
    out: dict[str, dict] = {}
    for r in rows:
        reasons = list(r["reasons"] or [])
        out[r["token_id"]] = {
            "copies": r["copies"],
            "skips": r["skips"],
            # The dominant reason is enough for a table cell; the full breakdown for
            # the wallet is in the skip-reason card above it.
            "skip_reason": reasons[0] if reasons else None,
            "skip_label": _skip_label(reasons[0]) if reasons else None,
            "skip_category": _skip_category(reasons[0]) if reasons else None,
            "extra_reasons": len(reasons) - 1 if len(reasons) > 1 else 0,
        }
    return out


async def logs_view(
    conn: asyncpg.Connection, bot_id: str | None, component: str | None, level: str | None, page: int = 1,
) -> dict:
    """The global /logs page (FR-C-2). `component` defaults to excluding the `paper`
    research logger — it's ~98% of raw event volume in practice (one row per
    simulated fill, across every fill of every watched target, not just this bot's
    own decisions) and drowns out the handful of actually operational rows
    (watcher.chain reconnects, bot.health halts) that make this page worth looking
    at. `component="all"` opts back in. Found investigating a report that this page
    was unreadable — it was, but the underlying rows (context especially) were
    already fine; nothing here was previously rendered or filterable at all.
    """
    all_components = [r["component"] for r in await conn.fetch("SELECT DISTINCT component FROM events ORDER BY component")]
    all_levels = [r["level"] for r in await conn.fetch("SELECT DISTINCT level FROM events ORDER BY level")]

    where = []
    params: list = []
    if bot_id:
        params.append(bot_id)
        where.append(f"bot_id = ${len(params)}")
    if component == "all":
        pass
    elif component:
        params.append(component)
        where.append(f"component = ${len(params)}")
    else:
        params.append("paper")
        where.append(f"component != ${len(params)}")
    if level:
        params.append(level)
        where.append(f"level = ${len(params)}")
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    total = await conn.fetchval(f"SELECT count(*) FROM events {where_sql}", *params)
    pg = _page_info(page, LOGS_PAGE_SIZE, total)
    rows = await conn.fetch(
        f"SELECT bot_id, level, component, message, context, at FROM events {where_sql} "
        f"ORDER BY at DESC LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}",
        *params, pg["page_size"], pg["offset"],
    )
    logs = [dict(r) for r in rows]
    for l in logs:
        l["context"] = _jsonb(l["context"]) if l["context"] is not None else None
        l["context_summary"] = _context_summary(l["context"])

    return {
        "logs": logs, "pg": pg,
        "all_components": all_components, "all_levels": all_levels,
        "component": component, "level": level, "bot_id": bot_id,
    }


# Histogram buckets for total copy-trade latency (chain-sourced fills only — see
# latency_view). Edges as (lo, hi, label); hi=None means "and up".
_LATENCY_BUCKETS: list[tuple[float, float | None, str]] = [
    (0, 0.5, "<0.5s"), (0.5, 1, "0.5–1s"), (1, 1.5, "1–1.5s"), (1.5, 2, "1.5–2s"),
    (2, 2.5, "2–2.5s"), (2.5, 3, "2.5–3s"), (3, 4, "3–4s"), (4, 5, "4–5s"),
    (5, 10, "5–10s"), (10, None, ">10s"),
]

# Shared by every latency_view query — one CTE per stage boundary (PRD §5 tables:
# target_fills.block_ts/detected_at, intents.created_at, orders.created_at,
# order_transitions.at) rather than a single denormalized view, since order state
# is itself an event log (multiple transitions per order), not a column.
_LATENCY_CTE = """
    WITH sub AS (
        SELECT order_id, MIN(at) AS submitted_at FROM order_transitions WHERE to_state = 'submitted' GROUP BY order_id
    ),
    term AS (
        SELECT order_id, MIN(at) AS terminal_at, MIN(to_state) AS terminal_state
        FROM order_transitions WHERE to_state IN ('filled','partial','rejected','cancelled','expired')
        GROUP BY order_id
    ),
    copy_orders AS (
        SELECT
            f.id AS fill_id, f.target, f.token_id, f.side, f.source, f.block_ts, f.detected_at,
            i.created_at AS intent_at, o.id AS order_id, o.created_at AS order_built_at,
            sub.submitted_at, term.terminal_at, term.terminal_state,
            EXTRACT(EPOCH FROM (f.detected_at - f.block_ts)) AS detect_lat,
            EXTRACT(EPOCH FROM (i.created_at - f.detected_at)) AS decision_lat,
            EXTRACT(EPOCH FROM (o.created_at - i.created_at)) AS build_lat,
            EXTRACT(EPOCH FROM (sub.submitted_at - o.created_at)) AS submit_lat,
            EXTRACT(EPOCH FROM (term.terminal_at - sub.submitted_at)) AS ack_lat,
            EXTRACT(EPOCH FROM (term.terminal_at - f.block_ts)) AS total_lat,
            EXTRACT(EPOCH FROM (term.terminal_at - f.detected_at)) AS our_side_lat
        FROM target_fills f
        JOIN intents i ON i.fill_id = f.id AND i.decision = 'COPY'
        JOIN orders o ON o.intent_id = i.id
        LEFT JOIN sub ON sub.order_id = o.id
        LEFT JOIN term ON term.order_id = o.id
    )
"""


async def latency_view(conn: asyncpg.Connection) -> dict:
    """Copy-trade latency: on-chain fill -> our order reaching a terminal state,
    across every COPY decision that produced an order (FR-C-2 analysis screen).

    `chain`-sourced fills have a watcher-observed block_ts trustworthy enough to
    anchor a latency measurement. `dataapi`-sourced ones don't — that source's
    reported block_ts occasionally lands *after* our own detected_at (an indexing
    artifact of polling a REST backfill, not real negative latency) — so they're
    reported separately, measured from our own detection instead of the chain
    timestamp, rather than blended into headline numbers that would then include
    fabricated negative latencies.
    """
    totals = await conn.fetchrow(_LATENCY_CTE + """
        SELECT
            count(*) AS n_total,
            count(*) FILTER (WHERE source = 'chain') AS n_chain,
            count(*) FILTER (WHERE source = 'dataapi') AS n_dataapi
        FROM copy_orders
    """)

    stage = await conn.fetchrow(_LATENCY_CTE + """
        SELECT
            percentile_cont(0.5) WITHIN GROUP (ORDER BY detect_lat) AS detect_p50,
            percentile_cont(0.9) WITHIN GROUP (ORDER BY detect_lat) AS detect_p90,
            percentile_cont(0.5) WITHIN GROUP (ORDER BY decision_lat) AS decision_p50,
            percentile_cont(0.9) WITHIN GROUP (ORDER BY decision_lat) AS decision_p90,
            percentile_cont(0.5) WITHIN GROUP (ORDER BY build_lat) AS build_p50,
            percentile_cont(0.9) WITHIN GROUP (ORDER BY build_lat) AS build_p90,
            percentile_cont(0.5) WITHIN GROUP (ORDER BY submit_lat) AS submit_p50,
            percentile_cont(0.9) WITHIN GROUP (ORDER BY submit_lat) AS submit_p90,
            percentile_cont(0.5) WITHIN GROUP (ORDER BY ack_lat) AS ack_p50,
            percentile_cont(0.9) WITHIN GROUP (ORDER BY ack_lat) AS ack_p90,
            percentile_cont(0.5) WITHIN GROUP (ORDER BY total_lat) AS total_p50,
            percentile_cont(0.9) WITHIN GROUP (ORDER BY total_lat) AS total_p90,
            percentile_cont(0.99) WITHIN GROUP (ORDER BY total_lat) AS total_p99,
            max(total_lat) AS total_max,
            avg(total_lat) AS total_mean,
            100.0 * count(*) FILTER (WHERE total_lat <= 4) / NULLIF(count(*), 0) AS pct_within_4s
        FROM copy_orders WHERE source = 'chain'
    """)

    dataapi = await conn.fetchrow(_LATENCY_CTE + """
        SELECT
            percentile_cont(0.5) WITHIN GROUP (ORDER BY our_side_lat) AS p50,
            percentile_cont(0.9) WITHIN GROUP (ORDER BY our_side_lat) AS p90
        FROM copy_orders WHERE source = 'dataapi'
    """)

    outcome_rows = await conn.fetch(_LATENCY_CTE + """
        SELECT terminal_state, count(*) AS n FROM copy_orders WHERE terminal_state IS NOT NULL GROUP BY terminal_state
    """)
    outcome_n = {r["terminal_state"]: r["n"] for r in outcome_rows}
    outcome_total = sum(outcome_n.values()) or 1
    outcomes = {
        state: {"n": outcome_n.get(state, 0), "pct": round(100 * outcome_n.get(state, 0) / outcome_total)}
        for state in ("filled", "partial", "rejected")
    }

    bucket_case = " ".join(
        (f"WHEN total_lat >= {lo} AND total_lat < {hi} THEN '{label}'" if hi is not None
         else f"WHEN total_lat >= {lo} THEN '{label}'")
        for lo, hi, label in _LATENCY_BUCKETS
    )
    hist_rows = await conn.fetch(_LATENCY_CTE + f"""
        SELECT CASE {bucket_case} END AS bucket, count(*) AS n
        FROM copy_orders WHERE source = 'chain' AND total_lat IS NOT NULL
        GROUP BY bucket
    """)
    hist_n = {r["bucket"]: r["n"] for r in hist_rows}
    histogram = [{"label": label, "n": hist_n.get(label, 0)} for _, _, label in _LATENCY_BUCKETS]
    max_bucket = max((h["n"] for h in histogram), default=0)
    for h in histogram:
        h["pct"] = round(100 * h["n"] / max_bucket) if max_bucket else 0

    slowest = await conn.fetch(_LATENCY_CTE + """
        SELECT co.fill_id, co.target, ts.alias, co.side, co.block_ts, co.total_lat, co.detect_lat, co.terminal_state
        FROM copy_orders co
        LEFT JOIN target_stats ts ON ts.target = co.target
        WHERE co.source = 'chain' AND co.total_lat IS NOT NULL
        ORDER BY co.total_lat DESC LIMIT 10
    """)

    stage_d = dict(stage) if stage else {}
    total_p50 = float(stage_d.get("total_p50") or 0)
    funnel = [
        {"key": "detect", "label": "On-chain → watcher detects", "p50": stage_d.get("detect_p50"), "p90": stage_d.get("detect_p90")},
        {"key": "decision", "label": "Detected → policy decision", "p50": stage_d.get("decision_p50"), "p90": stage_d.get("decision_p90")},
        {"key": "build", "label": "Decision → order built", "p50": stage_d.get("build_p50"), "p90": stage_d.get("build_p90")},
        {"key": "submit", "label": "Order built → submitted", "p50": stage_d.get("submit_p50"), "p90": stage_d.get("submit_p90")},
        {"key": "ack", "label": "Submitted → terminal state", "p50": stage_d.get("ack_p50"), "p90": stage_d.get("ack_p90")},
    ]
    for stg in funnel:
        p50 = float(stg["p50"] or 0)
        stg["pct"] = min(100, round(100 * p50 / total_p50)) if total_p50 else 0

    return {
        "n_total": totals["n_total"],
        "n_chain": totals["n_chain"],
        "n_dataapi": totals["n_dataapi"],
        "stage": stage_d,
        "funnel": funnel,
        "dataapi": dict(dataapi) if dataapi else {},
        "outcomes": outcomes,
        "histogram": histogram,
        "slowest": [dict(r) for r in slowest],
    }
