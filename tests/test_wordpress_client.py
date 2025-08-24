import datetime as dt
from unittest.mock import patch

import pytest

from kombajn import WordPressClient, WordPressSite, WordPressError


class DummyResponse:
    def __init__(self, data):
        self._data = data
        self.status_code = 200

    def json(self):
        return self._data

    def raise_for_status(self):
        pass


def test_get_stats():
    posts = [{"id": 1}, {"id": 2}]
    categories = [
        {"id": 1, "name": "A", "count": 1},
        {"id": 2, "name": "B", "count": 3},
    ]
    with patch("kombajn.http_get", side_effect=[DummyResponse(posts), DummyResponse(categories)]):
        client = WordPressClient(WordPressSite("http://example.com", "u", "p"))
        stats = client.get_stats()
    assert stats["posts"] == 2
    assert stats["categories"] == {"A": 1, "B": 3}


def test_schedule_post():
    with patch("kombajn.http_post", return_value=DummyResponse({"id": 99})) as mock_post:
        client = WordPressClient(WordPressSite("http://example.com", "u", "p"))
        publish_at = dt.datetime(2025, 1, 1, 10, 0, 0)
        result = client.schedule_post("Title", "Body", [1], publish_at)
    assert result == {"id": 99}
    assert mock_post.called


def test_schedule_post_auth_error():
    class UnauthorizedResponse:
        status_code = 401

        def json(self):
            return {"message": "nope"}

        def raise_for_status(self):
            raise RuntimeError("HTTP 401")

    with patch("kombajn.http_post", return_value=UnauthorizedResponse()):
        client = WordPressClient(WordPressSite("http://example.com", "u", "p"))
        publish_at = dt.datetime(2025, 1, 1, 10, 0, 0)
        with pytest.raises(WordPressError) as exc:
            client.schedule_post("Title", "Body", [1], publish_at)
        assert "Authentication failed" in str(exc.value)
