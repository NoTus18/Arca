from datetime import date
from decimal import Decimal

from analysis.portfolio import returns, time_weighted, xirr


def row(day, value, invested):
    """One performance() row: value and money in so far, in EUR."""
    return {"date": day, "value_eur": Decimal(value), "invested_eur": Decimal(invested)}


def close(actual, expected, tolerance="0.01"):
    return actual is not None and abs(actual - Decimal(expected)) < Decimal(tolerance)


# Jan: deposit 1000. 30 Jun: grown to 1200. 1 Jul: deposit 1000 more. Flat to 31 Dec at 2200.
EXAMPLE = [
    row(date(2026, 1, 1), 1000, 1000),
    row(date(2026, 6, 30), 1200, 1000),
    row(date(2026, 7, 1), 2200, 2000),
    row(date(2026, 12, 31), 2200, 2000),
]


# Step 1: time_weighted

def test_time_weighted_ignores_deposits():
    growth = time_weighted(EXAMPLE)
    assert [day for day, _ in growth] == [r["date"] for r in EXAMPLE]
    # +20% in the first half, 0% after the second deposit.
    assert [g for _, g in growth] == [1, Decimal("1.2"), Decimal("1.2"), Decimal("1.2")]


def test_time_weighted_ignores_withdrawals():
    # 500 taken out at the start of day 2 leaves 500, which grows to 600: +20%, not -40%.
    growth = time_weighted([row(date(2026, 1, 1), 1000, 1000), row(date(2026, 1, 2), 600, 500)])
    assert growth[-1][1] == Decimal("1.2")


def test_time_weighted_skips_days_with_nothing_invested():
    growth = time_weighted([row(date(2026, 1, 1), 0, 0), row(date(2026, 1, 2), 100, 100), row(date(2026, 1, 3), 110, 100)])
    assert [g for _, g in growth] == [1, 1, Decimal("1.1")]


# Step 2: xirr

def test_xirr_of_the_example_is_about_13_5_percent_a_year():
    flows = [(date(2026, 1, 1), Decimal(-1000)), (date(2026, 7, 1), Decimal(-1000)), (date(2026, 12, 31), Decimal(2200))]
    assert close(xirr(flows), "0.1349", "0.0001")


def test_xirr_of_one_deposit_growing_10_percent_in_a_year_is_10_percent():
    assert close(xirr([(date(2025, 1, 1), Decimal(-1000)), (date(2026, 1, 1), Decimal(1100))]), "0.10", "0.0001")


def test_xirr_is_none_without_a_solution():
    # Only money going in, nothing coming back: no rate makes it add up to zero.
    assert xirr([(date(2026, 1, 1), Decimal(-100)), (date(2026, 2, 1), Decimal(-50))]) is None


# Step 3: returns

def test_returns_for_less_than_a_year_are_not_annualised():
    result = returns(EXAMPLE)
    assert result["days"] == 364 and result["annualised"] is False
    assert result["time_weighted"] == 20
    # 13.49% a year over 364 days is 13.45% over the period.
    assert close(result["money_weighted"], "13.45")


def test_returns_over_a_year_are_per_year():
    result = returns([row(date(2025, 1, 1), 1000, 1000), row(date(2026, 1, 1), 1100, 1000)])
    assert result["days"] == 365 and result["annualised"] is True
    assert close(result["time_weighted"], "10") and close(result["money_weighted"], "10")


def test_returns_for_a_range_start_from_the_close_before_it():
    history = [row(date(2026, 1, 1), 1000, 1000), row(date(2026, 1, 2), 1100, 1000), row(date(2026, 1, 3), 1210, 1000)]
    result = returns(history, start=date(2026, 1, 3))
    # Measured from 1100 on 2 Jan, not from the 1000 deposited on 1 Jan.
    assert result["days"] == 1
    assert close(result["time_weighted"], "10") and close(result["money_weighted"], "10")
