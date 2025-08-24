"""Management utilities for multiple WordPress sites."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

from .wordpress_client import WordPressClient, WordPressSite


class PBNManager:
    """Aggregates statistics and operations across many sites."""

    def __init__(self, storage_path: str | Path = "sites.json") -> None:
        self.storage_path = Path(storage_path)
        self.clients: List[WordPressClient] = []
        self._load_sites()

    def _load_sites(self) -> None:
        if self.storage_path.exists():
            with self.storage_path.open() as f:
                data = json.load(f)
            for site in data:
                wp_site = WordPressSite(site["id"], site["url"], site["username"], site["password"])
                self.clients.append(WordPressClient(wp_site))

    def _save_sites(self) -> None:
        data = [
            {
                "id": client.site.id,
                "url": client.site.url,
                "username": client.site.username,
                "password": client.site.password,
            }
            for client in self.clients
        ]
        with self.storage_path.open("w") as f:
            json.dump(data, f, indent=2)

    # Site management -----------------------------------------------------
    def add_site(self, url: str, username: str, password: str) -> None:
        """Register a site with the manager."""
        next_id = max((c.site.id for c in self.clients), default=0) + 1
        site = WordPressSite(next_id, url, username, password)
        self.clients.append(WordPressClient(site))
        self._save_sites()

    # Reporting -----------------------------------------------------------
    def aggregate_stats(self) -> Dict[str, Dict[str, int]]:
        """Collect statistics from all registered sites."""
        stats: Dict[str, Dict[str, int]] = {}
        for client in self.clients:
            stats[client.site.url] = client.get_stats()
        return stats
