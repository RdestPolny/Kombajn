"""Single-file PBN management prototype.

This module bundles a minimal WordPress client, a manager for multiple sites
and an optional Streamlit interface.  The goal is to keep everything in a
single file to simplify distribution and experimentation.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib import request as urlrequest
from urllib.error import HTTPError
from urllib.parse import urlencode


# ---------------------------------------------------------------------------
# Minimal HTTP helper ------------------------------------------------------
class Response:
    """Lightweight HTTP response container."""

    def __init__(self, body: bytes, code: int):
        self._body = body
        self.status_code = code

    def json(self) -> Any:
        return json.loads(self._body.decode()) if self._body else None

    def raise_for_status(self) -> None:
        if 400 <= self.status_code:
            raise RuntimeError(f"HTTP {self.status_code}")

def _make_request(
    method: str,
    url: str,
    data: Optional[bytes] = None,
    auth: Optional[Tuple[str, str]] = None,
) -> Response:
    req = urlrequest.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if auth:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    try:
        with urlrequest.urlopen(req) as resp:
            body = resp.read()
            return Response(body, resp.getcode())
    except HTTPError as e:  # pragma: no cover - network failure
        return Response(e.read(), e.code)

def http_get(url: str, params: Optional[Dict[str, Any]] = None, auth=None) -> Response:
    if params:
        url = f"{url}?{urlencode(params)}"
    return _make_request("GET", url, auth=auth)

def http_post(url: str, json: Optional[Dict[str, Any]] = None, auth=None) -> Response:
    data = jsonlib.dumps(json).encode() if json is not None else None
    return _make_request("POST", url, data=data, auth=auth)

# Alias to avoid name clash with argument name
jsonlib = json


# ---------------------------------------------------------------------------
# WordPress client ---------------------------------------------------------
class WordPressError(RuntimeError):
    """Raised when the WordPress API returns an error response."""


@dataclass
class WordPressSite:
    """Configuration of a WordPress site."""

    url: str
    username: str
    password: str


class WordPressClient:
    """Simple client for the WordPress REST API."""

    def __init__(self, site: WordPressSite):
        self.site = site
        self.base = site.url.rstrip('/') + '/wp-json/wp/v2'
        self.auth = (site.username, site.password)

    def _handle_response(self, response: Response) -> Any:
        try:
            response.raise_for_status()
        except RuntimeError as exc:  # pragma: no cover - network failure
            if getattr(response, "status_code", None) == 401:
                raise WordPressError("Authentication failed (HTTP 401)") from exc
            raise WordPressError(str(exc)) from exc
        return response.json()

    def _get(self, path: str, **params: Any) -> Any:
        response = http_get(f"{self.base}/{path}", params=params, auth=self.auth)
        return self._handle_response(response)

    def _post(self, path: str, payload: Dict[str, Any]) -> Any:
        response = http_post(f"{self.base}/{path}", json=payload, auth=self.auth)
        return self._handle_response(response)

    def get_stats(self) -> Dict[str, Any]:
        """Return statistics about posts and categories."""
        posts = self._get('posts', per_page=100)
        categories = self._get('categories', per_page=100)
        return {
            'posts': len(posts),
            'categories': {c['name']: c['count'] for c in categories},
        }

    def schedule_post(
        self,
        title: str,
        content: str,
        categories: List[int],
        publish_at: dt.datetime,
    ) -> Dict[str, Any]:
        """Schedule a post for future publication."""
        payload = {
            'title': title,
            'content': content,
            'categories': categories,
            'status': 'future',
            'date': publish_at.isoformat(),
        }
        return self._post('posts', payload)


# ---------------------------------------------------------------------------
# Manager for multiple sites -----------------------------------------------
class PBNManager:
    """Aggregates statistics and operations across many sites."""

    def __init__(self, storage_path: str = "sites.json") -> None:
        self.storage_path = storage_path
        self.clients: List[WordPressClient] = []
        self._load()

    # Persistence ----------------------------------------------------------
    def _load(self) -> None:
        try:
            with open(self.storage_path) as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        for entry in data:
            self.clients.append(
                WordPressClient(
                    WordPressSite(entry["url"], entry["username"], entry["password"])
                )
            )

    def _save(self) -> None:
        data = [
            {
                "url": c.site.url,
                "username": c.site.username,
                "password": c.site.password,
            }
            for c in self.clients
        ]
        with open(self.storage_path, "w") as fh:
            json.dump(data, fh)

    # Public API -----------------------------------------------------------
    def add_site(self, url: str, username: str, password: str) -> None:
        self.clients.append(WordPressClient(WordPressSite(url, username, password)))
        self._save()

    def aggregate_stats(self) -> Dict[str, Dict[str, int]]:
        stats: Dict[str, Dict[str, int]] = {}
        for client in self.clients:
            stats[client.site.url] = client.get_stats()
        return stats


# ---------------------------------------------------------------------------
# Optional Streamlit interface ---------------------------------------------
try:  # pragma: no cover - UI components are not exercised in tests
    import streamlit as st  # type: ignore
except Exception:  # pragma: no cover - Streamlit not installed
    st = None

if st:
    if "manager" not in st.session_state:
        st.session_state.manager = PBNManager()
    manager: PBNManager = st.session_state.manager

    st.sidebar.header("Add WordPress Site")
    with st.sidebar.form("add-site"):
        url = st.text_input("Site URL", placeholder="https://example.com")
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Add")
        if submitted:
            if url and username and password:
                manager.add_site(url, username, password)
                st.sidebar.success(f"Added {url}")
            else:
                st.sidebar.error("All fields required")

    if manager.clients:
        st.sidebar.subheader("Registered sites")
        for client in manager.clients:
            st.sidebar.write(client.site.url)

    st.title("PBN Manager")
    st.header("Aggregated statistics")
    if manager.clients:
        stats = manager.aggregate_stats()
        st.json(stats)
    else:
        st.info("No sites registered yet")

    st.header("Schedule a post")
    if manager.clients:
        with st.form("schedule-post"):
            site_url = st.selectbox("Site", [c.site.url for c in manager.clients])
            title = st.text_input("Title")
            content = st.text_area("Content")
            categories = st.text_input("Category IDs (comma separated)")
            publish_date = st.date_input("Publish date", dt.date.today())
            publish_time = st.time_input("Publish time", dt.time(10, 0))
            submit = st.form_submit_button("Schedule")
            if submit:
                publish_dt = dt.datetime.combine(publish_date, publish_time)
                category_ids = [int(x.strip()) for x in categories.split(",") if x.strip()]
                for client in manager.clients:
                    if client.site.url == site_url:
                        try:
                            client.schedule_post(title, content, category_ids, publish_dt)
                            st.success("Post scheduled")
                        except Exception as e:
                            st.error(f"Failed to schedule post: {e}")
    else:
        st.info("Register a site to schedule posts")
