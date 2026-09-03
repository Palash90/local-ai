"""Focused web-search relevance checks (Live SearXNG Test).

Run with::

    PYTHONPATH=. python3 -m server.features.websearch
"""

import json
import os
from server.features import state


class MockEntrypoint:
    SEARXNG_URL = os.getenv("SEARXNG_URL", "http://127.0.0.1:8080")
    SEARXNG_PUBLIC_URL = os.getenv("SEARXNG_PUBLIC_URL", "http://127.0.0.1:8080")
    IMG_PATH = os.getenv("IMG_PATH", "/tmp")

    @staticmethod
    def server_url(lane):
        return ""

    @staticmethod
    def server_model_id(lane):
        return ""

    @staticmethod
    def is_model_ready(base, model_id):
        return False  # Bypasses LLM classifier in standalone mode

state._Registry.entrypoint = MockEntrypoint()

def main():
    from server.features.websearch.search import web_search

    print("--- WebSearch Live Relevance Checker ---")
    try:
        user_query = input("Enter the search query you want to check: ").strip()
    except EOFError:
        print("No query entered. Exiting.")
        return
    if not user_query:
        print("No query entered. Exiting.")
        return

    print(f"\nRunning search for: {user_query!r}...\n")
    response_json = web_search(user_query)
    data = json.loads(response_json)

    print("=== SEARCH QUERY ===")
    print(user_query)
    print("=== RESULTS ===")
    print(f"Low Confidence Flag : {data.get('low_confidence', False)}")
    if data.get("error"):
        print(f"Search Error        : {data['error']}")
        return

    results = data.get("results", [])
    print(f"Surviving Results   : {len(results)}\n")

    for i, res in enumerate(results, 1):
        print(f"[{i}] {res.get('title')}")
        print(f"    URL: {res.get('url')}")
        if "relevance" in res:
            print(f"    Semantic Score: {res['relevance']}")
        print(f"    Snippet: {res.get('snippet')[:120]}...\n")

if __name__ == "__main__":
    main()
