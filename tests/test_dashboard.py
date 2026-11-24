import json
from datetime import date
from decimal import Decimal

import duckdb
import pytest

from dashboard import components as ui
from dashboard import data
from dashboard.app import app  # noqa: F401  registers the pages
from dashboard.pages import activity, allocation, data_status, holdings, income, overview, performance, risk
from init_db import apply_sql_files


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    """Lightyear: deposit 1000 EUR, convert 500 EUR to USD, buy 1 META, then a USD dividend."""
    path = tmp_path / "arca.duckdb"
    with duckdb.connect(str(path)) as connection:
        apply_sql_files(connection)
        connection.execute("INSERT INTO accounts VALUES (2, 'Lightyear', 'EUR')")
        connection.execute("INSERT INTO instruments VALUES (5, 'US30303M1027', 'Meta', 'stock', 'USD', 'META')")
        connection.execute(
            """
            INSERT INTO transactions (transaction_id, account_id, instrument_id, type, event_date)
            VALUES (1, 2, NULL, 'deposit', '2026-01-08'), (2, 2, NULL, 'conversion', '2026-01-09'),
                   (3, 2, 5, 'buy', '2026-01-09'), (4, 2, 5, 'dividend', '2026-03-10')
            """
        )
        connection.execute(
            """
            INSERT INTO transaction_legs (transaction_id, leg_number, role, amount, currency,
                                          instrument_id, unit_price, price_currency, source_file, source_id)
            VALUES (1, 1, 'deposit', 1000, 'EUR', NULL, NULL, NULL, 't.csv', 'd'),
                   (2, 1, 'sold', -500, 'EUR', NULL, NULL, NULL, 't.csv', 'c'),
                   (2, 2, 'fee', -2, 'EUR', NULL, NULL, NULL, 't.csv', 'c'),
                   (2, 3, 'bought', 585, 'USD', NULL, NULL, NULL, 't.csv', 'c'),
                   (3, 1, 'shares', 1, NULL, 5, 580, 'USD', 't.csv', 'b'),
                   (3, 2, 'cost', -580, 'USD', NULL, NULL, NULL, 't.csv', 'b'),
                   (4, 1, 'income', 2, 'USD', NULL, NULL, NULL, 't.csv', 'v'),
                   (4, 2, 'tax', -0.3, 'USD', NULL, NULL, NULL, 't.csv', 'v')
            """
        )
        connection.execute("INSERT INTO fx_rates VALUES ('2026-01-08', 'EUR', 'USD', 1.17)")
        connection.execute("INSERT INTO prices VALUES (5, '2026-01-09', 600, 600, 'USD')")
        connection.execute("INSERT INTO import_runs VALUES ('lightyear', 'f', 'c', 4, '2026-01-10 12:00:00')")
    monkeypatch.setattr(data, "DATABASE", str(path))


def rendered(component):
    return json.dumps(component, ensure_ascii=False, default=lambda c: c.to_plotly_json() if hasattr(c, "to_plotly_json") else str(c))


@pytest.mark.parametrize("filters", [(["2"], None, None), (["2"], ["stock"], None), (["2"], None, ["META"])])
def test_every_page_renders_without_an_error(filters):
    pages = [
        overview.render(*filters),
        *(holdings.render(*filters, sort) for sort in holdings.SORTS),
        *(performance.render(*filters, choice) for choice in performance.RANGES),
        *(allocation.render(*filters, by, cash) for by in allocation.GROUPS for cash in (True, False)),
        risk.render(*filters),
        risk.render_correlation(*filters, risk.compare_options(*filters)[1]),
        *(income.render_dividends(*filters, by) for by in income.DIVIDEND_GROUPS),
        *(income.render_fees(*filters, by) for by in income.FEE_GROUPS),
        activity.render(*filters, "meta", ["buy"], 1)[0],
        data_status.layout(),
    ]
    for page in pages:
        assert "couldn't be calculated" not in rendered(page)


def test_overview_bridge_adds_up_to_the_total_gain():
    # Value: META 600 / 1.17 = 512.82, EUR cash 498, USD cash 6.70 / 1.17 = 5.73 -> 1016.55, so 16.55 gained on 1000.
    # Price gain (600 - 580) / 1.17 = 17.09, dividend 1.70 / 1.17 = 1.45, conversion fee -2.00 -> 16.55.
    # Converting at the day's own rate leaves nothing unexplained, so there is no remainder row.
    page = rendered(overview.render(["2"], None, None))
    for text in ("+€17.09", "+€1.45", "−€2.00", "+€16.55"):
        assert text in page
    assert "Currency moves and other" not in page


def test_no_accounts_selected_shows_a_message_not_an_error():
    assert "No accounts selected" in rendered(overview.render([], None, None))


def test_money_formatting_keeps_the_sign_visible():
    assert ui.eur(Decimal("-1234.5")) == "−€1,234.50"
    assert ui.eur(Decimal("12"), signed=True) == "+€12.00"
    assert ui.eur(None) == "–"
    assert ui.pct(Decimal("-3.21"), signed=True) == "−3.2%"
    assert ui.quantity(Decimal("0.5000000000")) == "0.5"


def test_every_month_fills_months_without_dividends():
    rows = [{"group": "2025-11", "net_eur": Decimal(1)}, {"group": "2026-02", "net_eur": Decimal(2)}]
    assert [r["group"] for r in income.every_month(rows)] == ["2025-11", "2025-12", "2026-01", "2026-02"]


def test_activity_shows_sub_cent_amounts():
    assert activity.decimals(Decimal("0.0018")) == 4
    assert activity.decimals(Decimal("0.73")) == 2


def test_lookthrough_spreads_holdings_over_sector_weights():
    # META has a sector but no country, so the country view has nothing to spread.
    with duckdb.connect(data.DATABASE) as connection:
        connection.execute("INSERT INTO exposures (instrument_id, kind, name, weight, source) "
                           "VALUES (5, 'sector', 'technology', 1, 'yfinance')")
    sector = rendered(allocation.render(["2"], None, None, "sector", True))
    assert "Technology" in sector and "€512.82" in sector and "Leaves out" not in sector
    assert "No country weights" in rendered(allocation.render(["2"], None, None, "country", True))


def test_fees_show_which_kind_of_transaction_they_came_from():
    page = rendered(income.render_fees(["2"], None, None, "year"))
    assert "Currency conversion" in page and "€2.00" in page


def test_overview_bridge_shows_trading_fees_on_their_own_line():
    # A $1 fee on the META buy: the price gain stays (600 - 580) / 1.17 = 17.09 before fees,
    # and the fee shows as 1 / 1.17 = 0.85 instead of hiding inside the average cost.
    with duckdb.connect(data.DATABASE) as connection:
        connection.execute("INSERT INTO transaction_legs (transaction_id, leg_number, role, amount, currency, source_file, "
                           "source_id) VALUES (3, 3, 'fee', -1, 'USD', 't.csv', 'b')")
    page = rendered(overview.render(["2"], None, None))
    for text in ("+€17.09", "−€0.85", "Trading fees", "−€2.00"):
        assert text in page
    assert "Currency moves and other" not in page


def test_last_12_months_dividends_leave_out_older_ones():
    # March 2026 (net 1.70 USD / 1.17 = 1.45) is inside the window; a 3 USD dividend from 2024 is not.
    with duckdb.connect(data.DATABASE) as connection:
        connection.execute("INSERT INTO fx_rates VALUES ('2024-01-10', 'EUR', 'USD', 1.10)")
        connection.execute("INSERT INTO transactions (transaction_id, account_id, instrument_id, type, event_date) "
                           "VALUES (5, 2, 5, 'dividend', '2024-01-10')")
        connection.execute("INSERT INTO transaction_legs (transaction_id, leg_number, role, amount, currency, source_file, "
                           "source_id) VALUES (5, 1, 'income', 3, 'USD', 't.csv', 'old')")
    page = rendered(income.render_dividends(["2"], None, None, "month"))
    assert "Last 12 months" in page and "€1.45" in page  # 1.45 in the window
    assert "€4.18" in page  # all-time received: 1.45 + 3 / 1.10


def test_holdings_show_each_holdings_share_of_the_gain():
    page = rendered(holdings.render(["2"], None, None, "value"))
    assert "Share of gain" in page and "+100.0%" in page


def test_performance_shows_time_and_money_weighted_returns_for_every_range():
    for choice in performance.RANGES:
        page = rendered(performance.render(["2"], None, None, choice))
        assert "Time-weighted return" in page and "Money-weighted return" in page
        assert "Gain as a percentage" not in page


def test_performance_explains_every_value_on_hover():
    for filters in ((["2"], None, None), (["2"], None, ["META"])):
        for choice in performance.RANGES:
            page = rendered(performance.render(*filters, choice))
            for help_text in ("Deposits never count as gain", "latest close", "factsheets", "XIRR"):
                assert help_text in page
    # A range other than All also explains where it starts.
    assert "close before this range began" in rendered(performance.render(["2"], None, None, "1M"))


def test_sidebar_groups_pages_into_basics_analysis_and_records():
    from dashboard.app import navigation
    # dict.fromkeys: under pytest each page is registered twice (as pages.x and dashboard.pages.x).
    groups = {group.label: list(dict.fromkeys(link.label for link in group.children)) for group in navigation().children}
    assert list(groups) == ["Basics", "Analysis", "Records"]
    assert groups["Basics"] == ["Overview", "Holdings", "Performance", "Allocation", "Dividends & fees"]
    assert groups["Analysis"] == ["Risk"] and groups["Records"] == ["Activity", "Data"]
    assert list(allocation.HELD) == ["asset_type", "currency", "account", "ticker"]
    assert list(allocation.LOOK_THROUGH) == ["region", "country", "market", "sector", "company"]


def test_lookthrough_company_tab_splits_direct_and_via_funds():
    # META is held directly in the fixture, so it shows as its own company.
    page = rendered(allocation.render(["2"], None, None, "company", True))
    assert "Meta" in page and "Held directly" in page and "€512.82" in page


def test_lookthrough_shows_concentration_on_country_sector_and_company():
    with duckdb.connect(data.DATABASE) as connection:
        connection.execute("INSERT INTO exposures (instrument_id, kind, name, weight, source) "
                           "VALUES (5, 'sector', 'technology', 1, 'yfinance'), (5, 'country', 'United States', 1, 'yfinance')")
    for by, noun in (("country", "countries"), ("sector", "sectors"), ("company", "companies")):
        page = rendered(allocation.render(["2"], None, None, by, True))
        assert "Concentration" in page and f"effective {noun}" in page
    assert "Concentration" not in rendered(allocation.render(["2"], None, None, "region", True))


def test_risk_page_shows_its_numbers_and_charts():
    page = rendered(risk.render(["2"], None, None))
    for text in ("Volatility", "Largest fall", "Worst day", "Worst month", "Drawdown over time", "trading days"):
        assert text in page


def test_pages_never_import_each_other():
    # Dash loads every page itself as pages.<name>; importing one from another loads it twice
    # and the app refuses to start ("duplicate paths"). Shared pieces belong in components.py.
    from pathlib import Path
    for page in Path("dashboard/pages").glob("*.py"):
        assert "dashboard.pages" not in page.read_text(encoding="utf-8"), page.name


def test_sidebar_groups_fold_and_the_current_one_opens():
    import dash
    from dashboard.app import navigation, open_current_group
    assert [g.opened for g in navigation().children] == [True, False, False]  # Basics open to start
    # On /risk, Analysis unfolds; the other groups keep whatever state you left them in.
    basics, analysis, records = open_current_group("/risk")
    assert analysis is True and basics is dash.no_update and records is dash.no_update


def test_correlation_compares_the_picked_holdings_as_a_lower_triangle(tmp_path, monkeypatch):
    from tests.test_risk import weekdays
    path = tmp_path / "corr.duckdb"
    days = weekdays(date(2026, 1, 5), 80)
    with duckdb.connect(str(path)) as connection:
        apply_sql_files(connection)
        connection.execute("INSERT INTO accounts VALUES (1, 'Broker', 'EUR')")
        connection.execute("INSERT INTO instruments VALUES (1, 'IE01', 'Alpha Fund', 'etf', 'EUR', 'A'), "
                           "(2, 'IE02', 'Beta Fund', 'etf', 'EUR', 'B'), (3, 'IE03', 'Gamma Fund', 'etf', 'EUR', 'C')")
        connection.execute("INSERT INTO transactions (transaction_id, account_id, type, event_date) VALUES (1, 1, 'buy', ?)", [days[0]])
        connection.execute("INSERT INTO transaction_legs (transaction_id, leg_number, role, amount, instrument_id, source_file, "
                           "source_id) VALUES (1, 1, 'shares', 3, 1, 't', 'a'), (1, 2, 'shares', 2, 2, 't', 'b'), "
                           "(1, 3, 'shares', 1, 3, 't', 'c')")
        price = Decimal(100)
        for i, day in enumerate(days):
            other = Decimal(100) + (i % 3)  # C moves on its own
            connection.execute("INSERT INTO prices VALUES (1, ?, ?, ?, 'EUR'), (2, ?, ?, ?, 'EUR'), (3, ?, ?, ?, 'EUR')",
                               [day, price, price, day, price, price, day, other, other])
            price *= Decimal("1.01") if i % 2 else Decimal("0.99")
    monkeypatch.setattr(data, "DATABASE", str(path))

    chips, picked = risk.compare_options(["1"], None, None)
    assert picked == ["A", "B", "C"]  # largest first: 3, 2 and 1 shares
    chips = rendered(chips)
    assert chips.index('"Alpha Fund"') < chips.index('"Beta Fund"') < chips.index('"Gamma Fund"')  # names on hover
    assert '"value": "A"' in chips and '"Chip"' in chips

    page = rendered(risk.render_correlation(["1"], None, None, ["A", "B"]))
    assert page.count("heat-cell") == 1 and "1.00" in page  # one pair, A and B move identically
    assert "Pick at least two" in rendered(risk.render_correlation(["1"], None, None, ["A"]))


def test_allocation_switches_between_what_you_hold_and_inside_your_funds():
    data_, value, cash_disabled = allocation.views("look", "asset_type")
    assert [o["value"] for o in data_] == list(allocation.LOOK_THROUGH) and value == "region" and cash_disabled
    data_, value, cash_disabled = allocation.views("held", "ticker")
    assert value == "ticker" and not cash_disabled  # the current view stays when it belongs to the mode


def test_holding_view_shows_effective_holdings_without_cash():
    page = rendered(allocation.render(["2"], None, None, "ticker", True))
    assert "Concentration" in page and "effective holdings of 1" in page
