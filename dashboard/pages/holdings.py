from decimal import Decimal

import dash
import dash_mantine_components as dmc
from dash import Input, Output, callback, html

from analysis import portfolio
from dashboard import components as ui
from dashboard import data

dash.register_page(__name__, path="/holdings", name="Holdings", order=1, group="Basics")

SORTS = {
    "value": ("Value", lambda h: h["value_eur"]),
    "gain": ("Gain %", lambda h: h["unrealised_pnl_percent"]),
    "name": ("Name", None),
}

layout = html.Div(
    [
        ui.page_header(
            "Holdings",
            "Open positions across the selected accounts, valued at the latest close. "
            "Cost is the average price paid, fees included.",
            right=dmc.SegmentedControl(
                id="holdings-sort", value="value",
                data=[{"value": k, "label": f"Sort by {label.lower()}"} for k, (label, _) in SORTS.items()],
                persistence=True, persistence_type="local",
            ),
        ),
        html.Div(id="holdings-content"),
    ]
)


@callback(Output("holdings-content", "children"), *ui.FILTERS, Input("holdings-sort", "value"))
@ui.guarded
def render(accounts, asset_types, tickers, sort):
    account_ids, asset_types, tickers = data.selection(accounts, asset_types, tickers)
    if not account_ids:
        return ui.no_accounts()

    with data.connect() as connection:
        snapshot = portfolio.holdings_snapshot(connection, account_ids, asset_types, tickers)
        realised = portfolio.realised_gains(connection, account_ids, asset_types, tickers)
        names = data.instruments(connection)

    if sort == "name":
        snapshot.sort(key=lambda h: names[h["ticker"]][0].lower())
    else:
        snapshot.sort(key=SORTS[sort][1], reverse=True)

    total_cost = sum((h["cost"] for h in snapshot), Decimal(0))
    total_value = sum((h["value_eur"] for h in snapshot), Decimal(0))
    total_gain = total_value - total_cost
    total_realised = sum((r["realised_eur"] for r in realised), Decimal(0))

    summary = dmc.SimpleGrid(
        [
            ui.stat("Market value", ui.eur(total_value)),
            ui.stat("Cost", ui.eur(total_cost)),
            ui.stat("Unrealised gain", ui.signed_text(ui.eur(total_gain, signed=True), total_gain,
                                                      className="stat-value"),
                    ui.signed_text(ui.pct(total_gain / total_cost * 100 if total_cost else None, signed=True),
                                   total_gain, size="sm")),
            ui.stat("Realised gain", ui.signed_text(ui.eur(total_realised, signed=True), total_realised,
                                                    className="stat-value")),
        ],
        cols={"base": 2, "md": 4}, spacing="lg",
    )

    if not snapshot:
        holdings_table = ui.empty("No open positions in this selection")
    else:
        # A share of a loss reads backwards, so shares only show when the selection is up overall.
        share = lambda h: h["unrealised_pnl_eur"] / total_gain * 100 if total_gain > 0 else None
        holdings_table = ui.table(
            ["Holding", "Type", "Shares", "Avg cost", "Cost", "Value", "Gain", "Gain %", "Share of gain", "Weight"],
            [
                [
                    dmc.Stack([dmc.Text(names[h["ticker"]][0], size="sm", fw=500),
                               dmc.Text(f"{h['ticker']}, priced in {names[h['ticker']][2]}", size="xs", c="dimmed")],
                              gap=0),
                    dmc.Text(ui.asset_type_label(names[h["ticker"]][1]), size="sm", c="dimmed"),
                    ui.quantity(h["qty"]),
                    ui.eur(h["avg_cost_eur"]),
                    ui.eur(h["cost"]),
                    ui.eur(h["value_eur"]),
                    ui.signed_text(ui.eur(h["unrealised_pnl_eur"], signed=True), h["unrealised_pnl_eur"], size="sm"),
                    ui.signed_text(ui.pct(h["unrealised_pnl_percent"], signed=True), h["unrealised_pnl_eur"], size="sm"),
                    ui.signed_text(ui.pct(share(h), signed=True), h["unrealised_pnl_eur"], size="sm"),
                    ui.weight_bar(h["weight_pct"]),
                ]
                for h in snapshot
            ],
            numeric=(2, 3, 4, 5, 6, 7, 8),
            min_width=1100,
            foot=["Total", "", "", "", ui.eur(total_cost), ui.eur(total_value),
                  ui.signed_text(ui.eur(total_gain, signed=True), total_gain, size="sm"),
                  ui.signed_text(ui.pct(total_gain / total_cost * 100 if total_cost else None, signed=True),
                                 total_gain, size="sm"),
                  ui.pct(100) if total_gain > 0 else "", ""],
        )

    if realised:
        realised_table = ui.table(
            ["Holding", "Realised gain"],
            [[dmc.Stack([dmc.Text(names[r["ticker"]][0], size="sm"),
                         dmc.Text(r["ticker"], size="xs", c="dimmed")], gap=0),
              ui.signed_text(ui.eur(r["realised_eur"], signed=True), r["realised_eur"], size="sm")]
             for r in realised],
            numeric=(1,),
        )
    else:
        realised_table = ui.empty("Nothing sold yet", "Gains from sales show here, at average cost.")

    return dmc.Stack(
        [
            ui.panel(summary),
            ui.panel(holdings_table, title="Open positions",
                     description="Weight is the share of the selected holdings' value. Share of gain is each "
                                 "holding's part of the total unrealised gain; losers show a negative share."),
            ui.panel(realised_table, title="Realised from sales",
                     description="Sale proceeds minus the average cost of the shares sold, fully sold holdings included."),
        ],
        gap="lg",
    )
