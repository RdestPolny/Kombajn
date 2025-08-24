"""PBN Manager package.

Provides classes for interacting with multiple WordPress sites and
aggregating statistics. The package is intentionally lightweight and
relies on the WordPress REST API.
"""

__all__ = ["WordPressClient", "PBNManager"]

from .wordpress_client import WordPressClient
from .manager import PBNManager
