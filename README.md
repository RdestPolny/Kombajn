# PBN Manager Prototype

This repository contains a lightweight prototype for managing multiple
WordPress blogs from a single place.  It provides a small Streamlit
application together with a Python package for interacting with the
WordPress REST API.

## Features

- Register multiple WordPress sites and aggregate statistics.
- Visual panel built with Streamlit to show aggregated statistics.
- Schedule posts for future publication via the REST API.

## Configuration

WordPress site credentials are stored in `sites.json`. The file contains a
list of entries with an `id`, `url`, `username` and `password` for each
site. Example:

```json
[
  {"id": 1, "url": "https://example.com", "username": "admin", "password": "secret"}
]
```

The Streamlit app loads this file on startup and lists the configured sites
in the sidebar. Selecting a site reveals its recent posts.

The project is intentionally small and serves as a foundation for a more
complete private blog network management system.

## Running

```bash
pip install -r requirements.txt
streamlit run app.py  # starts a development server
```

Unit tests can be executed with:

```bash
pytest
```
