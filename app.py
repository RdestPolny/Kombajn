"""Example web application exposing a tiny management panel.

The application provides two endpoints:
- ``/stats`` returns a JSON summary of all registered sites.
- ``/schedule`` schedules a new post on a chosen site.

It is deliberately small and intended as a starting point for a more
feature complete panel.
"""
from __future__ import annotations

import datetime as dt
from flask import Flask, jsonify, request

from pbn_manager import PBNManager

app = Flask(__name__)
manager = PBNManager()


@app.route("/stats")
def stats() -> "flask.Response":
    """Return aggregated statistics for all sites."""
    return jsonify(manager.aggregate_stats())


@app.post("/schedule")
def schedule_post():
    """Schedule a post on a specified site.

    Expected JSON payload::

        {
            "site": "https://example.com",
            "title": "My post",
            "content": "<p>Body</p>",
            "categories": [1, 2],
            "publish_at": "2025-01-01T10:00:00"
        }
    """
    payload = request.get_json(force=True)
    site_url = payload["site"]
    for client in manager.clients:
        if client.site.url == site_url:
            publish_at = dt.datetime.fromisoformat(payload["publish_at"])
            result = client.schedule_post(
                payload["title"],
                payload["content"],
                payload.get("categories", []),
                publish_at,
            )
            return jsonify(result), 201
    return jsonify({"error": "site not registered"}), 404


if __name__ == "__main__":
    # Example: run a development server. In real usage the manager should
    # register sites before starting the server.
    app.run(debug=True)
