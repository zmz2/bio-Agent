from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import AgentConfig, Config, NCBIConfig, SearchConfig


@pytest.fixture
def fake_config() -> Config:
    return Config(
        dashscope_api_key="",
        search=SearchConfig(
            allow_web_fallback=True,
            max_results_per_source=4,
            min_results_to_review=8,
            source_policy="authority_first",
        ),
        agent=AgentConfig(
            max_rounds=2,
            min_rounds=1,
            min_final_citations=3,
            synthesis_evidence_limit=6,
            max_followup_questions=1,
        ),
        ncbi=NCBIConfig(),
    )


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "")
    monkeypatch.setenv("ALLOW_WEB_FALLBACK", "true")
    monkeypatch.setenv("MAX_ROUNDS", "2")
    monkeypatch.setenv("MIN_ROUNDS", "1")
    monkeypatch.setenv("MAX_RESULTS_PER_SOURCE", "4")
    monkeypatch.setenv("MIN_RESULTS_TO_REVIEW", "8")
    monkeypatch.setenv("MIN_FINAL_CITATIONS", "3")
    monkeypatch.setenv("SEARCH_SOURCE_POLICY", "authority_first")
    monkeypatch.delenv("RUN_ONLINE_SMOKE", raising=False)
