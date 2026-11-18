"""Update everything, then open the dashboard.
    python start.py             import changed brokers, download market data, open the dashboard
    python start.py --offline   same, but rebuild market data from the cache
    python start.py --force     re-import every broker first
"""

import os

import update_all
from dashboard.app import app

# Debug mode restarts this script in a child process to watch for file changes;
# only the first run updates, or every reload would download prices again.
if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
    try:
        update_all.main()
    except Exception as error:
        # Old data beats no dashboard, e.g. when offline or Yahoo is down.
        print(f"\nUpdate failed, opening the dashboard with the data you already have: {error!r}")

app.run(debug=True)
