"""Focused web-search relevance checks.

Run with::

    PYTHONPATH=. python3 -m server.features.websearch
"""


def main():
    from server.features.websearch import relevance

    # Keep this smoke test deterministic and independent of the local embedder.
    relevance.page_cache.embed_texts = lambda texts: None
    _filter_relevant_results = relevance._filter_relevant_results

    cases = [
        (
            "real time traffic condition in Bangalore",
            [{"title": "Real Madrid CF", "url": "https://example.test/madrid", "snippet": "Real football club"}],
            [],
        ),
        (
            "signs and ways to check for GPU issues",
            [
                {"title": "GPU Failure Diagnosis", "url": "https://example.test/gpu", "snippet": "GPU issues and artifacts"},
                {"title": "Traffic Signal Signs", "url": "https://example.test/traffic", "snippet": "Road signs and symbols"},
            ],
            ["GPU Failure Diagnosis"],
        ),
        (
            "current traffic situation in Kolkata",
            [{"title": "Traffic status updates - Transport for London", "url": "https://tfl.gov.uk/traffic/status", "snippet": "London traffic status"}],
            [],
        ),
    ]
    for query, results, expected in cases:
        filtered, _ = _filter_relevant_results(results, query)
        actual = [result["title"] for result in filtered]
        assert actual == expected, f"{query!r}: expected {expected}, got {actual}"
    print(f"websearch checks passed ({len(cases)} cases)")


if __name__ == "__main__":
    main()
