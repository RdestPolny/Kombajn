# PBN Manager Prototype

This repository contains a lightweight prototype for managing multiple
WordPress blogs from a single file.  The script bundles a small Streamlit
application together with a minimal WordPress client and a manager for
aggregating statistics.

## Features

- Register multiple WordPress sites and aggregate statistics.
- Visual panel built with Streamlit to show aggregated statistics.
- Schedule posts for future publication via the REST API.
- Stores WordPress credentials locally so you don't re-enter them each run.

## Data persistence

Site credentials are saved to a local `sites.json` file and reloaded
automatically on startup so you only have to enter them once.

The project is intentionally small and serves as a foundation for a more
complete private blog network management system.

## Running

```bash
pip install -r requirements.txt
streamlit run kombajn.py  # starts a development server
```

Unit tests can be executed with:

```bash
pytest
```
