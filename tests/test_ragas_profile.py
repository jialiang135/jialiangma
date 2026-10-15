"""CI guards the optional RAGAS dependency profile without making network calls."""

import importlib.util

import pytest


def test_optional_ragas_text_metrics_load():
    if importlib.util.find_spec("ragas") is None:
        pytest.skip("optional RAGAS profile is not installed")

    from core.eval_runner import _load_ragas

    loaded = _load_ragas()
    assert set(loaded["metrics"]) == {
        "faithfulness",
        "answer_relevancy",
        "context_precision",
        "context_recall",
    }
