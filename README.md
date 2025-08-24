# PBN Manager Prototype

This repository contains a lightweight prototype for managing multiple
WordPress blogs from a single place.  It provides a small Flask
application together with a Python package for interacting with the
WordPress REST API.

## Features

- Register multiple WordPress sites and aggregate statistics.
- Expose a JSON API to fetch statistics across all registered sites.
- Schedule posts for future publication via the REST API.

The project is intentionally small and serves as a foundation for a more
complete private blog network management system.

## Running

```bash
pip install -r requirements.txt
python app.py  # starts a development server
```

Unit tests can be executed with:

```bash
pytest
```
