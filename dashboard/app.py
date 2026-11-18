"""Arca dashboard. Run from the project root: python -m dashboard.app"""

from pathlib import Path

import dash
import dash_mantine_components as dmc
from dash import Dash, Input, Output, State, callback, clientside_callback, dcc, page_container

from dashboard import data

FONT = "'Schibsted Grotesk', system-ui, -apple-system, 'Segoe UI', sans-serif"

THEME = {
    "fontFamily": FONT,
    "headings": {"fontFamily": FONT, "fontWeight": "650"},
    "primaryColor": "brass",
    "primaryShade": {"light": 7, "dark": 4},
    "defaultRadius": "md",
    "colors": {
        # Strongbox brass: the one accent, used for identity and the active page.
        "brass": ["#fbf6ea", "#f3e8cc", "#e7d3a0", "#dbbd72", "#d0aa4c",
                  "#c99d35", "#b88a28", "#9c7420", "#7f5e1b", "#5f4613"],
        # Cool slate instead of Mantine's neutral greys, for the dark scheme.
        "dark": ["#d5dae2", "#aeb5c0", "#8a93a1", "#5e6877", "#3a4352",
                 "#2b3340", "#212833", "#1a202a", "#141921", "#0e1218"],
    },
    "components": {
        "Paper": {"defaultProps": {"shadow": "none"}},
        "Badge": {"defaultProps": {"tt": "none", "fw": 500}},  # sentence case, not Mantine's all caps
        "MultiSelect": {"defaultProps": {"size": "sm", "radius": "md"}},
    },
}

dmc.pre_render_color_scheme()  # no light flash before a dark page loads

HERE = Path(__file__).parent

app = Dash(
    __name__,
    use_pages=True,
    pages_folder=str(HERE / "pages"),
    assets_folder=str(HERE / "assets"),
    suppress_callback_exceptions=True,
    title="Arca",
    external_stylesheets=[
        "https://fonts.googleapis.com/css2?family=Schibsted+Grotesk:wght@400..800&display=swap"
    ],
)


def navigation():
    pages = sorted(dash.page_registry.values(), key=lambda page: page["order"])
    return dmc.Stack(
        [dmc.NavLink(label=page["name"], href=page["relative_path"], active="exact", className="nav-link")
         for page in pages],
        gap=2,
    )


def filters(options):
    return dmc.Stack(
        [
            dmc.Text("Selection", size="xs", fw=600, c="dimmed"),
            dmc.MultiSelect(
                id="f-accounts", label="Accounts", data=options["accounts"],
                value=[o["value"] for o in options["accounts"]],
                persistence=True, persistence_type="local", clearable=True,
            ),
            dmc.MultiSelect(
                id="f-asset-types", label="Asset types", placeholder="All asset types",
                data=[{"value": t, "label": t.upper() if len(t) <= 4 else t.title()} for t in options["asset_types"]],
                persistence=True, persistence_type="local", clearable=True,
            ),
            dmc.MultiSelect(
                id="f-tickers", label="Holdings", placeholder="All holdings", data=options["tickers"],
                searchable=True, persistence=True, persistence_type="local", clearable=True,
            ),
            dmc.Text("A holdings filter leaves out cash and deposits.", size="xs", c="dimmed"),
        ],
        gap="sm",
    )


def layout():
    # A function, so each page load sees new imports and prices.
    with data.connect() as connection:
        options = data.filter_options(connection)
        as_of = data.prices_as_of(connection)

    header = dmc.AppShellHeader(
        dmc.Group(
            [
                dmc.Group(
                    [dmc.Burger(id="burger", size="sm", hiddenFrom="sm", opened=False),
                     dcc.Link(dmc.Group([dmc.Box(className="brand-mark"), dmc.Text("Arca", className="brand")],
                                        gap=10), href="/", className="brand-link")],
                    gap="sm",
                ),
                dmc.Text(f"Prices to {as_of:%-d %b %Y}" if as_of else "No prices yet", size="xs", c="dimmed",
                         visibleFrom="xs"),
            ],
            justify="space-between", h="100%", px="lg",
        )
    )

    navbar = dmc.AppShellNavbar(
        [
            dmc.AppShellSection(navigation()),
            dmc.AppShellSection(dmc.ScrollArea(filters(options), type="never"), grow=True, mt="xl"),
            dmc.AppShellSection(
                dmc.SegmentedControl(
                    id="color-scheme", data=[{"value": "auto", "label": "Auto"},
                                             {"value": "light", "label": "Light"},
                                             {"value": "dark", "label": "Dark"}],
                    value="auto", fullWidth=True, size="xs", persistence=True, persistence_type="local",
                ),
                mt="md",
            ),
        ],
        p="md",
    )

    return dmc.MantineProvider(
        dmc.AppShell(
            [header, navbar, dmc.AppShellMain(dmc.Container(page_container, size=1240, px={"base": "md", "sm": "xl"},
                                                            py="xl"), className="canvas")],
            header={"height": 56},
            navbar={"width": 272, "breakpoint": "sm", "collapsed": {"mobile": True}},
            id="shell",
            padding=0,
        ),
        theme=THEME,
        defaultColorScheme="auto",
    )


app.layout = layout


@callback(
    Output("shell", "navbar"),
    Input("burger", "opened"),
    State("shell", "navbar"),
)
def toggle_navbar(opened, navbar):
    navbar["collapsed"] = {"mobile": not opened}
    return navbar


@callback(Output("burger", "opened"), Input("_pages_location", "pathname"), prevent_initial_call=True)
def close_navbar_on_navigate(_):
    return False


clientside_callback(
    """
    function (scheme) {
        const dark = window.matchMedia('(prefers-color-scheme: dark)').matches;
        const resolved = scheme === 'auto' ? (dark ? 'dark' : 'light') : scheme;
        document.documentElement.setAttribute('data-mantine-color-scheme', resolved);
        try { localStorage.setItem('mantine-color-scheme-value', scheme); } catch (e) {}
        return window.dash_clientside.no_update;
    }
    """,
    Output("color-scheme", "id"),
    Input("color-scheme", "value"),
)


if __name__ == "__main__":
    app.run(debug=True)
