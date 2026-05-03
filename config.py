"""
Configuration loading for the Bio Deep Research agent.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


@dataclass
class NCBIConfig:
    """NCBI programmatic access settings."""

    email: str = "bio-agent@example.com"
    tool: str = "bio-agent"
    api_key: Optional[str] = None
    request_timeout: int = 30
    max_abstracts: int = 3


@dataclass
class SearchConfig:
    """Search backend settings."""

    tavily_api_key: Optional[str] = None
    source_policy: str = "authority_first"
    allow_web_fallback: bool = True
    max_results_per_source: int = 25
    min_results_to_review: int = 100
    max_retries: int = 3
    retry_delay: float = 1.0
    user_agent: str = "bio-agent/2.0"


@dataclass
class AgentConfig:
    """Agent orchestration settings."""

    model: str = "qwen-max"
    max_rounds: int = 8
    min_rounds: int = 2
    temperature: float = 0.2
    top_p: float = 0.8
    request_timeout: int = 60
    model_retries: int = 2
    min_official_evidence: int = 2
    max_followup_questions: int = 6
    # 注意：min_final_citations 现在是软指标，仅用于评估证据充分性，不再阻止输出
    # 系统会在任何情况下都输出最佳结论，即使引用数量不足
    min_final_citations: int = 12
    synthesis_evidence_limit: int = 20


@dataclass
class Config:
    """Global application config."""

    dashscope_api_key: str = ""
    search: SearchConfig = field(default_factory=SearchConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    ncbi: NCBIConfig = field(default_factory=NCBIConfig)

    @classmethod
    def load(cls) -> "Config":
        min_final_citations = _get_int("MIN_FINAL_CITATIONS", 10)
        synthesis_evidence_limit = max(_get_int("SYNTHESIS_EVIDENCE_LIMIT", 16), min_final_citations)
        return cls(
            dashscope_api_key=os.getenv("DASHSCOPE_API_KEY", ""),
            search=SearchConfig(
                tavily_api_key=os.getenv("TAVILY_API_KEY"),
                source_policy=os.getenv("SEARCH_SOURCE_POLICY", "authority_first"),
                allow_web_fallback=_get_bool("ALLOW_WEB_FALLBACK", True),
                max_results_per_source=_get_int("MAX_RESULTS_PER_SOURCE", 25),
                min_results_to_review=_get_int("MIN_RESULTS_TO_REVIEW", 100),
                max_retries=_get_int("SEARCH_MAX_RETRIES", 3),
                retry_delay=_get_float("SEARCH_RETRY_DELAY", 1.0),
                user_agent=os.getenv("BIO_AGENT_USER_AGENT", "bio-agent/2.0"),
            ),
            agent=AgentConfig(
                model=os.getenv("DASHSCOPE_MODEL", "qwen-max"),
                max_rounds=_get_int("MAX_ROUNDS", 6),
                min_rounds=_get_int("MIN_ROUNDS", 1),
                temperature=_get_float("MODEL_TEMPERATURE", 0.2),
                top_p=_get_float("MODEL_TOP_P", 0.8),
                request_timeout=_get_int("MODEL_REQUEST_TIMEOUT", 60),
                model_retries=_get_int("MODEL_RETRIES", 2),
                min_official_evidence=_get_int("MIN_OFFICIAL_EVIDENCE", 1),
                max_followup_questions=_get_int("MAX_FOLLOWUP_QUESTIONS", 4),
                min_final_citations=min_final_citations,
                synthesis_evidence_limit=synthesis_evidence_limit,
            ),
            ncbi=NCBIConfig(
                email=os.getenv("NCBI_EMAIL", "bio-agent@example.com"),
                tool=os.getenv("NCBI_TOOL", "bio-agent"),
                api_key=os.getenv("NCBI_API_KEY"),
                request_timeout=_get_int("NCBI_REQUEST_TIMEOUT", 30),
                max_abstracts=_get_int("NCBI_MAX_ABSTRACTS", 3),
            ),
        )


def get_config() -> Config:
    """Return a cached config instance."""

    if not hasattr(get_config, "_config"):
        get_config._config = Config.load()
    return get_config._config
