import csv
import functools
from collections import defaultdict
from decimal import Decimal
from pathlib import Path


def check_known(connection, column, given):
    """Raise if any value in `given` isn't in instruments.<column>. None means no filter."""
    if given is None:
        return
    known = {row[0] for row in connection.execute(f"SELECT DISTINCT {column} FROM instruments").fetchall()}
    unknown = set(given) - known
    if unknown:
        raise ValueError(f"Unknown {column}: {sorted(unknown)}")


# Join and filter shared by every query on a selection. Cash rows have no instrument,
# so they pass only on include_cash; holding rows must pass every filter that is set.
SELECTION = """
    FROM daily_valuation v
    LEFT JOIN instruments i ON i.instrument_id = v.instrument_id
    WHERE list_contains($account_ids, v.account_id)
      AND (
        (v.instrument_id IS NULL AND $include_cash)
        OR (
          v.instrument_id IS NOT NULL
          AND ($tickers IS NULL OR list_contains($tickers, i.price_ticker))
          AND ($asset_types IS NULL OR list_contains($asset_types, i.asset_type))
        )
      )
"""


def portfolio_value(connection, account_ids, asset_types=None, tickers=None, include_cash=True):
    """Total value in EUR per day of the selection, as (date, total_value) rows.

    asset_types and tickers of None mean no filter; all filters combine with AND.
    """
    if not account_ids:
        raise ValueError("account_ids cannot be empty")
    check_known(connection, "price_ticker", tickers)
    check_known(connection, "asset_type", asset_types)

    params = {
        "account_ids": account_ids,
        "tickers": tickers,
        "asset_types": asset_types,
        "include_cash": include_cash,
    }

    # SUM skips NULLs, so a missing exchange rate would silently lower the total.
    missing = connection.execute(
        f"""
        SELECT v.date, v.account_id, v.currency
        {SELECTION}
          AND v.value_eur IS NULL
        ORDER BY v.date
        LIMIT 5
        """,
        params,
    ).fetchall()
    if missing:
        raise ValueError(f"No exchange rate for (date, account_id, currency): {missing}")

    return connection.execute(
        f"""
        SELECT v.date, SUM(v.value_eur) AS total_value
        {SELECTION}
        GROUP BY v.date
        ORDER BY v.date
        """,
        params,
    ).fetchall()


def average_cost_positions(connection, account_ids, asset_types=None, tickers=None):
    rows = connection.execute(
        """
        SELECT t.account_id, i.price_ticker, t.quantity, t.cash_eur FROM trades t
        JOIN instruments i ON i.instrument_id = t.instrument_id
        WHERE list_contains($account_ids, t.account_id)
          AND ($tickers IS NULL OR list_contains($tickers, i.price_ticker))
          AND ($asset_types IS NULL OR list_contains($asset_types, i.asset_type))
        ORDER BY t.event_date, t.transaction_id
        """
        ,
        {
            "account_ids": account_ids,
            "tickers": tickers,
            "asset_types": asset_types,
        }
    ).fetchall()

    positions = {}
    for account_id, ticker, quantity, cash_eur in rows:
        position = positions.setdefault(
            (account_id, ticker), {"qty": Decimal(0), "cost": Decimal(0), "realised": Decimal(0)}
        )
        if quantity > 0:
            position["qty"] += quantity
            position["cost"] += -(cash_eur)
        else:
            # Average cost: the sold shares take their share of the cost, at the qty before the sale.
            sold = -quantity
            sold_cost = position["cost"] * sold / position["qty"]
            position["realised"] += cash_eur - sold_cost
            position["cost"] -= sold_cost
            position["qty"] -= sold
    return positions


def holdings_snapshot(connection, account_ids, asset_types=None, tickers=None):
    if not account_ids:
        raise ValueError("account_ids cannot be empty")
    check_known(connection, "price_ticker", tickers)
    check_known(connection, "asset_type", asset_types)

    positions = average_cost_positions(connection, account_ids, asset_types, tickers)

    value_rows = connection.execute(
        f"""
        SELECT v.account_id, i.price_ticker, v.value_eur
        {SELECTION}
        AND v.date = (SELECT MAX(date) FROM daily_valuation)
        """,
        {
            "account_ids": account_ids,
            "tickers": tickers,
            "asset_types": asset_types,
            "include_cash": False,
        }
    ).fetchall()
    values = {(account_id, ticker): value_eur for account_id, ticker, value_eur in value_rows}
    by_ticker = {}
    for (account_id, ticker), position in positions.items():
        if position["qty"] > 0:
            total = by_ticker.setdefault(ticker, {"qty": Decimal(0), "cost": Decimal(0), "value": Decimal(0)})
            total["qty"] += position["qty"]
            total["cost"] += position["cost"]
            total["value"] += values[(account_id, ticker)]
    total_value = sum(total["value"] for total in by_ticker.values())
    result = []
    for ticker, sums in by_ticker.items():
        result.append({
            "ticker": ticker,
            "qty": sums["qty"],
            "avg_cost_eur": sums["cost"] / sums["qty"],
            "cost": sums["cost"],
            "value_eur": sums["value"],
            "weight": sums["value"] / total_value,
            "unrealised_pnl_eur": sums["value"] - sums["cost"],
            "unrealised_pnl_percent": (sums["value"] - sums["cost"]) / sums["cost"] * 100,
            "weight_pct": (sums["value"] / total_value) * 100,
        })
    result.sort(key=lambda row: row["value_eur"], reverse=True)
    return result


def realised_gains(connection, account_ids, asset_types=None, tickers=None):
    """Realised gain in EUR per ticker from sells at average cost, fully sold tickers included."""
    if not account_ids:
        raise ValueError("account_ids cannot be empty")
    check_known(connection, "price_ticker", tickers)
    check_known(connection, "asset_type", asset_types)

    by_ticker = {}
    for (_, ticker), position in average_cost_positions(connection, account_ids, asset_types, tickers).items():
        by_ticker[ticker] = by_ticker.get(ticker, Decimal(0)) + position["realised"]
    return [
        {"ticker": ticker, "realised_eur": realised}
        for ticker, realised in sorted(by_ticker.items())
        if realised != 0
    ]


COUNTRIES_FILE = Path(__file__).parent.parent / "reference" / "countries.csv"
UNCLASSIFIED = "Unclassified"


@functools.cache
def load_countries(path=COUNTRIES_FILE):
    """Country -> (region, market_type). Regions are geographic; market type is MSCI's."""
    with open(path, newline="", encoding="utf-8") as f:
        return {r["country"]: (r["region"], r["market_type"]) for r in csv.DictReader(f)}


# Groups that spread each holding over its weights in exposures; region and market roll countries up.
EXPOSURE_KINDS = {"country": "country", "region": "country", "market": "country", "sector": "sector"}
ROLL_UPS = {"region": 0, "market": 1}


def allocation(connection, account_ids, by="asset_type", asset_types=None, tickers=None, include_cash=True):
    """Latest value per group. Country, region and sector leave out cash and holdings without weights."""
    if not account_ids:
        raise ValueError("account_ids cannot be empty")
    check_known(connection, "price_ticker", tickers)
    check_known(connection, "asset_type", asset_types)

    GROUPS = {
        "asset_type": "COALESCE(i.asset_type, 'cash')",
        "currency": "v.currency",
        "account": "v.account_id",
        "ticker": "COALESCE(i.price_ticker, 'cash')",
    }
    if by not in GROUPS and by not in EXPOSURE_KINDS:
        raise ValueError(f"by must be one of {sorted([*GROUPS, *EXPOSURE_KINDS])}")

    params = {
        "account_ids": account_ids,
        "tickers": tickers,
        "asset_types": asset_types,
        "include_cash": include_cash,
    }
    if by in GROUPS:
        rows = connection.execute(
            f"""
            SELECT {GROUPS[by]} AS grp, SUM(v.value_eur) AS value_eur
            {SELECTION}
            AND v.date = (SELECT MAX(date) FROM daily_valuation)
            GROUP BY grp
            ORDER BY value_eur DESC
            """,
            params,
        ).fetchall()
        rows = [(grp, value, None) for grp, value in rows]
    else:
        parts = connection.execute(
            f"""
            SELECT e.name, s.price_ticker, SUM(s.value_eur * e.weight)
            FROM (
                SELECT v.instrument_id, i.price_ticker, v.value_eur
                {SELECTION}
                AND v.date = (SELECT MAX(date) FROM daily_valuation)
            ) s
            JOIN exposures e ON e.instrument_id = s.instrument_id AND e.kind = $kind
            GROUP BY e.name, s.price_ticker
            """,
            {**params, "include_cash": False, "kind": EXPOSURE_KINDS[by]},
        ).fetchall()

        if by in ROLL_UPS:
            countries = load_countries()
            unknown = {name for name, _, _ in parts} - countries.keys() - {UNCLASSIFIED}
            if unknown:
                raise ValueError(f"Countries missing from reference/countries.csv: {sorted(unknown)}")
            column = ROLL_UPS[by]
            parts = [(name if name == UNCLASSIFIED else countries[name][column], ticker, value)
                     for name, ticker, value in parts]

        groups = defaultdict(lambda: defaultdict(Decimal))
        for name, ticker, value in parts:
            groups[name][ticker] += value
        rows = [(name, sum(by_ticker.values()), sorted(by_ticker.items(), key=lambda p: p[1], reverse=True))
                for name, by_ticker in groups.items()]
        # Unclassified is a gap in the data, not a group, so it goes last whatever its size.
        rows.sort(key=lambda row: (row[0] == UNCLASSIFIED, -row[1], row[0]))

    total = sum(value_eur for _, value_eur, _ in rows)
    result = []
    for grp, value, by_ticker in rows:
        row = {
            "group": grp,
            "value_eur": value,
            "weight_pct": (value / total) * 100 if total else Decimal(0),
        }
        if by_ticker is not None:
            row["by_ticker"] = by_ticker
        result.append(row)
    return result

def performance(connection, account_ids, asset_types=None, tickers=None):
    if not account_ids:
        raise ValueError("account_ids cannot be empty")
    check_known(connection, "price_ticker", tickers)
    check_known(connection, "asset_type", asset_types)

    if asset_types is None and tickers is None:
        rows = connection.execute(
            """
            SELECT t.event_date, SUM(CAST(tl.amount / CASE WHEN tl.currency = 'EUR' THEN 1 ELSE f.rate END AS DECIMAL(18, 6))) AS amount_eur
            FROM transaction_legs tl
            JOIN transactions t ON t.transaction_id = tl.transaction_id
            LEFT JOIN daily_fx f ON f.quote_currency = tl.currency AND f.date = t.event_date
            WHERE list_contains($account_ids, t.account_id)
            AND tl.role IN ('deposit', 'withdrawal', 'reward')
            GROUP BY t.event_date
            """,
            {"account_ids": account_ids},
        ).fetchall()
    else:
        rows = connection.execute(
            """
            SELECT t.event_date, -SUM(t.cash_eur) AS amount_eur
            FROM trades t
            JOIN instruments i ON i.instrument_id = t.instrument_id
            WHERE list_contains($account_ids, t.account_id)
            AND ($tickers IS NULL OR list_contains($tickers, i.price_ticker))
            AND ($asset_types IS NULL OR list_contains($asset_types, i.asset_type))
            GROUP BY t.event_date
            """,
            {
                "account_ids": account_ids,
                "tickers": tickers,
                "asset_types": asset_types,
            }
        ).fetchall()

    flows = dict(rows)

    include_cash = asset_types is None and tickers is None
    if not include_cash:
        # Holdings view: dividends are money coming back, like sell proceeds. The account
        # view needs nothing, since they already sit in the cash it counts.
        for row in dividends(connection, account_ids, by="date", asset_types=asset_types, tickers=tickers):
            flows[row["group"]] = flows.get(row["group"], Decimal(0)) - row["net_eur"]
    value_rows = portfolio_value(connection, account_ids, asset_types=asset_types, tickers=tickers, include_cash=include_cash)
    result = []
    invested = Decimal(0)
    for day, value in value_rows:
        invested += flows.get(day, Decimal(0))
        result.append({
            "date": day,
            "value_eur": value,
            "invested_eur": invested,
            "unrealised_pnl_eur": value - invested,
            "unrealised_pnl_percent": (value - invested) / invested * 100 if invested > 0 else None,
        })
    return result


def dividends(connection, account_ids, by="month", asset_types=None, tickers=None):
    """Dividends in EUR per group, as gross, withholding tax (negative) and net."""
    if not account_ids:
        raise ValueError("account_ids cannot be empty")
    check_known(connection, "price_ticker", tickers)
    check_known(connection, "asset_type", asset_types)

    GROUPS = {
        "date": "t.event_date",
        "month": "strftime(t.event_date, '%Y-%m')",
        "year": "year(t.event_date)",
        "ticker": "i.price_ticker",
    }
    if by not in GROUPS:
        raise ValueError(f"by must be one of {sorted(GROUPS)}")

    rows = connection.execute(
        f"""
        WITH legs AS (
            SELECT {GROUPS[by]} AS grp, tl.role,
                   CAST(tl.amount / CASE WHEN tl.currency = 'EUR' THEN 1 ELSE f.rate END AS DECIMAL(18, 6)) AS amount_eur
            FROM transaction_legs tl
            JOIN transactions t ON t.transaction_id = tl.transaction_id
            JOIN instruments i ON i.instrument_id = t.instrument_id
            LEFT JOIN daily_fx f ON f.quote_currency = tl.currency AND f.date = t.event_date
            WHERE t.type = 'dividend'
              AND tl.role IN ('income', 'tax')
              AND list_contains($account_ids, t.account_id)
              AND ($tickers IS NULL OR list_contains($tickers, i.price_ticker))
              AND ($asset_types IS NULL OR list_contains($asset_types, i.asset_type))
        )
        SELECT grp,
               COALESCE(SUM(amount_eur) FILTER (WHERE role = 'income'), 0) AS gross_eur,
               COALESCE(SUM(amount_eur) FILTER (WHERE role = 'tax'), 0) AS tax_eur
        FROM legs
        GROUP BY grp
        ORDER BY grp
        """,
        {
            "account_ids": account_ids,
            "tickers": tickers,
            "asset_types": asset_types,
        },
    ).fetchall()

    return [
        {"group": grp, "gross_eur": gross, "tax_eur": tax, "net_eur": gross + tax}
        for grp, gross, tax in rows
    ]


def fees(connection, account_ids, by="year"):
    """Fees paid in EUR per group, as negative amounts. Includes currency conversion fees.

    No holdings filter: conversion fees belong to no instrument.
    """
    if not account_ids:
        raise ValueError("account_ids cannot be empty")

    GROUPS = {
        "month": "strftime(t.event_date, '%Y-%m')",
        "year": "year(t.event_date)",
        "account": "t.account_id",
        "type": "t.type",
    }
    if by not in GROUPS:
        raise ValueError(f"by must be one of {sorted(GROUPS)}")

    rows = connection.execute(
        f"""
        SELECT {GROUPS[by]} AS grp,
               SUM(CAST(tl.amount / CASE WHEN tl.currency = 'EUR' THEN 1 ELSE f.rate END AS DECIMAL(18, 6))) AS fees_eur
        FROM transaction_legs tl
        JOIN transactions t ON t.transaction_id = tl.transaction_id
        LEFT JOIN daily_fx f ON f.quote_currency = tl.currency AND f.date = t.event_date
        WHERE tl.role = 'fee'
          AND list_contains($account_ids, t.account_id)
        GROUP BY grp
        ORDER BY grp
        """,
        {"account_ids": account_ids},
    ).fetchall()

    return [{"group": grp, "fees_eur": amount} for grp, amount in rows]