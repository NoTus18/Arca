import dash
import dash_mantine_components as dmc
from dash import Input, Output, callback, html

from analysis import portfolio
from dashboard import components as ui
from dashboard import data

dash.register_page(__name__, path="/risk", name="Risk", order=6, group="Analysis")

DEFAULT_COMPARE = 5

layout = html.Div(
    [
        ui.page_header("Risk", "How bumpy the ride has been: daily swings, falls from the top and how holdings move "
                               "together. Deposits are taken out, so only market moves count."),
        html.Div(id="risk-content"),
        ui.panel(
            dmc.Stack([
                dmc.ChipGroup(id="risk-compare", multiple=True, value=[], children=[]),
                html.Div(id="risk-correlation"),
            ], gap="md"),
            title="How holdings move together", mt="lg",
            description="Correlation of daily returns in euros: 1 moves together, 0 unrelated, −1 opposite. Starts "
                        f"with your {DEFAULT_COMPARE} largest holdings; click a ticker to add or remove it. US stocks "
                        "close hours after European markets, so against European-listed funds they look less "
                        "related than they are.",
        ),
    ]
)


def when(day):
    return f"{day:%-d %b %Y}" if day else "–"


def stats(summary):
    md = summary["max_drawdown"]
    note = lambda text: dmc.Text(text, size="xs", c="dimmed")
    signed = lambda value: ui.signed_text(ui.pct(value, signed=True), value or 0, className="stat-value")
    if md["bottom"] is None:
        md_note = "Never below a previous high"
    else:
        md_note = (f"{when(md['peak'])} to {when(md['bottom'])}, "
                   + (f"back by {when(md['recovery'])}" if md["recovery"] else "not recovered yet"))
    month = summary["worst_month"]
    return [
        ui.explained(ui.stat("Volatility", ui.pct(summary["volatility"]), note("per year")),
                     "How much the value swings from day to day, scaled to a year (standard deviation of daily "
                     "returns × √252). A broad stock ETF is typically 12–18%; single stocks swing more."),
        ui.explained(ui.stat("Largest fall", signed(md["depth"]), note(md_note)),
                     "The deepest drop from a high point to the low that followed (maximum drawdown), with when it "
                     "started, bottomed and got back to that high."),
        ui.explained(ui.stat("Now", signed(summary["current_drawdown"]), note("from the highest point so far")),
                     "How far the latest value is below its highest point so far. 0% means it's at a high."),
        ui.explained(ui.stat("Worst day", signed(summary["worst_day"][1]), note(when(summary["worst_day"][0]))),
                     "The biggest one-day fall, with deposits taken out."),
        ui.explained(ui.stat("Best day", signed(summary["best_day"][1]), note(when(summary["best_day"][0]))),
                     "The biggest one-day rise, with deposits taken out."),
        ui.explained(ui.stat("Worst month", signed(month[1]), note(f"{month[0][5:]}/{month[0][:4]}")),
                     "The calendar month with the lowest return, the daily returns of that month chained together."),
    ]


def heat(value):
    """A cell coloured by correlation: blue when they move together, red when opposite, blank when unknown."""
    if value is None:
        return {"color": "var(--mantine-color-dimmed)"}
    color = ui.SERIES[0] if value >= 0 else ui.BAD
    return {"background": f"color-mix(in srgb, {color} {abs(value) * 70:.0f}%, transparent)"}


def heatmap(result, picked, names):
    """Only the picked holdings, lower half: each pair once, no diagonal of 1s."""
    index = {t: i for i, t in enumerate(result["tickers"])}
    picked = [t for t in picked if t in index]
    head = html.Tr([html.Th("")] + [html.Th(t, className="heat-head", title=names.get(t, t)) for t in picked[:-1]])
    rows = []
    for i, a in enumerate(picked[1:], start=1):
        cells = []
        for b in picked[:i]:
            v = result["matrix"][index[a]][index[b]]
            label = "–" if v is None else f"{v:.2f}"
            cells.append(html.Td(label, style=heat(v), className="heat-cell",
                                 title=f"{names.get(a, a)} and {names.get(b, b)}: {label}"))
        cells += [html.Td("") for _ in picked[i:-1]]
        rows.append(html.Tr([html.Th(a, className="heat-label", title=names.get(a, a))] + cells))
    return dmc.ScrollArea(html.Table([html.Thead(head), html.Tbody(rows)], className="heat-table"), type="auto")


def with_portfolio(result):
    ranked = sorted(((t, v) for t, v in result["to_portfolio"].items() if v is not None), key=lambda x: x[1], reverse=True)
    return dmc.Stack(
        [dmc.Group([dmc.Text(t, size="sm", w=110, truncate=True),
                    dmc.Progress(value=abs(v) * 100, color=ui.SERIES[0] if v >= 0 else ui.BAD, size="md", radius="sm",
                                 style={"flex": 1}),
                    dmc.Text(f"{v:.2f}", size="sm", className="num", w=48, ta="right")], gap="md", wrap="nowrap")
         for t, v in ranked],
        gap=6,
    )


@callback(Output("risk-content", "children"), *ui.FILTERS)
@ui.guarded
def render(accounts, asset_types, tickers):
    account_ids, asset_types, tickers = data.selection(accounts, asset_types, tickers)
    if not account_ids:
        return ui.no_accounts()

    with data.connect() as connection:
        history = portfolio.performance(connection, account_ids, asset_types, tickers)
        summary = portfolio.risk_summary(history)

    if not summary or summary["volatility"] is None:
        return ui.empty("Not enough history yet", "Risk numbers need at least a few trading days of prices.")

    period = (f"Based on {summary['observations']} trading days, {when(summary['start'])} to {when(summary['end'])}. "
              "Weekends are left out; exchange holidays aren't.")
    cards = stats(summary)
    if not summary["enough"]:
        # Under 60 trading days the numbers are mostly noise: shown greyed, with the reason.
        cards = [html.Div(c, style={"opacity": 0.45}) for c in cards]
        period = f"Not enough history yet: {summary['observations']} trading days, 60 needed. " + period

    downs = [{"date": day.isoformat(), "drawdown": data.num(d * 100)} for day, d in portfolio.drawdowns(history)
             if day >= summary["start"]]
    underwater = ui.time_chart(
        downs, [{"name": "drawdown", "label": "Below the high", "color": ui.BAD}], h=220,
        valueFormatter={"function": "pct"}, yAxisProps={"tickFormatter": {"function": "pct"}, "width": 56},
        referenceLines=[{"y": 0, "color": "var(--arca-ink-soft)"}],
    )

    return dmc.Stack(
        [
            ui.panel(dmc.Stack([dmc.SimpleGrid(cards, cols={"base": 2, "md": 3}, spacing="lg"),
                                dmc.Text(period, size="xs", c="dimmed")], gap="md")),
            ui.panel(underwater, title="Drawdown over time",
                     description="How far below its highest point so far the portfolio was each day. 0% is a new high."),
        ],
        gap="lg",
    )


@callback(Output("risk-compare", "children"), Output("risk-compare", "value"), *ui.FILTERS)
def compare_options(accounts, asset_types, tickers):
    """The held holdings of the selection to pick from, starting with the largest few."""
    account_ids, asset_types, tickers = data.selection(accounts, asset_types, tickers)
    if not account_ids:
        return [], []
    with data.connect() as connection:
        held = [r["group"] for r in portfolio.allocation(connection, account_ids, by="ticker", asset_types=asset_types,
                                                         tickers=tickers, include_cash=False) if r["value_eur"] != 0]
        names = data.instruments(connection)
    # Largest first (allocation sorts that way), so the preselected ones lead the row; full name on hover.
    chips = dmc.Group([dmc.Tooltip(dmc.Chip(t, value=t, size="xs", variant="light"), label=names[t][0], openDelay=300)
                       for t in held], gap=6)
    return chips, held[:DEFAULT_COMPARE]


@callback(Output("risk-correlation", "children"), *ui.FILTERS, Input("risk-compare", "value"))
@ui.guarded
def render_correlation(accounts, asset_types, tickers, compare):
    account_ids, asset_types, tickers = data.selection(accounts, asset_types, tickers)
    if not account_ids:
        return None
    if not compare or len(compare) < 2:
        return ui.empty("Pick at least two holdings to compare", "Correlation compares holdings with each other.")

    with data.connect() as connection:
        result = portfolio.correlations(connection, account_ids, asset_types, tickers)
        names = {t: name for t, (name, _, _) in data.instruments(connection).items()}

    shown = [t for t in compare if t in result["tickers"]]
    if not any(result["matrix"][result["tickers"].index(a)][result["tickers"].index(b)] is not None
               for i, a in enumerate(shown) for b in shown[:i]):
        return ui.empty("Not enough shared history", "Correlations need 60 trading days that both holdings have.")

    return dmc.Grid(
        [dmc.GridCol(heatmap(result, shown, names), span={"base": 12, "lg": 7}),
         dmc.GridCol(dmc.Stack([dmc.Text("Each holding with the whole portfolio", size="sm", fw=600),
                                with_portfolio(result)], gap="sm"), span={"base": 12, "lg": 5})],
        gutter="xl",
    )
