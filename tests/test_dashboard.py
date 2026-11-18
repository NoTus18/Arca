import json
from decimal import Decimal

import duckdb
import pytest

from dashboard import components as ui
from dashboard import data
from dashboard.app import app  # noqa: F401  registers the pages
from dashboard.pages import activity, allocation, data_status, holdings, income, overview, performance
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
