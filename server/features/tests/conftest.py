"""Pytest bootstrap for this directory.

``server/dotenv.py`` (the repo's stdlib-only ``.env`` loader) shadows the
third-party ``dotenv`` (python-dotenv) package for any import that runs
after pytest prepends ``server/`` to ``sys.path`` — which happens in
``Package.setup()`` for ``server.features`` (``import_path`` resolves the
namespace package to module name ``features`` with pkg root ``server/``).

Importing the real ``dotenv`` here pins it in ``sys.modules`` while the
path is still clean, so the later ``from dotenv import dotenv_values``
inside ``mcp`` -> ``pydantic_settings`` resolves correctly regardless of
collection order. Without this, the suite only passes when the
alphabetically-first test module happens to import the heavy chain at
collection time (see test_openai_search_rewrite docstring for the same
shadowing note).
"""

import dotenv  # noqa: F401  (real python-dotenv; must stay the first import)
