from datetime import date

import dash
import dash_mantine_components as dmc
from dash import html

from dashboard import components as ui
from dashboard import data

dash.register_page(__name__, path="/data", name="Data", order=9, group="Records")

STALE_AFTER_DAYS = 5  # a weekend plus a holiday is normal; more than that usually means a broken ticker
WEIGHTS_STALE_AFTER_DAYS = 365  # hand-written country weights are refreshed once a year
UNCLASSIFIED_WARN = 0.10


def layout(**_):
    # A function, so every visit reads the database again. No filters apply here.
    return html.Div(
        [
            ui.page_header(
                "Data",
                "What the numbers are built on: the last imports, and how old each price and exchange rate is. "
                "Run python update_all.py to refresh.",
            ),
            content(),
        ]
    )


def age_badge(last, today):
    if last is None:
        return dmc.Badge("No prices", color="red", variant="light", radius="sm")
    days = (today - last).days
    if days > STALE_AFTER_DAYS:
        return dmc.Badge(f"{days} days old", color="orange", variant="light", radius="sm")
    return dmc.Badge("Up to date", color="green", variant="light", radius="sm")


def weights_badge(source, as_of, unclassified, countries, today):
    if not countries:
        return dmc.Badge("No country weights", color="red", variant="light", radius="sm")
    if source == "reference" and as_of is None:
        return dmc.Badge("No date", color="orange", variant="light", radius="sm")
    if as_of and (today - as_of).days > WEIGHTS_STALE_AFTER_DAYS:
        return dmc.Badge(f"{(today - as_of).days // 30} months old", color="orange", variant="light", radius="sm")
    if unclassified > UNCLASSIFIED_WARN:
        return dmc.Badge(f"{ui.pct(unclassified * 100)} unclassified", color="orange", variant="light", radius="sm")
    return dmc.Badge("Up to date", color="green", variant="light", radius="sm")


@ui.guarded
def content():
    today = date.today()
    with data.connect() as connection:
        imports = data.import_runs(connection)
        prices = data.price_freshness(connection)
        rates = data.fx_freshness(connection)
        unvalued = data.unvalued_positions(connection)
        counts = data.table_counts(connection)
        names = data.accounts(connection)
        weights = data.exposure_freshness(connection)

    problems = [p for p in prices if p[4] and (p[3] is None or (today - p[3]).days > STALE_AFTER_DAYS)]
    if unvalued or problems:
        status = dmc.Alert(
            [dmc.Text(f"Missing a value today: {', '.join(f'{t} in {names.get(a, a)}' for a, t in unvalued)}.",
                      size="sm") if unvalued else None,
             dmc.Text(f"Old prices for held instruments: {', '.join(p[0] for p in problems)}.", size="sm")
             if problems else None],
            title="Some numbers may be out of date", color="orange", variant="light", radius="md",
        )
    else:
        status = dmc.Alert("Every held position has a recent price and exchange rate.", title="All values are current",
                           color="green", variant="light", radius="md")

    return dmc.Stack(
        [
            status,
            ui.panel(
                dmc.SimpleGrid([ui.stat(label, f"{counts[key]:,}") for key, label in [
                    ("transactions", "Transactions"), ("transaction_legs", "Legs"), ("instruments", "Instruments"),
                    ("prices", "Daily prices"), ("fx_rates", "Exchange rates")]],
                    cols={"base": 2, "sm": 3, "md": 5}, spacing="lg"),
            ),
            dmc.SimpleGrid(
                [
                    ui.panel(ui.table(["Broker", "Transactions", "Imported"],
                                      [[b, f"{n:,}", f"{at:%-d %b %Y, %H:%M}"] for b, n, at in imports],
                                      numeric=(1,))
                             if imports else ui.empty("No imports yet"),
                             title="Last imports"),
                    ui.panel(ui.table(["Currency", "Latest rate", "Status"],
                                      [[c, f"{d:%-d %b %Y}", age_badge(d, today)] for c, d in rates])
                             if rates else ui.empty("No exchange rates yet"),
                             title="Exchange rates", description="ECB rates to EUR, via Frankfurter."),
                ],
                cols={"base": 1, "md": 2}, spacing="lg",
            ),
            ui.panel(
                ui.table(
                    ["Instrument", "Currency", "Latest price", "Status"],
                    [[dmc.Stack([dmc.Text(name, size="sm"),
                                 dmc.Text(f"{ticker}{'' if held else ', no longer held'}", size="xs", c="dimmed")], gap=0),
                      currency, f"{last:%-d %b %Y}" if last else "–", age_badge(last, today)]
                     for ticker, name, currency, last, held in prices],
                ),
                title="Prices", description="Daily closes from Yahoo Finance. Weekends use Friday's close.",
            ),
            ui.panel(
                ui.table(
                    ["Instrument", "Source", "As of", "Unclassified", "Status"],
                    [[dmc.Stack([dmc.Text(name, size="sm"), dmc.Text(ticker, size="xs", c="dimmed")], gap=0),
                      {"reference": "Factsheet", "yfinance": "Yahoo Finance"}.get(source, source or "–"),
                      f"{as_of:%-d %b %Y}" if as_of else ("Every update" if source == "yfinance" else "–"),
                      ui.pct(unclassified * 100),
                      weights_badge(source, as_of, unclassified, countries, today)]
                     for ticker, name, _, source, as_of, unclassified, countries in weights],
                    numeric=(3,),
                ),
                title="Country weights",
                description="Fund weights come from factsheets in data/reference/exposures.csv; refresh them once a "
                            "year, with the full country list, to keep Unclassified small.",
            ),
        ],
        gap="lg",
    )
