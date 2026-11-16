from datetime import date
from decimal import Decimal

import duckdb
import pytest

from analysis.portfolio import allocation, performance, portfolio_value
from init_db import apply_sql_files


@pytest.fixture
def connection():
    """Account 1 deposits 100 EUR on 5 Jan, account 2 deposits 50 EUR on 6 Jan."""
    connection = duckdb.connect(database=":memory:")
    apply_sql_files(connection)
    connection.execute(
        """
        INSERT INTO accounts (account_id, broker_name, base_currency)
        VALUES (1, 'Broker A', 'EUR'), (2, 'Broker B', 'EUR')
        """
    )
    add_deposit(connection, 1, 1, "2026-01-05", "100", "EUR")
    add_deposit(connection, 2, 2, "2026-01-06", "50", "EUR")
    yield connection
    connection.close()


def add_deposit(connection, transaction_id, account_id, event_date, amount, currency):
    connection.execute(
        "INSERT INTO transactions (transaction_id, account_id, type, event_date) VALUES (?, ?, 'deposit', ?)",
        [transaction_id, account_id, event_date],
    )
    connection.execute(
        """
        INSERT INTO transaction_legs (transaction_id, leg_number, role, amount, currency, source_file, source_id)
        VALUES (?, 1, 'deposit', ?, ?, 'test.csv', ?)
        """,
        [transaction_id, amount, currency, f"deposit-{transaction_id}"],
    )


def test_portfolio_value_adds_up_the_selected_accounts(connection):
    both = portfolio_value(connection, [1, 2])
    assert both[:2] == [(date(2026, 1, 5), Decimal("100")), (date(2026, 1, 6), Decimal("150"))]
    assert isinstance(both[0][1], Decimal)

    assert portfolio_value(connection, [2])[0] == (date(2026, 1, 6), Decimal("50"))


def test_portfolio_value_converts_foreign_cash_to_eur(connection):
    add_deposit(connection, 3, 1, "2026-01-07", "125", "USD")
    connection.execute("INSERT INTO fx_rates VALUES ('2026-01-07', 'EUR', 'USD', 1.25)")

    assert portfolio_value(connection, [1])[2] == (date(2026, 1, 7), Decimal("200"))


def test_portfolio_value_raises_when_an_exchange_rate_is_missing(connection):
    add_deposit(connection, 3, 1, "2026-01-07", "125", "USD")

    with pytest.raises(ValueError, match="USD"):
        portfolio_value(connection, [1])


def test_portfolio_value_rejects_unknown_tickers_and_asset_types(connection):
    connection.execute(
        """
        INSERT INTO instruments (instrument_id, isin, name, asset_type, currency, price_ticker)
        VALUES (1, 'IE0000000001', 'Test ETF', 'etf', 'EUR', 'ETF1')
        """
    )

    with pytest.raises(ValueError, match="NOPE"):
        portfolio_value(connection, [1], tickers=["ETF1", "NOPE"])
    with pytest.raises(ValueError, match="ETF"):
        portfolio_value(connection, [1], asset_types=["ETF"])  # types are lowercase


def add_etf_and_stock(connection):
    """Account 1 on 5 Jan: 100 EUR cash, 2 x ETF1 at 10 = 20, 1 x STK1 at 30 = 30."""
    connection.execute(
        """
        INSERT INTO instruments (instrument_id, isin, name, asset_type, currency, price_ticker)
        VALUES (1, 'IE0000000001', 'Test ETF', 'etf', 'EUR', 'ETF1'),
               (2, 'US0000000002', 'Test Stock', 'stock', 'EUR', 'STK1')
        """
    )
    connection.execute("INSERT INTO transactions (transaction_id, account_id, type, event_date) VALUES (10, 1, 'buy', '2026-01-05')")
    connection.execute(
        """
        INSERT INTO transaction_legs (transaction_id, leg_number, role, amount, instrument_id, source_file, source_id)
        VALUES (10, 1, 'shares', 2, 1, 'test.csv', 'buy-etf'), (10, 2, 'shares', 1, 2, 'test.csv', 'buy-stock')
        """
    )
    connection.execute("INSERT INTO prices VALUES (1, '2026-01-05', 10, 10, 'EUR'), (2, '2026-01-05', 30, 30, 'EUR')")


def test_portfolio_value_filters_by_ticker_asset_type_and_cash(connection):
    add_etf_and_stock(connection)

    def first_day(**filters):
        return portfolio_value(connection, [1], **filters)[0][1]

    assert first_day() == Decimal("150")
    assert first_day(include_cash=False) == Decimal("50")
    assert first_day(tickers=["ETF1"]) == Decimal("120")
    assert first_day(asset_types=["stock"], include_cash=False) == Decimal("30")
    # Filters combine with AND: STK1 is not an etf.
    assert first_day(tickers=["ETF1", "STK1"], asset_types=["etf"], include_cash=False) == Decimal("20")


def test_portfolio_value_raises_when_a_holding_has_no_price_yet(connection):
    # Bought on 4 Jan, but the first price is 5 Jan: 4 Jan must not silently drop the holding.
    connection.execute(
        """
        INSERT INTO instruments (instrument_id, isin, name, asset_type, currency, price_ticker)
        VALUES (1, 'IE0000000001', 'Test ETF', 'etf', 'EUR', 'ETF1')
        """
    )
    connection.execute("INSERT INTO transactions (transaction_id, account_id, type, event_date) VALUES (10, 1, 'buy', '2026-01-04')")
    connection.execute(
        """
        INSERT INTO transaction_legs (transaction_id, leg_number, role, amount, instrument_id, source_file, source_id)
        VALUES (10, 1, 'shares', 2, 1, 'test.csv', 'buy-etf')
        """
    )
    connection.execute("INSERT INTO prices VALUES (1, '2026-01-05', 10, 10, 'EUR')")

    with pytest.raises(ValueError, match="2026, 1, 4"):
        portfolio_value(connection, [1])

    # Cash alone is unaffected by the missing price.
    assert portfolio_value(connection, [1], include_cash=True, asset_types=[])[0] == (date(2026, 1, 5), Decimal("100"))


def test_allocation_groups_todays_value_and_adds_up_to_100(connection):
    add_etf_and_stock(connection)  # plus 50 EUR cash in account 2: 200 EUR in total

    def by(group, **filters):
        return [(row["group"], row["value_eur"], row["weight_pct"]) for row in allocation(connection, [1, 2], by=group, **filters)]

    assert by("asset_type") == [
        ("cash", Decimal("150"), Decimal("75")),
        ("stock", Decimal("30"), Decimal("15")),
        ("etf", Decimal("20"), Decimal("10")),
    ]
    assert by("account") == [(1, Decimal("150"), Decimal("75")), (2, Decimal("50"), Decimal("25"))]
    assert by("ticker", include_cash=False) == [("STK1", Decimal("30"), Decimal("60")), ("ETF1", Decimal("20"), Decimal("40"))]

    with pytest.raises(ValueError, match="by must be one of"):
        allocation(connection, [1], by="nonsense")


def test_performance_account_view_counts_deposits_as_invested(connection):
    rows = performance(connection, [1, 2])

    # 5 Jan: 100 deposited into account 1. 6 Jan: 50 more into account 2. Cash only, so no gain.
    assert [(row["date"], row["value_eur"], row["invested_eur"]) for row in rows[:2]] == [
        (date(2026, 1, 5), Decimal("100"), Decimal("100")),
        (date(2026, 1, 6), Decimal("150"), Decimal("150")),
    ]
    assert rows[-1]["unrealised_pnl_eur"] == 0


def test_portfolio_value_needs_accounts(connection):
    with pytest.raises(ValueError):
        portfolio_value(connection, [])
