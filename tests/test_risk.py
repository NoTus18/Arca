from datetime import date, timedelta
from decimal import Decimal

import duckdb
import pytest

from analysis.portfolio import correlations, daily_returns, drawdowns, risk_summary
from init_db import apply_sql_files


def row(day, value, invested=100):
    return {"date": day, "value_eur": Decimal(str(value)), "invested_eur": Decimal(str(invested))}


def weekdays(start, count):
    days, day = [], start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


MON = date(2026, 1, 5)  # a Monday
# 100 -> 120 (peak, Tue) -> 90 (bottom, Wed) -> 125 (back above the peak, Thu).
SWING = [row(MON, 100), row(MON + timedelta(1), 120), row(MON + timedelta(2), 90), row(MON + timedelta(3), 125)]


# daily_returns

def test_daily_returns_ignore_deposits():
    # +10% on day 2; on day 3 a 100 deposit lifts the value to 210, which is no gain at all.
    rows = [row(MON, 100), row(MON + timedelta(1), 110), row(MON + timedelta(2), 210, invested=200)]
    assert daily_returns(rows) == [(MON + timedelta(1), Decimal("0.1")), (MON + timedelta(2), 0)]


def test_daily_returns_skip_weekends():
    fri, sat, sun, mon = date(2026, 1, 9), date(2026, 1, 10), date(2026, 1, 11), date(2026, 1, 12)
    rows = [row(fri, 100), row(sat, 100), row(sun, 100), row(mon, 102)]
    assert daily_returns(rows) == [(mon, Decimal("0.02"))]


# drawdowns and risk_summary

def test_drawdowns_measure_the_fall_from_the_highest_point_so_far():
    assert [d for _, d in drawdowns(SWING)] == [0, 0, Decimal("-0.25"), 0]


def test_max_drawdown_has_its_depth_and_three_dates():
    summary = risk_summary(SWING)
    assert summary["max_drawdown"] == {"depth": -25, "peak": MON + timedelta(1), "bottom": MON + timedelta(2),
                                       "recovery": MON + timedelta(3)}
    assert summary["current_drawdown"] == 0


def test_a_drawdown_not_yet_recovered_has_no_recovery_date():
    summary = risk_summary(SWING[:3])
    assert summary["max_drawdown"]["recovery"] is None
    assert summary["current_drawdown"] == -25


def test_worst_and_best_day_and_worst_month():
    summary = risk_summary(SWING)
    assert summary["worst_day"] == (MON + timedelta(2), -25)  # 90 / 120
    assert summary["best_day"][0] == MON + timedelta(3) and round(summary["best_day"][1], 2) == Decimal("38.89")
    assert summary["worst_month"] == ("2026-01", 25)  # 125 / 100, the only month
    assert (summary["start"], summary["end"], summary["observations"]) == (MON, MON + timedelta(3), 3)
    assert summary["enough"] is False  # under 60 trading days


def test_constant_returns_have_no_volatility():
    rows, value = [], Decimal(100)
    for day in weekdays(MON, 80):
        rows.append(row(day, value))
        value *= Decimal("1.01")
    summary = risk_summary(rows)
    assert summary["volatility"] < Decimal("0.0001")
    assert summary["enough"] is True


def test_weekend_zeros_dont_lower_volatility():
    moves = [Decimal(m) for m in ("1.02", "0.99", "1.01", "0.97", "1.03")] * 15
    trading, calendar, value = [], [], Decimal(100)
    day = MON
    for move in moves:
        while day.weekday() >= 5:  # weekends repeat Friday's value, as daily_valuation does
            calendar.append(row(day, calendar[-1]["value_eur"]))
            day += timedelta(days=1)
        trading.append(row(day, value))
        calendar.append(row(day, value))
        value *= move
        day += timedelta(days=1)
    assert risk_summary(calendar)["volatility"] == risk_summary(trading)["volatility"] > 0


# correlations

@pytest.fixture
def connection():
    """Account 1 holds A, B (A's price doubled, so the same returns) and C (only 30 days of prices)."""
    connection = duckdb.connect(database=":memory:")
    apply_sql_files(connection)
    connection.execute("INSERT INTO accounts VALUES (1, 'Broker', 'EUR')")
    connection.execute(
        """
        INSERT INTO instruments VALUES (1, 'IE0000000001', 'A', 'etf', 'EUR', 'A'),
            (2, 'IE0000000002', 'B', 'etf', 'EUR', 'B'), (3, 'IE0000000003', 'C', 'stock', 'EUR', 'C')
        """
    )
    days = weekdays(MON, 80)
    # C is bought the day its prices start, so it always has a value.
    connection.execute("INSERT INTO transactions (transaction_id, account_id, type, event_date) "
                       "VALUES (1, 1, 'buy', ?), (2, 1, 'buy', ?)", [days[0], days[50]])
    connection.execute(
        "INSERT INTO transaction_legs (transaction_id, leg_number, role, amount, instrument_id, source_file, source_id) "
        "VALUES (1, 1, 'shares', 1, 1, 't', 'a'), (1, 2, 'shares', 1, 2, 't', 'b'), (2, 1, 'shares', 1, 3, 't', 'c')"
    )
    moves = [Decimal(m) for m in ("1.02", "0.99", "1.01", "0.97", "1.03", "1.00", "0.98")]
    price = Decimal(100)
    for i, day in enumerate(days):
        connection.execute("INSERT INTO prices VALUES (1, ?, ?, ?, 'EUR'), (2, ?, ?, ?, 'EUR')",
                           [day, price, price, day, price * 2, price * 2])
        if i >= 50:  # C only has the last 30 days
            connection.execute("INSERT INTO prices VALUES (3, ?, ?, ?, 'EUR')", [day, price, price])
        price *= moves[i % len(moves)]
    yield connection
    connection.close()


def test_holdings_with_the_same_returns_correlate_perfectly(connection):
    result = correlations(connection, [1])
    assert result["tickers"] == ["A", "B", "C"]
    a, b = result["tickers"].index("A"), result["tickers"].index("B")
    assert result["matrix"][a][b] == pytest.approx(1)
    assert result["matrix"][a][a] == 1


def test_pairs_with_under_60_shared_days_are_left_out(connection):
    result = correlations(connection, [1])
    c = result["tickers"].index("C")
    assert result["matrix"][0][c] is None and result["matrix"][c][1] is None
    assert result["to_portfolio"]["C"] is None
    assert -1 <= result["to_portfolio"]["A"] <= 1
