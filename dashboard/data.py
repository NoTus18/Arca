"""Read-only access to the database for the dashboard.

Money stays Decimal here. Only `num` turns it into float, for chart data that has to go to JSON.
"""

from decimal import Decimal

import duckdb

from analysis.portfolio import dividends
from init_db import DATABASE_FILE

DATABASE = str(DATABASE_FILE)  # tests point this at a fixture database


def connect():
    return duckdb.connect(DATABASE, read_only=True)


def num(value):
    """Decimal to float for charts only. Never do sums on the result."""
    return None if value is None else float(value)


def selection(accounts, asset_types, tickers):
    """Filter values from the sidebar to the arguments the analysis functions take."""
    return [int(a) for a in accounts or []], asset_types or None, tickers or None


def is_holdings_view(asset_types, tickers):
    """With a holdings filter set, cash and deposits drop out of the numbers."""
    return asset_types is not None or tickers is not None


def accounts(connection):
    return dict(connection.execute("SELECT account_id, broker_name FROM accounts ORDER BY account_id").fetchall())


def instruments(connection):
    """Ticker -> (name, asset_type, currency)."""
    rows = connection.execute("SELECT price_ticker, name, asset_type, currency FROM instruments").fetchall()
    return {ticker: (name, asset_type, currency) for ticker, name, asset_type, currency in rows}


def filter_options(connection):
    return {
        "accounts": [{"value": str(k), "label": v} for k, v in accounts(connection).items()],
        "asset_types": sorted({row[1] for row in instruments(connection).values()}),
        "tickers": sorted(
            ({"value": t, "label": f"{name} ({t})"} for t, (name, _, _) in instruments(connection).items()),
            key=lambda option: option["label"],
        ),
    }


def prices_as_of(connection):
    return connection.execute("SELECT MAX(date) FROM prices").fetchone()[0]


def activity(connection, account_ids, asset_types=None, tickers=None):
    """One row per transaction with its share change and net cash per currency."""
    return connection.execute(
        """
        WITH legs AS (
            SELECT tl.transaction_id,
                   SUM(tl.amount) FILTER (WHERE tl.instrument_id IS NOT NULL) AS shares,
                   LIST({'currency': tl.currency, 'amount': tl.amount}) FILTER (WHERE tl.currency IS NOT NULL) AS cash
            FROM transaction_legs tl
            GROUP BY tl.transaction_id
        )
        SELECT t.event_date, a.broker_name, t.type, i.name, i.price_ticker, l.shares, l.cash, t.is_reinvestment
        FROM transactions t
        JOIN accounts a ON a.account_id = t.account_id
        JOIN legs l ON l.transaction_id = t.transaction_id
        LEFT JOIN instruments i ON i.instrument_id = t.instrument_id
        WHERE list_contains($account_ids, t.account_id)
          AND (($tickers IS NULL AND $asset_types IS NULL) OR (
                t.instrument_id IS NOT NULL
                AND ($tickers IS NULL OR list_contains($tickers, i.price_ticker))
                AND ($asset_types IS NULL OR list_contains($asset_types, i.asset_type))))
        ORDER BY t.event_date DESC, t.transaction_id DESC
        """,
        {"account_ids": account_ids, "asset_types": asset_types, "tickers": tickers},
    ).fetchall()


def money_in_breakdown(connection, account_ids, asset_types, tickers, after, until):
    """What performance()'s money put in is made of, for after < event_date <= until, as (label, EUR) rows.

    Same sums as performance(): deposits, withdrawals and rewards for accounts; buys, sales and
    dividends for a holdings filter.
    """
    params = {"account_ids": account_ids, "asset_types": asset_types, "tickers": tickers,
              "after": after, "until": until}
    if not is_holdings_view(asset_types, tickers):
        rows = connection.execute(
            """
            SELECT tl.role, SUM(CAST(tl.amount / CASE WHEN tl.currency = 'EUR' THEN 1 ELSE f.rate END AS DECIMAL(18, 6)))
            FROM transaction_legs tl
            JOIN transactions t ON t.transaction_id = tl.transaction_id
            LEFT JOIN daily_fx f ON f.quote_currency = tl.currency AND f.date = t.event_date
            WHERE list_contains($account_ids, t.account_id)
              AND tl.role IN ('deposit', 'withdrawal', 'reward')
              AND t.event_date > $after AND t.event_date <= $until
            GROUP BY tl.role
            """,
            {k: params[k] for k in ("account_ids", "after", "until")},
        ).fetchall()
        labels = {"deposit": "Deposits", "withdrawal": "Withdrawals", "reward": "Rewards"}
        return [(labels[role], amount) for role, amount in sorted(rows, key=lambda r: list(labels).index(r[0]))]

    rows = connection.execute(
        """
        SELECT t.quantity > 0 AS bought, -SUM(t.cash_eur)
        FROM trades t
        JOIN instruments i ON i.instrument_id = t.instrument_id
        WHERE list_contains($account_ids, t.account_id)
          AND ($tickers IS NULL OR list_contains($tickers, i.price_ticker))
          AND ($asset_types IS NULL OR list_contains($asset_types, i.asset_type))
          AND t.event_date > $after AND t.event_date <= $until
        GROUP BY bought
        ORDER BY bought DESC
        """,
        params,
    ).fetchall()
    result = [("Bought" if bought else "Sold", amount) for bought, amount in rows]
    received = dividends_between(connection, account_ids, asset_types, tickers, after, until)
    if received:
        result.append(("Dividends received", -received))
    return result


def dividends_between(connection, account_ids, asset_types, tickers, after, until):
    """Net dividends in EUR for after < date <= until."""
    return sum((r["net_eur"] for r in dividends(connection, account_ids, by="date", asset_types=asset_types,
                                                 tickers=tickers) if after < r["group"] <= until), Decimal(0))


def import_runs(connection):
    return connection.execute(
        "SELECT broker, transaction_count, imported_at FROM import_runs ORDER BY broker"
    ).fetchall()


def price_freshness(connection):
    """Latest price per instrument, and whether it is still held."""
    return connection.execute(
        """
        SELECT i.price_ticker, i.name, i.currency, MAX(p.date) AS last_date,
               COALESCE((SELECT SUM(h.quantity) FROM holdings h WHERE h.instrument_id = i.instrument_id), 0) > 0 AS held
        FROM instruments i
        LEFT JOIN prices p ON p.instrument_id = i.instrument_id
        GROUP BY i.instrument_id, i.price_ticker, i.name, i.currency
        ORDER BY held DESC, last_date NULLS FIRST, i.name
        """
    ).fetchall()


def fx_freshness(connection):
    return connection.execute(
        "SELECT quote_currency, MAX(date) FROM fx_rates GROUP BY quote_currency ORDER BY quote_currency"
    ).fetchall()


def unvalued_positions(connection):
    """Positions or cash on the latest day with no EUR value (missing price or rate)."""
    return connection.execute(
        """
        SELECT v.account_id, COALESCE(i.price_ticker, v.currency)
        FROM daily_valuation v
        LEFT JOIN instruments i ON i.instrument_id = v.instrument_id
        WHERE v.date = (SELECT MAX(date) FROM daily_valuation)
          AND v.value_eur IS NULL
          AND COALESCE(v.quantity, v.balance) <> 0
        """
    ).fetchall()


def table_counts(connection):
    return {
        name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        for name in ("transactions", "transaction_legs", "instruments", "prices", "fx_rates")
    }
