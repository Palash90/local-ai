"""Compatibility facade for the extracted web-search vector store."""

from server.features.websearch import vector_store as _implementation


def __getattr__(name):
    """Forward public and private legacy helpers to the implementation."""
    return getattr(_implementation, name)


def __dir__():
    return sorted(set(globals()) | set(dir(_implementation)))
