from datetime import date
from decimal import Decimal

import duckdb

from init_db import apply_sql_files


def test_trades_sums_cash_legs_in_eur_per_trade():
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

    rows = connection.execute(
        "SELECT transaction_id, event_date, quantity, is_reinvestment, cash_eur FROM trades ORDER BY transaction_id"
    ).fetchall()

    # Buy: (-600 - 1) / 1.17. Sell: (350 - 1) / 1.20. The conversion and its fee are not a trade.
    assert rows == [
        (1, date(2026, 1, 9), Decimal("1"), False, Decimal("-513.675214")),
        (3, date(2026, 1, 10), Decimal("-0.5"), False, Decimal("290.833334")),
        (4, date(2026, 1, 9), Decimal("0.01"), True, Decimal("-5.128205")),
    ]

    connection.close()
