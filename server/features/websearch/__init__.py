"""Public web-search APIs."""

from server.features.websearch.fetch import fetch_page
from server.features.websearch.search import web_search

__all__ = ["web_search", "fetch_page"]
