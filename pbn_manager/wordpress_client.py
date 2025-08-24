"""Client for interacting with a single WordPress installation.

The implementation uses the WordPress REST API and basic authentication.
It is intentionally small and focuses on the features required by the
exercise: retrieving basic statistics and scheduling posts.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Dict, List, Any

import requests


@dataclass
class WordPressSite:
    """Configuration of a WordPress site."""
    id: int
    url: str
    username: str
    password: str


class WordPressClient:
    """Simple client for the WordPress REST API."""

    def __init__(self, site: WordPressSite):
        self.site = site
        self.base = site.url.rstrip('/') + '/wp-json/wp/v2'
        self.auth = (site.username, site.password)

    def _get(self, path: str, **params: Any) -> Any:
        response = requests.get(f"{self.base}/{path}", params=params, auth=self.auth)
        response.raise_for_status()
        return response.json()

    def _post(self, path: str, payload: Dict[str, Any]) -> Any:
        response = requests.post(f"{self.base}/{path}", json=payload, auth=self.auth)
        response.raise_for_status()
        return response.json()

    # Public API -----------------------------------------------------------
    def get_stats(self) -> Dict[str, Any]:
        """Return statistics about posts and categories.

        The method fetches all posts and categories and summarises their
        counts. The return value is a dictionary with the total number of
        posts and a mapping of category name to the number of posts in that
        category.
        """
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
        """Schedule a post for future publication.

        Parameters
        ----------
        title: str
            Title of the post.
        content: str
            Body of the post as HTML.
        categories: list[int]
            List of category IDs to assign.
        publish_at: datetime.datetime
            When the post should be published.
        """
        payload = {
            'title': title,
            'content': content,
            'categories': categories,
            'status': 'future',
            'date': publish_at.isoformat(),
        }
        return self._post('posts', payload)
