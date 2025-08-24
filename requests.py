"""A very small subset of the popular :mod:`requests` API.

The real dependency is unavailable in the execution environment, so this
module provides just enough features for the prototype.  It uses
``urllib`` under the hood and only implements what is needed by the
:mod:`pbn_manager` package.
"""
from __future__ import annotations

import base64
import json
from typing import Any, Dict, Optional, Tuple
from urllib import request as urlrequest
from urllib.error import HTTPError
from urllib.parse import urlencode


class Response:
    def __init__(self, body: bytes, code: int):
        self._body = body
        self.status_code = code

    # Compatibility helpers ----------------------------------------------
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


def get(url: str, params: Optional[Dict[str, Any]] = None, auth=None) -> Response:
    if params:
        url = f"{url}?{urlencode(params)}"
    return _make_request("GET", url, auth=auth)


def post(url: str, json: Optional[Dict[str, Any]] = None, auth=None) -> Response:
    data = jsonlib.dumps(json).encode() if json is not None else None
    return _make_request("POST", url, data=data, auth=auth)


# Alias for the json module to avoid name clash with function argument
jsonlib = json
