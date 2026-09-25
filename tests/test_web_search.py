import pytest

from actions import web_search


# ── _format_ddg: empty results must read as inconclusive, not as absence ──────

def test_format_ddg_empty_is_inconclusive():
    out = web_search._format_ddg("Dangote Refinery IPO", [])
    assert "Dangote Refinery IPO" in out
    assert "NOT evidence" in out or "not evidence" in out.lower()
    # The old bug: a bare "No results found for: X" that downstream LLM
    # synthesis treated as proof the topic doesn't exist publicly.
    assert "no publicly available information" not in out.lower()


def test_format_ddg_with_results():
    results = [{"title": "T", "snippet": "S", "url": "https://x.y"}]
    out = web_search._format_ddg("q", results)
    assert "T" in out and "S" in out and "https://x.y" in out


# ── _unified_web_results: engine failures fall through to the next engine ────

def test_unified_falls_back_to_google_news(monkeypatch):
    monkeypatch.setattr(web_search, "_ddg_search",
                        lambda q, max_results=6: (_ for _ in ()).throw(RuntimeError("429 ratelimit")))
    monkeypatch.setattr(web_search, "_google_news_rss",
                        lambda q, max_results=8: [{"title": "GN", "snippet": "s", "url": "u", "source": "G"}])
    results, status = web_search._unified_web_results("anything")
    assert status == "ok" and results and results[0]["title"] == "GN"


def test_unified_reports_failed_when_all_engines_error(monkeypatch):
    def boom(q, max_results=6):
        raise RuntimeError("network down")
    monkeypatch.setattr(web_search, "_ddg_search", boom)
    monkeypatch.setattr(web_search, "_google_news_rss", boom)
    results, status = web_search._unified_web_results("anything")
    assert results == [] and status == "failed"


def test_unified_reports_empty_when_cleanly_nothing(monkeypatch):
    monkeypatch.setattr(web_search, "_ddg_search", lambda q, max_results=6: [])
    monkeypatch.setattr(web_search, "_google_news_rss", lambda q, max_results=8: [])
    results, status = web_search._unified_web_results("xyzzy nothing here")
    assert results == [] and status == "empty"


# ── _search: backend failure must never be phrased as 'no information' ───────

def test_search_backend_failure_message(monkeypatch):
    monkeypatch.setattr(web_search, "_get_search_provider", lambda: "skip")
    def boom(q, max_results=6):
        raise RuntimeError("403")
    monkeypatch.setattr(web_search, "_ddg_search", boom)
    monkeypatch.setattr(web_search, "_google_news_rss", boom)
    out = web_search._search("Dangote Refinery IPO")
    assert "could NOT verify" in out
    assert "no publicly available information" not in out.lower()


def test_search_empty_retries_broadened_then_honest(monkeypatch):
    monkeypatch.setattr(web_search, "_get_search_provider", lambda: "skip")
    calls = []

    def fake_unified(query, max_results=6):
        calls.append(query)
        # First (raw) query: clean empty. Broadened retry: hits.
        if "Nigeria 2026" in query:
            return [{"title": "Hit", "snippet": "s", "url": "u"}], "ok"
        return [], "empty"

    monkeypatch.setattr(web_search, "_unified_web_results", fake_unified)
    out = web_search._search("dangote ipo")
    assert any("Nigeria 2026" in c for c in calls), "broadened retry should fire"
    assert "Hit" in out


def test_search_inconclusive_when_even_broadened_finds_nothing(monkeypatch):
    monkeypatch.setattr(web_search, "_get_search_provider", lambda: "skip")
    monkeypatch.setattr(web_search, "_unified_web_results", lambda q, max_results=6: ([], "empty"))
    out = web_search._search("ultra obscure topic")
    assert "inconclusive" in out.lower()


# ── synthesis prompt: guards against 'topic doesn't exist' hallucination ─────

def test_synthesis_prompt_contains_guardrails(monkeypatch):
    captured = {}

    class _R:
        text = "ok answer"

    def fake_generate(prompt):
        captured["prompt"] = prompt
        return _R()

    import types
    fake_mod = types.ModuleType("core.ai_client")
    fake_mod.generate_content = fake_generate
    monkeypatch.setitem(__import__("sys").modules, "core.ai_client", fake_mod)
    web_search._synthesize_from_results("q", "some results", kind="search")
    p = captured["prompt"].lower()
    assert "never claim" in p and "no publicly available information" in p


# ── live network test (skipped when offline / engines down) ──────────────────

def test_google_news_rss_live():
    try:
        items = web_search._google_news_rss("Dangote Refinery", max_results=3)
    except Exception as e:
        pytest.skip(f"Network unavailable or engine down: {e}")
    assert items and items[0]["title"]