"""Management utilities for multiple WordPress sites."""
from __future__ import annotations

from typing import Dict, List

from .wordpress_client import WordPressClient, WordPressSite


class PBNManager:
    """Aggregates statistics and operations across many sites."""

    def __init__(self) -> None:
        self.clients: List[WordPressClient] = []

    # Site management -----------------------------------------------------
    def add_site(self, url: str, username: str, password: str) -> None:
        """Register a site with the manager."""
        self.clients.append(WordPressClient(WordPressSite(url, username, password)))

    # Reporting -----------------------------------------------------------
    def aggregate_stats(self) -> Dict[str, Dict[str, int]]:
        """Collect statistics from all registered sites."""
        stats: Dict[str, Dict[str, int]] = {}
        for client in self.clients:
            stats[client.site.url] = client.get_stats()
        return stats
