from decimal import Decimal

import dash
import dash_mantine_components as dmc
from dash import Input, Output, callback, html

from dashboard import components as ui
from dashboard import data

dash.register_page(__name__, path="/activity", name="Activity", order=5)

PAGE_SIZE = 25
TYPE_COLORS = {"buy": "blue", "sell": "orange", "dividend": "teal", "deposit": "gray", "withdrawal": "gray",
               "conversion": "violet", "reward": "yellow"}

layout = html.Div(
    [
        ui.page_header(
            "Activity",
            "Every transaction in the selected accounts, newest first. Cash shows the net change per currency.",
        ),
        ui.panel(
            dmc.Stack(
                [
                    dmc.Group(
                        [dmc.TextInput(id="activity-search", placeholder="Search holding or ticker", w=260,
                                       debounce=300),
                         dmc.MultiSelect(id="activity-types", placeholder="All types", w=300, clearable=True,
                                         data=[{"value": t, "label": t.title()} for t in TYPE_COLORS])],
                        gap="sm",
                    ),
                    html.Div(id="activity-content"),
                    dmc.Group([dmc.Text(id="activity-count", size="xs", c="dimmed"),
                               dmc.Pagination(id="activity-page", total=1, value=1, size="sm")],
                              justify="space-between"),
                ],
                gap="md",
            )
        ),
    ]
)


def decimals(amount):
    # IBKR converts sub-cent dividend leftovers; two decimals would show them as 0.00.
    return 2 if amount == 0 or abs(amount) >= Decimal("0.01") else 4


def cash_badges(cash):
    """Net cash per currency for one transaction, e.g. -100.00 EUR and +117.30 USD for a conversion."""
    totals = {}
    for leg in cash or []:
        totals[leg["currency"]] = totals.get(leg["currency"], 0) + leg["amount"]
    return dmc.Group(
        [ui.signed_text(f"{'+' if amount > 0 else '−' if amount < 0 else ''}{abs(amount):,.{decimals(amount)}f} {currency}",
                        amount, size="sm") for currency, amount in sorted(totals.items())],
        gap="sm", justify="flex-end", wrap="nowrap",
    )


@callback(
    Output("activity-content", "children"),
    Output("activity-page", "total"),
    Output("activity-count", "children"),
    *ui.FILTERS,
    Input("activity-search", "value"),
    Input("activity-types", "value"),
    Input("activity-page", "value"),
)
def render(accounts, asset_types, tickers, search, types, page):
    try:
        return build(accounts, asset_types, tickers, search, types, page)
    except Exception as error:  # ui.guarded handles one output; this callback has three
        return ui.error_message(error), 1, ""


def build(accounts, asset_types, tickers, search, types, page):
    account_ids, asset_types, tickers = data.selection(accounts, asset_types, tickers)
    if not account_ids:
        return ui.no_accounts(), 1, ""

    with data.connect() as connection:
        rows = data.activity(connection, account_ids, asset_types, tickers)

    if types:
        rows = [r for r in rows if r[2] in types]
    if search:
        needle = search.lower()
        rows = [r for r in rows if needle in f"{r[3] or ''} {r[4] or ''}".lower()]
    if not rows:
        return ui.empty("No transactions match", "Clear the search or the type filter."), 1, ""

    pages = (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE
    page = min(page or 1, pages)
    shown = rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]

    body = [
        [
            f"{day:%-d %b %Y}",
            broker,
            dmc.Group([dmc.Badge(kind.title(), color=TYPE_COLORS.get(kind, "gray"), variant="light", radius="sm", size="sm"),
                       reinvested and dmc.Badge("Reinvested", color="gray", variant="outline", radius="sm", size="xs")],
                      gap=4, wrap="nowrap"),
            dmc.Stack([dmc.Text(name, size="sm", truncate=True, maw=260), dmc.Text(ticker, size="xs", c="dimmed")], gap=0)
            if name else dmc.Text("–", c="dimmed", size="sm"),
            ui.signed_text(f"{'+' if shares > 0 else '−'}{ui.quantity(abs(shares))}", shares, size="sm") if shares else "",
            cash_badges(cash),
        ]
        for day, broker, kind, name, ticker, shares, cash, reinvested in shown
    ]
    count = f"{len(rows)} transactions, showing {(page - 1) * PAGE_SIZE + 1}–{(page - 1) * PAGE_SIZE + len(shown)}"
    return ui.table(["Date", "Account", "Type", "Holding", "Shares", "Cash"], body, numeric=(4, 5), min_width=920), pages, count
