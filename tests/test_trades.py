from datetime import date
from decimal import Decimal

import duckdb
import pytest

from analysis.portfolio import holdings_snapshot, performance
from init_db import apply_sql_files


@pytest.fixture
def connection():
    """Lightyear META: buy 1 with a fee, sell 0.5 with a fee, reinvest 0.01; plus a currency conversion."""
    connection = duckdb.connect(database=":memory:")
    apply_sql_files(connection)

    connection.execute("INSERT INTO accounts VALUES (2, 'Lightyear', 'EUR')")
    connection.execute("INSERT INTO instruments VALUES (5, 'US30303M1027', 'Meta', 'stock', 'USD', 'META')")
    connection.execute(
        """
        INSERT INTO transactions (transaction_id, account_id, instrument_id, type, event_date, is_reinvestment)
        VALUES (1, 2, 5, 'buy', '2026-01-09', false),
               (2, 2, NULL, 'conversion', '2026-01-09', false),
               (3, 2, 5, 'sell', '2026-01-10', false),
               (4, 2, 5, 'buy', '2026-01-09', true)
        """
    )
    connection.execute(
        """
        INSERT INTO transaction_legs (
            transaction_id, leg_number, role, amount, currency,
            instrument_id, unit_price, price_currency, source_file, source_id
        )
        VALUES
            (1, 1, 'shares', 1, NULL, 5, 600, 'USD', 'test.csv', 'buy'),
            (1, 2, 'cost', -600, 'USD', NULL, NULL, NULL, 'test.csv', 'buy'),
            (1, 3, 'fee', -1, 'USD', NULL, NULL, NULL, 'test.csv', 'buy'),
            (2, 1, 'sold', -500, 'EUR', NULL, NULL, NULL, 'test.csv', 'conversion'),
            (2, 2, 'fee', -2, 'EUR', NULL, NULL, NULL, 'test.csv', 'conversion'),
            (2, 3, 'bought', 585, 'USD', NULL, NULL, NULL, 'test.csv', 'conversion'),
            (3, 1, 'shares', -0.5, NULL, 5, 700, 'USD', 'test.csv', 'sell'),
            (3, 2, 'proceeds', 350, 'USD', NULL, NULL, NULL, 'test.csv', 'sell'),
            (3, 3, 'fee', -1, 'USD', NULL, NULL, NULL, 'test.csv', 'sell'),
            (4, 1, 'shares', 0.01, NULL, 5, 600, 'USD', 'test.csv', 'reinvest'),
            (4, 2, 'cost', -6, 'USD', NULL, NULL, NULL, 'test.csv', 'reinvest')
        """
    )
    connection.execute(
        "INSERT INTO fx_rates VALUES ('2026-01-09', 'EUR', 'USD', 1.17), ('2026-01-10', 'EUR', 'USD', 1.20)"
    )
    yield connection
    connection.close()


def test_trades_sums_cash_legs_in_eur_per_trade(connection):
    rows = connection.execute(
        "SELECT transaction_id, event_date, quantity, is_reinvestment, cash_eur FROM trades ORDER BY transaction_id"
    ).fetchall()

    # Buy: (-600 - 1) / 1.17. Sell: (350 - 1) / 1.20. The conversion and its fee are not a trade.
    assert rows == [
        (1, date(2026, 1, 9), Decimal("1"), False, Decimal("-513.675214")),
        (3, date(2026, 1, 10), Decimal("-0.5"), False, Decimal("290.833334")),
        (4, date(2026, 1, 9), Decimal("0.01"), True, Decimal("-5.128205")),
    ]


def test_holdings_snapshot_uses_average_cost_on_sells(connection):
    # In date order: buy 1 (513.675214) and reinvest 0.01 (5.128205) on 9 Jan -> 1.01 shares
    # costing 518.803419. Selling 0.5 on 10 Jan keeps 0.51 / 1.01 of that cost.
    # Latest close 500 USD at the latest rate 1.20: 0.51 * 500 / 1.20 = 212.50 EUR.
    connection.execute("INSERT INTO prices VALUES (5, '2026-01-10', 500, 500, 'USD')")

    [meta] = holdings_snapshot(connection, [2])

    assert meta["ticker"] == "META"
    assert meta["qty"] == Decimal("0.51")
    assert round(meta["cost"], 6) == Decimal("261.970043")
    assert round(meta["avg_cost_eur"], 2) == Decimal("513.67")
    assert meta["value_eur"] == Decimal("212.5")
    assert round(meta["unrealised_pnl_eur"], 6) == Decimal("-49.470043")
    assert round(meta["unrealised_pnl_percent"], 2) == Decimal("-18.88")
    assert meta["weight_pct"] == 100


def test_performance_holdings_view_counts_buys_minus_sells_as_invested(connection):
    connection.execute("INSERT INTO prices VALUES (5, '2026-01-09', 600, 600, 'USD'), (5, '2026-01-10', 500, 500, 'USD')")

    rows = performance(connection, [2], tickers=["META"])

    def day(d):
        row = next(row for row in rows if row["date"] == d)
        return row["value_eur"], row["invested_eur"], row["unrealised_pnl_eur"]

    # 9 Jan: 1.01 shares at 600 USD / 1.17; invested = buy 513.675214 + reinvest 5.128205.
    assert day(date(2026, 1, 9)) == (Decimal("517.948718"), Decimal("518.803419"), Decimal("-0.854701"))
    # 10 Jan: sold 0.5 for 290.833334, so invested drops by that; 0.51 shares at 500 USD / 1.20.
    assert day(date(2026, 1, 10)) == (Decimal("212.5"), Decimal("227.970085"), Decimal("-15.470085"))
