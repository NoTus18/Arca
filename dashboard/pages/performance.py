from datetime import date, timedelta
from decimal import Decimal

import dash
import dash_mantine_components as dmc
from dash import Input, Output, callback, html

from analysis import portfolio
from dashboard import components as ui
from dashboard import data

dash.register_page(__name__, path="/performance", name="Performance", order=2)

RANGES = ["1M", "3M", "6M", "YTD", "1Y", "All"]

layout = html.Div(
    [
        ui.page_header(
            "Performance",
            "Value against the money put in, day by day.",
            right=dmc.SegmentedControl(id="performance-range", data=RANGES, value="All",
                                       persistence=True, persistence_type="local"),
        ),
        html.Div(id="performance-content"),
    ]
)


def range_start(choice, last):
    if choice == "YTD":
        return date(last.year, 1, 1)
    days = {"1M": 30, "3M": 91, "6M": 182, "1Y": 365}.get(choice)
    return last - timedelta(days=days) if days else None


def money_in_card(total, breakdown, holdings_view, dividends_in_gain):
    """The money put in, with what it's made of on hover."""
    label = "Net bought" if holdings_view else "Money put in"
    meaning = ("Buys minus sales and dividends of the selected holdings in this range." if holdings_view else
               "Deposits minus withdrawals in this range; rewards count as deposits.")
    lines = [dmc.Text(meaning, size="xs", c="dimmed")]
    lines += [dmc.Group([dmc.Text(name, size="sm"), dmc.Text(ui.eur(amount), size="sm", className="num")],
                        justify="space-between") for name, amount in breakdown]
    lines.append(dmc.Group([dmc.Text("Total", size="sm", fw=650), dmc.Text(ui.eur(total), size="sm", fw=650)],
                           justify="space-between", className="breakdown-total"))
    if not holdings_view and dividends_in_gain:
        lines.append(dmc.Text(f"Dividends of {ui.eur(dividends_in_gain)} were paid into the accounts, so they "
                              "count in the gain, not here.", size="xs", c="dimmed"))
    if not breakdown:
        lines.append(dmc.Text("No money in or out in this period", size="sm"))
    return ui.explained(ui.stat(label, ui.eur(total), dmc.Text("Hover for the breakdown", size="xs", c="dimmed")),
                        lines)


@callback(Output("performance-content", "children"), *ui.FILTERS, Input("performance-range", "value"))
@ui.guarded
def render(accounts, asset_types, tickers, choice):
    account_ids, asset_types, tickers = data.selection(accounts, asset_types, tickers)
    if not account_ids:
        return ui.no_accounts()
    holdings_view = data.is_holdings_view(asset_types, tickers)

    with data.connect() as connection:
        history = portfolio.performance(connection, account_ids, asset_types, tickers)
        names = data.accounts(connection)
        per_account = {
            account_id: portfolio.portfolio_value(connection, [account_id], asset_types, tickers,
                                                  include_cash=not holdings_view)
            for account_id in account_ids
        }

    if not history:
        return ui.empty("Nothing to show yet", "Run python update_all.py to import your exports.")

    start = range_start(choice, history[-1]["date"])
    index = next((i for i, r in enumerate(history) if start is None or r["date"] >= start), len(history) - 1)
    shown, last = history[index:], history[-1]
    # Changes are measured from the close before the range; "All" starts from nothing.
    base = history[index - 1] if index > 0 else {"value_eur": Decimal(0), "invested_eur": Decimal(0),
                                                 "unrealised_pnl_eur": Decimal(0)}
    invested_label = "Net invested" if holdings_view else "Deposited"

    # Start value + money put in + gain = value now, so new deposits never count as gain.
    gain_change = last["unrealised_pnl_eur"] - base["unrealised_pnl_eur"]
    money_in = last["invested_eur"] - base["invested_eur"]
    after = base["date"] if index > 0 else date.min
    with data.connect() as connection:
        breakdown = data.money_in_breakdown(connection, account_ids, asset_types, tickers, after, last["date"])
        dividends_in_gain = data.dividends_between(connection, account_ids, asset_types, tickers, after, last["date"])
    note = lambda text: dmc.Text(text, size="xs", c="dimmed")

    all_time = f"{ui.pct(last['unrealised_pnl_percent'], signed=True)} on " \
               f"{ui.eur(last['invested_eur'], decimals=0)} {invested_label.lower()}, all time"
    end = f"{last['date']:%-d %b %Y}"
    cards = [
        ui.explained(
            ui.stat("Gain" if index == 0 else f"Gain, {base['date']:%-d %b %Y} to {end}",
                    ui.signed_text(ui.eur(gain_change, signed=True), gain_change, className="stat-value"),
                    note(all_time if index == 0 else "Value change, not counting money put in")),
            "What the investments made in this range: value at the end, minus value at the start, minus the "
            "money put in. Deposits never count as gain. The percentage underneath is all-time gain over money "
            "in, which new deposits dilute; the returns below don't have that problem."),
    ]
    if index > 0:  # on "All" the start is always €0
        cards.append(ui.explained(
            ui.stat("Value at start", ui.eur(base["value_eur"]), note(f"Close on {base['date']:%-d %b %Y}")),
            "What the selection was worth at the close before this range began. Changes in this range are "
            "measured from here."))
    cards += [
        ui.explained(
            ui.stat("Value at end", ui.eur(last["value_eur"]), note(end if index == 0 else all_time)),
            "What the selection is worth at the latest close, at that day's prices and ECB exchange rates."
            + ("" if holdings_view else " Includes cash in the accounts.")),
        money_in_card(money_in, breakdown, holdings_view, dividends_in_gain),
    ]
    result = portfolio.returns(history, start)
    period = "Per year" if result["annualised"] else f"Over {result['days']} days"
    twr, mwr = result["time_weighted"], result["money_weighted"]
    per = ("It's per year because the range is a year or more." if result["annualised"] else
           "It's over the range, not per year, because the range is shorter than a year.")
    return_cards = [
        ui.explained(
            ui.stat("Time-weighted return", ui.signed_text(ui.pct(twr, signed=True), twr, className="stat-value"),
                    note(period)),
            "How the investments themselves did, as if no money had been added or taken out. This is what fund "
            f"factsheets and indexes report, so it's the one to compare with them, or one account with another. {per}"),
        ui.explained(
            ui.stat("Money-weighted return",
                    ui.signed_text(ui.pct(mwr, signed=True), mwr or 0, className="stat-value"), note(period)),
            "What your euros actually earned (XIRR): money that was invested longer counts for more. Below the "
            "time-weighted return means deposits tended to land before weaker periods; above means your timing "
            f"helped. {per}"),
    ]
    gap = mwr - twr if mwr is not None else Decimal(0)
    timing = None
    if abs(gap) >= Decimal("0.5"):  # smaller gaps are noise from daily closes
        timing = note(f"Your timing {'added' if gap > 0 else 'cost'} {abs(gap):.1f} points: money tended to go in "
                      f"before {'stronger' if gap > 0 else 'weaker'} days.")
    summary = dmc.Stack(
        [dmc.SimpleGrid(cards, cols={"base": 2, "md": len(cards)}, spacing="lg"),
         dmc.Divider(),
         dmc.SimpleGrid(return_cards, cols={"base": 1, "sm": 2}, spacing="lg"),
         timing],
        gap="md",
    )

    # Time-weighted growth chains, so the range starts at 0% from the close before it.
    growth = portfolio.time_weighted(history)
    growth_base = growth[index - 1][1] if index > 0 else Decimal(1)
    rows = [{"date": r["date"].isoformat(),
             "value": data.num(r["value_eur"]),
             "invested": data.num(r["invested_eur"]),
             "gain": data.num(r["unrealised_pnl_eur"]),
             "twr": data.num((g / growth_base - 1) * 100)}
            for r, (_, g) in zip(shown, growth[index:])]

    value_chart = ui.time_chart(
        rows,
        [{"name": "value", "label": "Value", "color": ui.SERIES[0], "type": "area"},
         {"name": "invested", "label": invested_label, "color": ui.SERIES[1], "type": "line", "strokeDasharray": "5 4"}],
        kind=dmc.CompositeChart, h=320,
    )
    gain_chart = ui.time_chart(
        rows, [{"name": "gain", "label": "Gain", "color": ui.SERIES[0]}],
        type="split", splitColors=[ui.GOOD, ui.BAD], h=220,
    )
    twr_chart = ui.time_chart(
        rows, [{"name": "twr", "label": "Time-weighted return", "color": ui.SERIES[0]}],
        kind=dmc.LineChart, h=220,
        valueFormatter={"function": "pct"},
        yAxisProps={"tickFormatter": {"function": "pct"}, "width": 56},
        referenceLines=[{"y": 0, "color": "var(--arca-ink-soft)"}],
    )

    accounts_series = [{"name": names[a], "color": ui.SERIES[(a - 1) % len(ui.SERIES)]} for a in account_ids]
    by_date = {}
    for account_id, values in per_account.items():
        for day, value in values:
            if start is None or day >= start:
                # An account adds 0 before its first transaction, so the stack stays continuous.
                row = by_date.setdefault(day, {"date": day.isoformat(), **{s["name"]: 0 for s in accounts_series}})
                row[names[account_id]] = data.num(value)
    accounts_chart = ui.time_chart([by_date[d] for d in sorted(by_date)], accounts_series, type="stacked", h=280)

    return dmc.Stack(
        [
            ui.panel(summary),
            ui.panel(dmc.Stack([ui.legend([("Value", ui.SERIES[0]), (invested_label, ui.SERIES[1])]), value_chart]),
                     title="Value and money in",
                     description="Money in counts deposits and withdrawals." if not holdings_view else
                     "Net invested is buys minus sales and dividends of the selected holdings."),
            dmc.SimpleGrid(
                [ui.panel(gain_chart, title="Gain in euros", description="Value minus money in."),
                 ui.panel(twr_chart, title="Time-weighted return",
                          description="Growth of the investments with deposits taken out; the line a benchmark "
                                      "would sit next to.")],
                cols={"base": 1, "md": 2}, spacing="lg",
            ),
            ui.panel(dmc.Stack([ui.legend([(s["name"], s["color"]) for s in accounts_series]), accounts_chart]),
                     title="Value by account", description="Stacked, so the top line is the total."),
        ],
        gap="lg",
    )
