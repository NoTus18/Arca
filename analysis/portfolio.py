from decimal import Decimal


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


def holdings_snapshot(connection, account_ids, asset_types=None, tickers=None):
    if not account_ids:
        raise ValueError("account_ids cannot be empty")
    check_known(connection, "price_ticker", tickers)
    check_known(connection, "asset_type", asset_types)

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
        position = positions.setdefault((account_id, ticker), {"qty": Decimal(0), "cost": Decimal(0)})
        if quantity > 0:
            position["qty"] += quantity
            position["cost"] += -(cash_eur)
        else:
            sold = -quantity
            position["cost"] -= position["cost"] * sold / position["qty"]
            position["qty"] -= sold

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

