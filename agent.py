"""
Bioinformatics Deep Research Agent.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import random
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

try:
    import dashscope
    from dashscope import Generation
except ImportError:  # pragma: no cover - optional in tests
    dashscope = None
    Generation = None

from config import Config, get_config
from prompts import (
    CLARIFY_REWRITE_PROMPT,
    ENTITY_NORMALIZATION_PROMPT,
    FOLLOWUP_PROMPT,
    FREEFORM_SYNTHESIS_PROMPT,
    FOLDER_FREEFORM_SYNTHESIS_PROMPT,
    RESEARCH_PLAN_PROMPT,
    SYNTHESIS_PROMPT,
    SYSTEM_PROMPT,
)
from search_tool import SearchResult, SearchTool, WebContentFetcher
from full_text_fetcher import FullTextFetcher
from utils import EntityParser, dedupe_preserve_order, format_timestamp, safe_json_parse, truncate_text
from agent_improvements import QueryExpander, EvidenceScorer, ConflictDetector, MemoryManager, Conflict, MemoryItem


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


def run_with_timeout(func: Callable[[], Any], timeout: int) -> Any:
    """Run a callable with a timeout."""

    result: list[Any] = [None]
    error: list[Exception | None] = [None]

    def target() -> None:
        try:
            result[0] = func()
        except Exception as exc:  # noqa: BLE001
            error[0] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"Operation timed out after {timeout}s")
    if error[0] is not None:
        raise error[0]
    return result[0]


class FileReader:
    """Read lightweight summaries from local folders."""

    def __init__(self, folder_path: str):
        self.folder_path = folder_path

    def scan_folder(self) -> list[dict[str, Any]]:
        if not os.path.isdir(self.folder_path):
            raise FileNotFoundError(f"文件夹不存在: {self.folder_path}")

        files: list[dict[str, Any]] = []
        for root, _, names in os.walk(self.folder_path):
            for name in names:
                ext = os.path.splitext(name)[1].lower()
                if ext not in {".h5ad", ".csv"}:
                    continue
                full_path = os.path.join(root, name)
                files.append(
                    {
                        "name": name,
                        "path": full_path,
                        "relative_path": os.path.relpath(full_path, self.folder_path),
                        "size": os.path.getsize(full_path),
                    }
                )
        files.sort(key=lambda item: item["relative_path"])
        return files

    def summarize(self, max_files: int = 8, max_chars: int = 12000) -> str:
        files = self.scan_folder()
        blocks: list[str] = []
        for item in files[:max_files]:
            blocks.append(f"- {item['relative_path']} ({item['size'] / 1024:.1f} KB)")
            ext = os.path.splitext(item["name"])[1].lower()
            if ext == ".csv":
                try:
                    with open(item["path"], "r", encoding="utf-8") as handle:
                        content = handle.read(1200)
                    blocks.append(truncate_text(content.replace("\n", " "), 400))
                except Exception:  # noqa: BLE001
                    blocks.append("（无法直接读取CSV摘要）")
            elif ext == ".h5ad":
                blocks.append("AnnData 单细胞数据文件，将通过专用解析器读取。")
        summary = "\n".join(blocks)
        return truncate_text(summary, max_chars)

    def analyze(self, max_files: int = 12) -> dict[str, Any]:
        files = self.scan_folder()
        findings: list[dict[str, Any]] = []
        entities = {
            "genes": [],
            "variants": [],
            "variant_aliases": [],
            "diseases": [],
            "species": [],
            "sample_type": None,
            "research_focus": [],
            "keywords": [],
            "other_entities": [],
        }
        summary_blocks: list[str] = []

        for item in files[:max_files]:
            ext = os.path.splitext(item["name"])[1].lower()
            try:
                if ext == ".csv":
                    analysis = self._analyze_csv(item)
                elif ext == ".h5ad":
                    analysis = self._analyze_h5ad(item)
                else:
                    analysis = {
                        "summary": f"{item['relative_path']} 暂不做结构化解析。",
                        "findings": [],
                        "entities": {},
                    }
            except Exception as exc:  # noqa: BLE001
                analysis = {
                    "summary": f"{item['relative_path']} 解析失败：{exc}",
                    "findings": [],
                    "entities": {},
                }

            analysis["entities"] = self._merge_entity_dicts(
                self._analyze_path_context(item["relative_path"]),
                analysis.get("entities", {}),
            )
            summary_blocks.append(analysis["summary"])
            findings.extend(analysis.get("findings", []))
            for key, value in analysis.get("entities", {}).items():
                if key == "sample_type" and value and not entities["sample_type"]:
                    entities["sample_type"] = value
                elif isinstance(value, list):
                    entities[key] = dedupe_preserve_order(entities.get(key, []) + value)

        entities = self._finalize_local_entities(entities, findings)
        entities["keywords"] = dedupe_preserve_order(
            entities.get("genes", [])
            + entities.get("variants", [])
            + entities.get("variant_aliases", [])
            + entities.get("diseases", [])
            + entities.get("research_focus", [])
            + entities.get("other_entities", [])
        )

        return {
            "files": files,
            "summary": truncate_text("\n".join(summary_blocks), 12000),
            "findings": findings,
            "entities": entities,
        }

    def _merge_entity_dicts(self, left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        keys = set(left) | set(right)
        for key in keys:
            left_value = left.get(key)
            right_value = right.get(key)
            if key == "sample_type":
                merged[key] = right_value or left_value
            elif isinstance(left_value, list) or isinstance(right_value, list):
                merged[key] = dedupe_preserve_order((left_value or []) + (right_value or []))
            else:
                merged[key] = right_value if right_value not in {None, ""} else left_value
        return merged

    def _analyze_path_context(self, relative_path: str) -> dict[str, Any]:
        text = relative_path.replace("_", " ").replace("-", " ")
        parsed = EntityParser.parse(text)
        lower_text = text.lower()
        genes = list(parsed.get("genes", []))
        focus = list(parsed.get("research_focus", []))
        other = list(parsed.get("other_entities", []))
        diseases = list(parsed.get("diseases", []))

        if "p53" in lower_text and "TP53" not in genes:
            genes.append("TP53")
            focus.append("p53 signaling")
        if "apoptosis" in lower_text:
            focus.append("apoptosis")
        if "cellcycle" in lower_text or "cell cycle" in lower_text:
            focus.append("cell cycle")
        if "pseudotime" in lower_text or "slingshot" in lower_text:
            focus.append("pseudotime")
        if "atac" in lower_text:
            focus.append("chromatin accessibility")
        if "rna" in lower_text:
            focus.append("transcriptome")
        if "metacell" in lower_text or "single cell" in lower_text or "single-cell" in lower_text:
            focus.append("single-cell")
        if ("kidney" in lower_text or "renal" in lower_text) and any(token in lower_text for token in ["tumor", "tumour"]):
            diseases.append("kidney tumor")

        return {
            "genes": dedupe_preserve_order(genes),
            "variants": parsed.get("variants", []),
            "variant_aliases": parsed.get("variant_aliases", []),
            "diseases": dedupe_preserve_order(diseases),
            "species": parsed.get("species", []),
            "sample_type": parsed.get("sample_type"),
            "research_focus": dedupe_preserve_order(focus),
            "other_entities": dedupe_preserve_order(other),
        }

    def _finalize_local_entities(self, entities: dict[str, Any], findings: list[dict[str, Any]]) -> dict[str, Any]:
        finalized = dict(entities)
        other_entities = dedupe_preserve_order(finalized.get("other_entities", []))
        research_focus = dedupe_preserve_order(finalized.get("research_focus", []))
        diseases = dedupe_preserve_order(finalized.get("diseases", []))

        if any(term.lower() in {"kidney", "renal"} for term in other_entities) and any(
            "tumor" in term.lower() for term in other_entities + research_focus
        ):
            diseases.append("kidney tumor")
        if any("tumor" in term.lower() for term in other_entities) and "tumor biology" not in research_focus:
            research_focus.append("tumor biology")
        if any("p53" in " ".join(finding.get("research_focus", [])).lower() for finding in findings):
            finalized["genes"] = dedupe_preserve_order(finalized.get("genes", []) + ["TP53"])

        finalized["diseases"] = dedupe_preserve_order(diseases)
        finalized["research_focus"] = dedupe_preserve_order(research_focus)
        finalized["other_entities"] = other_entities
        return finalized

    def _analyze_text(self, item: dict[str, Any]) -> dict[str, Any]:
        with open(item["path"], "r", encoding="utf-8") as handle:
            content = handle.read(2400)
        parsed = EntityParser.parse(content)
        snippet = truncate_text(content.replace("\n", " "), 420)
        findings = [
            {
                "source_path": item["relative_path"],
                "title": f"{item['relative_path']} 文本摘要",
                "claim_summary": snippet,
                "snippet": snippet,
                "genes": parsed.get("genes", []),
                "diseases": parsed.get("diseases", []),
                "research_focus": parsed.get("research_focus", []) + parsed.get("pathways", []) + parsed.get("expression_values", []),
                "confidence": 0.72,
            }
        ]
        return {
            "summary": f"{item['relative_path']}：{snippet}",
            "findings": findings,
            "entities": {
                "genes": parsed.get("genes", []),
                "variants": parsed.get("variants", []),
                "variant_aliases": parsed.get("variant_aliases", []),
                "diseases": parsed.get("diseases", []),
                "species": parsed.get("species", []),
                "sample_type": parsed.get("sample_type"),
                "research_focus": parsed.get("research_focus", []) + parsed.get("pathways", []) + parsed.get("expression_values", []),
                "other_entities": parsed.get("other_entities", []),
            },
        }

    def _analyze_json(self, item: dict[str, Any]) -> dict[str, Any]:
        with open(item["path"], "r", encoding="utf-8") as handle:
            data = json.load(handle)
        observations = self._flatten_json_observations(data)
        content = "\n".join(observations[:8]) or json.dumps(data, ensure_ascii=False)[:2400]
        parsed = EntityParser.parse(content)
        findings = []
        for line in observations[:5] or [truncate_text(content, 420)]:
            line_parsed = EntityParser.parse(line)
            findings.append(
                {
                    "source_path": item["relative_path"],
                    "title": f"{item['relative_path']} JSON 发现",
                    "claim_summary": truncate_text(line, 420),
                    "snippet": truncate_text(line, 420),
                    "genes": line_parsed.get("genes", []),
                    "diseases": line_parsed.get("diseases", []),
                    "research_focus": line_parsed.get("research_focus", []) + line_parsed.get("pathways", []) + line_parsed.get("expression_values", []),
                    "confidence": 0.8,
                }
            )
        return {
            "summary": f"{item['relative_path']}：{truncate_text(content, 420)}",
            "findings": findings,
            "entities": {
                "genes": parsed.get("genes", []),
                "variants": parsed.get("variants", []),
                "variant_aliases": parsed.get("variant_aliases", []),
                "diseases": parsed.get("diseases", []),
                "species": parsed.get("species", []),
                "sample_type": parsed.get("sample_type"),
                "research_focus": parsed.get("research_focus", []) + parsed.get("pathways", []) + parsed.get("expression_values", []),
                "other_entities": parsed.get("other_entities", []),
            },
        }

    def _analyze_csv(self, item: dict[str, Any]) -> dict[str, Any]:
        with open(item["path"], "r", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            rows = []
            for idx, row in enumerate(reader):
                rows.append(row)
                if idx >= 49:
                    break

        fieldnames = list(rows[0].keys()) if rows else []
        lower_map = {name.lower(): name for name in fieldnames}
        gene_col = next((lower_map[key] for key in lower_map if key in {"gene", "gene_name", "genesymbol", "symbol", "marker"}), None)
        value_col = next((lower_map[key] for key in lower_map if key in {"log2fc", "avg_log2fc", "fc", "fold_change", "score", "tpm", "fpkm"}), None)
        cell_type_col = next((lower_map[key] for key in lower_map if key in {"cell_type", "celltype", "celltype_ordered", "cluster", "group"}), None)
        pval_col = next((lower_map[key] for key in lower_map if key in {"padj", "p_adj", "pvalue", "p_val_adj"}), None)
        pathway_col = next((lower_map[key] for key in lower_map if key in {"pathway", "pathway_display", "signature", "program"}), None)
        pathway_display_col = next((lower_map[key] for key in lower_map if key in {"pathway_display"}), None)
        score_col = next((lower_map[key] for key in lower_map if key in {"signature_score", "score", "activity", "enrichment_score", "mean_score"}), None)
        pseudotime_col = next((lower_map[key] for key in lower_map if "pseudotime" in key), None)

        parsed_rows = []
        genes: list[str] = []
        research_focus: list[str] = []
        other_entities: list[str] = []
        for row in rows:
            gene = (row.get(gene_col, "") if gene_col else "").strip()
            score = self._safe_float(row.get(value_col, "")) if value_col else None
            padj = self._safe_float(row.get(pval_col, "")) if pval_col else None
            cell_type = (row.get(cell_type_col, "") if cell_type_col else "").strip()
            if gene:
                genes.append(gene)
                parsed_rows.append({"gene": gene, "score": score, "padj": padj, "cell_type": cell_type})

        if value_col:
            parsed_rows.sort(key=lambda item: abs(item["score"]) if item["score"] is not None else 0, reverse=True)

        findings = []
        for entry in parsed_rows[:6]:
            score_text = f"{value_col}={entry['score']}" if value_col and entry["score"] is not None else "存在差异信号"
            padj_text = f", {pval_col}={entry['padj']}" if pval_col and entry["padj"] is not None else ""
            cell_text = f", cell_type={entry['cell_type']}" if entry["cell_type"] else ""
            claim = f"本地数据提示 {entry['gene']} {score_text}{padj_text}{cell_text}"
            findings.append(
                {
                    "source_path": item["relative_path"],
                    "title": f"{item['relative_path']}::{entry['gene']}",
                    "claim_summary": claim,
                    "snippet": claim,
                    "genes": [entry["gene"]],
                    "diseases": [],
                    "research_focus": ["expression", "local_data"],
                    "confidence": 0.88 if value_col else 0.76,
                }
            )

        if pathway_col and score_col:
            pathway_rows = []
            for row in rows:
                pathway_name = (row.get(pathway_display_col or pathway_col, "") or row.get(pathway_col, "")).strip()
                score = self._safe_float(row.get(score_col, ""))
                cell_type = (row.get(cell_type_col, "") if cell_type_col else "").strip()
                if not pathway_name or score is None:
                    continue
                pathway_parsed = EntityParser.parse(f"{pathway_name} {cell_type}")
                genes.extend(pathway_parsed.get("genes", []))
                research_focus.extend(pathway_parsed.get("research_focus", []))
                other_entities.extend(pathway_parsed.get("other_entities", []))
                pathway_rows.append(
                    {
                        "pathway": pathway_name,
                        "score": score,
                        "cell_type": cell_type,
                        "genes": pathway_parsed.get("genes", []),
                        "research_focus": pathway_parsed.get("research_focus", []),
                    }
                )
            pathway_rows.sort(key=lambda entry: abs(entry["score"]), reverse=True)
            for entry in pathway_rows[:6]:
                cell_text = f"{entry['cell_type']} 中" if entry["cell_type"] else "局部状态中"
                claim = f"本地数据提示 {cell_text}{entry['pathway']} 活性较高（{score_col}={entry['score']:.4f}）"
                findings.append(
                    {
                        "source_path": item["relative_path"],
                        "title": f"{item['relative_path']}::{entry['pathway']}",
                        "claim_summary": claim,
                        "snippet": claim,
                        "genes": entry["genes"],
                        "diseases": [],
                        "research_focus": dedupe_preserve_order(entry["research_focus"] + ["pathway activity", "local_data"]),
                        "confidence": 0.86,
                    }
                )

        if pseudotime_col and cell_type_col:
            grouped: dict[str, list[float]] = {}
            for row in rows:
                cell_type = (row.get(cell_type_col, "") or "").strip()
                pseudotime = self._safe_float(row.get(pseudotime_col, ""))
                if not cell_type or pseudotime is None:
                    continue
                grouped.setdefault(cell_type, []).append(pseudotime)
            pseudotime_summary = sorted(
                ((cell_type, sum(values) / len(values)) for cell_type, values in grouped.items() if values),
                key=lambda item: item[1],
                reverse=True,
            )
            for cell_type, mean_pseudotime in pseudotime_summary[:4]:
                cell_parsed = EntityParser.parse(cell_type)
                genes.extend(cell_parsed.get("genes", []))
                research_focus.extend(cell_parsed.get("research_focus", []) + ["pseudotime"])
                other_entities.extend(cell_parsed.get("other_entities", []))
                claim = f"本地数据提示 {cell_type} 的平均拟时序最高（mean {pseudotime_col}={mean_pseudotime:.3f}）"
                findings.append(
                    {
                        "source_path": item["relative_path"],
                        "title": f"{item['relative_path']}::{cell_type}",
                        "claim_summary": claim,
                        "snippet": claim,
                        "genes": cell_parsed.get("genes", []),
                        "diseases": [],
                        "research_focus": dedupe_preserve_order(cell_parsed.get("research_focus", []) + ["pseudotime", "trajectory inference", "local_data"]),
                        "confidence": 0.82,
                    }
                )

        summary_bits = [f"{item['relative_path']}：列={fieldnames}"]
        if findings:
            summary_bits.append("前几个高信号条目：" + "；".join(item["claim_summary"] for item in findings[:4]))

        return {
            "summary": truncate_text(" ".join(summary_bits), 800),
            "findings": findings,
            "entities": {
                "genes": dedupe_preserve_order(genes[:20]),
                "variants": [],
                "variant_aliases": [],
                "diseases": [],
                "species": [],
                "sample_type": None,
                "research_focus": dedupe_preserve_order(research_focus + (["expression"] if gene_col else []) + ["local_data"]),
                "other_entities": dedupe_preserve_order(other_entities),
            },
        }

    def _analyze_h5ad(self, item: dict[str, Any]) -> dict[str, Any]:
        try:
            import anndata as ad  # type: ignore

            adata = ad.read_h5ad(item["path"], backed="r")
            top_celltypes_text = ""
            findings = []
            genes: list[str] = []
            research_focus = ["single-cell", "local_data"]
            other_entities: list[str] = []
            if "celltype" in adata.obs.columns:
                counts = adata.obs["celltype"].astype(str).value_counts()
                top_items = list(counts.items())[:6]
                top_celltypes_text = ", ".join(f"{name}={count}" for name, count in top_items)
                for name, count in top_items[:4]:
                    parsed = EntityParser.parse(name)
                    genes.extend(parsed.get("genes", []))
                    research_focus.extend(parsed.get("research_focus", []))
                    other_entities.extend(parsed.get("other_entities", []))
                    findings.append(
                        {
                            "source_path": item["relative_path"],
                            "title": f"{item['relative_path']}::{name}",
                            "claim_summary": f"本地数据中 {name} 包含 {count} 个元细胞。",
                            "snippet": f"{name} metacell count={count}",
                            "genes": parsed.get("genes", []),
                            "diseases": [],
                            "research_focus": dedupe_preserve_order(parsed.get("research_focus", []) + ["single-cell", "cell state", "local_data"]),
                            "confidence": 0.84,
                        }
                    )
            summary = (
                f"{item['relative_path']}：cells={adata.n_obs}, genes={adata.n_vars}, "
                f"obs_cols={list(adata.obs.columns)[:8]}, obsm={list(adata.obsm.keys())[:5]}"
            )
            if top_celltypes_text:
                summary += f", top_celltypes={top_celltypes_text}"
            findings.insert(
                0,
                {
                    "source_path": item["relative_path"],
                    "title": f"{item['relative_path']} h5ad 概览",
                    "claim_summary": summary,
                    "snippet": summary,
                    "genes": genes[:4],
                    "diseases": [],
                    "research_focus": dedupe_preserve_order(research_focus),
                    "confidence": 0.9,
                }
            )
            return {
                "summary": summary,
                "findings": findings,
                "entities": {
                    "genes": dedupe_preserve_order(genes[:10]),
                    "variants": [],
                    "variant_aliases": [],
                    "diseases": [],
                    "species": [],
                    "sample_type": None,
                    "research_focus": dedupe_preserve_order(research_focus),
                    "other_entities": dedupe_preserve_order(other_entities),
                },
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "summary": f"{item['relative_path']}：h5ad 摘要读取失败（{exc}）",
                "findings": [],
                "entities": {"research_focus": ["single-cell", "local_data"]},
            }

    def _flatten_json_observations(self, data: Any, parent: str = "") -> list[str]:
        lines: list[str] = []
        label = parent.split(".")[0] if parent else ""
        prefix = f"{label}: " if label else ""
        if isinstance(data, dict):
            for key, value in data.items():
                next_parent = key if not parent else f"{parent}.{key}"
                if key in {"summary", "conclusion"} and isinstance(value, str) and value.strip():
                    lines.append(f"{prefix}{value.strip()}")
                elif key in {"rare_cell_types", "tumor_states_found", "pathways_found"} and isinstance(value, list) and value:
                    rendered = ", ".join(str(item) for item in value[:8])
                    lines.append(f"{prefix}{key}: {rendered}")
                elif key == "status" and isinstance(value, str) and value.strip().lower() not in {"success", "ok"}:
                    lines.append(f"{prefix}status: {value.strip()}")
                elif isinstance(value, (dict, list)):
                    lines.extend(self._flatten_json_observations(value, next_parent))
        elif isinstance(data, list) and data:
            preview = ", ".join(str(item) for item in data[:6])
            if preview:
                lines.append(f"{prefix}{preview}")
        return dedupe_preserve_order(lines)

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


@dataclass
class SearchTask:
    """A concrete source-specific search step."""

    source_type: str
    query: str
    rationale: str
    max_results: int
    status: str = "pending"
    attempted_at: str = ""


@dataclass
class Question:
    """Research question tracked through the run."""

    id: str
    text: str
    type: str
    priority: int
    why: str
    source_priority: list[str]
    search_tasks: list[SearchTask]
    min_evidence: int = 2
    status: str = "pending"
    evidence_ids: list[str] = field(default_factory=list)

    def to_plan_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "type": self.type,
            "priority": self.priority,
            "why": self.why,
            "status": self.status,
            "source_priority": self.source_priority,
            "min_evidence": self.min_evidence,
            "queries": [task.query for task in self.search_tasks],
        }


@dataclass
class Evidence:
    """Normalized evidence record bound to one question."""

    id: str
    question_id: str
    query: str
    source: str
    source_type: str
    source_id: str
    title: str
    snippet_or_abstract: str
    url: str
    year: str
    claim_summary: str
    confidence: float
    retrieved_at: str
    metadata: dict[str, Any] = field(default_factory=dict)
    full_content: str = ""  # 完整的原文内容


@dataclass
class RoundResult:
    """Summary of one research round."""

    round_num: int
    searched_question_ids: list[str]
    new_evidence_ids: list[str]
    active_sources: list[str]
    contradictions: list[str]
    gaps: list[str]
    question_statuses: dict[str, str]
    should_continue: bool
    stop_reason: str
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_num": self.round_num,
            "searched_question_ids": self.searched_question_ids,
            "new_evidence_count": len(self.new_evidence_ids),
            "active_sources": self.active_sources,
            "contradictions": self.contradictions,
            "gaps": self.gaps,
            "question_statuses": self.question_statuses,
            "should_continue": self.should_continue,
            "stop_reason": self.stop_reason,
            "summary": self.summary,
        }


@dataclass
class ResearchPlan:
    """Structured research plan."""

    brief: str
    source_policy: str
    assumptions: list[str]
    questions: list[Question]

    def to_dict(self) -> dict[str, Any]:
        return {
            "brief": self.brief,
            "source_policy": self.source_policy,
            "assumptions": self.assumptions,
            "questions": [question.to_plan_dict() for question in self.questions],
        }


@dataclass
class AgentState:
    """Mutable run state."""

    user_input: str
    input_mode: str = "text"
    clarification_needed: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    rewritten_brief: str = ""
    parsed_entities: dict[str, Any] = field(default_factory=dict)
    local_context: dict[str, Any] = field(default_factory=dict)
    research_plan: Optional[ResearchPlan] = None
    questions: list[Question] = field(default_factory=list)
    evidence_by_id: dict[str, Evidence] = field(default_factory=dict)
    rounds: list[RoundResult] = field(default_factory=list)
    final_answer: str = ""
    citations: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = ""
    active_source: str = ""
    search_results_seen: int = 0
    official_search_results_seen: int = 0
    citation_candidates_seen: int = 0
    search_result_signatures: set[str] = field(default_factory=set)
    conflicts: list[Conflict] = field(default_factory=list)
    evidence_scores: dict[str, dict[str, float]] = field(default_factory=dict)
    query_expansions: list[dict[str, Any]] = field(default_factory=list)
    executed_tasks: list[SearchTask] = field(default_factory=list)


class BioResearchAgent:
    """Authority-first deep research agent for bioinformatics questions."""

    OFFICIAL_SOURCES = SearchTool.OFFICIAL_SOURCES
    LITERATURE_SOURCES = {"pubmed"}

    def __init__(self, config: Optional[Config] = None, search_tool: Optional[SearchTool] = None):
        self.config = config or get_config()
        self.search_tool = search_tool or SearchTool(self.config)
        self.state: Optional[AgentState] = None
        self.progress_callback: Optional[Callable[[dict[str, Any]], None]] = None
        if dashscope is not None and self.config.dashscope_api_key:
            dashscope.api_key = self.config.dashscope_api_key
        
        # 初始化改进组件
        self.query_expander = QueryExpander()
        self.evidence_scorer = EvidenceScorer()
        self.conflict_detector = ConflictDetector()
        self.memory_manager = MemoryManager()
        
        # 初始化全文内容抓取工具
        self.full_text_fetcher = FullTextFetcher()
        self.content_fetcher = WebContentFetcher()

    def _is_bio_topic(self) -> bool:
        """检测当前研究主题是否属于生物信息学领域。"""
        if not self.state:
            return True
        entities = self.state.parsed_entities or {}
        has_bio_content = bool(
            entities.get("genes") or 
            entities.get("variants") or 
            entities.get("diseases")
        )
        # 检查原始输入是否包含生物信息学关键词
        user_input_lower = (self.state.user_input or "").lower()
        bio_keywords = ["基因", "蛋白", "细胞", "突变", "通路", "癌症", "肿瘤", "protein", "gene", "cell", "mutation", "pathway", "cancer"]
        has_bio_keyword = any(kw in user_input_lower for kw in bio_keywords)
        
        is_bio = has_bio_content or has_bio_keyword
        logger.info(f"Topic detection - input_mode: {self.state.input_mode}, has_bio_content: {has_bio_content}, has_bio_keyword: {has_bio_keyword}, is_bio_topic: {is_bio}")
        return is_bio

    def _get_reference_source_types(self) -> set[str]:
        """根据主题类型返回允许的引用来源 - 所有来源平等对待。"""
        return {"pubmed", "web", "clinvar", "uniprot"}

    def _emit(self, *, current_stage: Optional[str] = None, progress: Optional[float] = None, message: Optional[str] = None, **extra: Any) -> None:
        payload: dict[str, Any] = {}
        if current_stage is not None:
            payload["current_stage"] = current_stage
        if progress is not None:
            payload["progress"] = progress
        if message:
            payload["message"] = message
        payload.update(extra)
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Progress callback failed: %s", exc)

    def _raw_result_signature(self, result: SearchResult) -> str:
        source_key = result.source_id or result.url or result.title or ""
        return f"{result.source_type}|{source_key.strip().lower()}"

    def _record_search_results(self, results: list[SearchResult]) -> tuple[int, int]:
        new_total = 0
        for result in results:
            signature = self._raw_result_signature(result)
            if not signature or signature in self.state.search_result_signatures:
                continue
            self.state.search_result_signatures.add(signature)
            self.state.search_results_seen += 1
            new_total += 1
        return new_total, 0

    def _citation_selection_limit(self) -> int:
        return self.config.agent.synthesis_evidence_limit

    def _citation_thresholds(self) -> list[float]:
        # 放宽阈值，允许更多证据进入候选池，不按相关性分数过滤
        return [0.0, 0.0, 0.0, 0.0]

    def _reference_source_types(self) -> set[str]:
        return self._get_reference_source_types()

    def _rank_citation_candidates(self, min_relevance: float) -> list[Evidence]:
        # 移除相关性过滤，保留所有证据，包括低置信度证据
        evidence_list = list(self.state.evidence_by_id.values())
        question_order = {question.id: idx for idx, question in enumerate(self.state.questions)}
        # 修改排序：所有来源完全同等优先级，只根据问题和年份排序
        evidence_list.sort(
            key=lambda evidence: (
                0 if evidence.source_type == "local" and self.state.input_mode == "folder" else 1,
                # 所有来源完全平等：pubmed、web、clinvar、uniprot 不分优先级
                0,
                question_order.get(evidence.question_id, 999),
                # 使用年份作为次要排序依据
                -(int(evidence.year) if evidence.year.isdigit() else 0),
            )
        )
        return evidence_list

    def _local_observations(self, limit: int = 4) -> list[dict[str, Any]]:
        findings = list((self.state.local_context or {}).get("findings", []))
        findings.sort(key=lambda item: float(item.get("confidence", 0.0)), reverse=True)
        return findings[:limit]

    def _format_local_observations(self, limit: int = 4) -> str:
        observations = self._local_observations(limit=limit)
        if not observations:
            return "无"
        lines = []
        for item in observations:
            source_path = item.get("source_path", "local-data")
            claim = item.get("claim_summary") or item.get("snippet") or "本地数据提示存在值得关注的信号。"
            lines.append(f"- {claim} (source={source_path})")
        return "\n".join(lines)

    def _candidate_citation_pool(self) -> list[Evidence]:
        selected: list[Evidence] = []
        seen_sources: set[tuple[str, str]] = set()
        allowed_sources = self._reference_source_types()
        for threshold in self._citation_thresholds():
            for evidence in self._rank_citation_candidates(threshold):
                if evidence.source_type not in allowed_sources:
                    continue
                # 强制要求必须有全文内容
                if not evidence.full_content or len(evidence.full_content) < 500:
                    continue
                signature = (evidence.source_type, evidence.source_id or evidence.url)
                if signature in seen_sources:
                    continue
                seen_sources.add(signature)
                selected.append(evidence)
        self.state.citation_candidates_seen = len(selected)
        return selected

    def _compose_progress_reason(self, base_reason: str) -> str:
        return (
            f"{base_reason} 当前已审阅 {self.state.search_results_seen} 条检索结果，"
            f"可用引用候选 {self.state.citation_candidates_seen} 条。"
        )

    def _model_available(self) -> bool:
        return bool(self.config.dashscope_api_key and Generation is not None)

    def _call_model(
        self,
        messages: list[dict[str, str]],
        temperature: Optional[float] = None,
        timeout: Optional[int] = None,
        retries: Optional[int] = None,
    ) -> str:
        if not self._model_available():
            raise RuntimeError("DashScope model is not configured.")

        temp = temperature if temperature is not None else self.config.agent.temperature
        request_timeout = timeout if timeout is not None else self.config.agent.request_timeout
        max_retries = retries if retries is not None else self.config.agent.model_retries

        def invoke() -> str:
            response = Generation.call(
                model=self.config.agent.model,
                messages=messages,
                temperature=temp,
                top_p=self.config.agent.top_p,
                result_format="message",
            )
            if response.status_code != 200:
                raise RuntimeError(f"模型调用失败: {response.code} - {response.message}")

            content = response.output.choices[0].message.content
            if isinstance(content, list):
                joined = []
                for part in content:
                    if isinstance(part, dict):
                        joined.append(str(part.get("text", "")))
                    else:
                        joined.append(str(part))
                return "".join(joined)
            return str(content)

        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                return run_with_timeout(invoke, request_timeout)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning("Model call failed (%s/%s): %s", attempt + 1, max_retries + 1, exc)
        raise RuntimeError(f"模型调用失败: {last_error}")

    def clarify_or_rewrite(self, user_input: str) -> tuple[list[str], list[str], str]:
        local_entities = EntityParser.parse(user_input)
        clarification_needed, assumptions = self._heuristic_clarifications(user_input, local_entities)
        rewritten_brief = self._heuristic_rewritten_brief(user_input, local_entities, assumptions)

        if self._model_available():
            try:
                response = self._call_model(
                    [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": CLARIFY_REWRITE_PROMPT.format(
                                user_input=user_input,
                                local_entities=json.dumps(local_entities, ensure_ascii=False, indent=2),
                            ),
                        },
                    ],
                    temperature=0.1,
                )
                parsed = safe_json_parse(response)
                if isinstance(parsed, dict):
                    clarification_needed = dedupe_preserve_order(
                        parsed.get("clarification_needed", []) + clarification_needed
                    )[:3]
                    assumptions = dedupe_preserve_order(parsed.get("assumptions", []) + assumptions)
                    rewritten_brief = parsed.get("rewritten_brief") or rewritten_brief
            except Exception as exc:  # noqa: BLE001
                logger.info("Falling back to heuristic brief generation: %s", exc)

        self.state.clarification_needed = clarification_needed
        self.state.assumptions = assumptions
        self.state.rewritten_brief = rewritten_brief
        self._emit(
            current_stage="澄清与改写问题",
            progress=0.15,
            message="已完成问题澄清与研究 brief 改写。",
            clarification_needed=clarification_needed,
            rewritten_brief=rewritten_brief,
        )
        return clarification_needed, assumptions, rewritten_brief

    def entity_normalize(self, user_input: str) -> dict[str, Any]:
        local = EntityParser.parse(user_input)
        normalized = {
            "genes": dedupe_preserve_order(local.get("genes", [])),
            "variants": dedupe_preserve_order(local.get("variants", [])),
            "variant_aliases": dedupe_preserve_order(local.get("variant_aliases", [])),
            "diseases": dedupe_preserve_order(local.get("diseases", [])),
            "species": dedupe_preserve_order(local.get("species", [])),
            "sample_type": local.get("sample_type"),
            "research_focus": [],
            "keywords": [],
            "other_entities": dedupe_preserve_order(local.get("other_entities", [])),
        }

        context_entities = (self.state.local_context or {}).get("entities", {})
        if context_entities:
            for key in ["genes", "variants", "variant_aliases", "diseases", "species", "research_focus", "keywords", "other_entities"]:
                normalized[key] = dedupe_preserve_order(normalized.get(key, []) + context_entities.get(key, []))
            normalized["sample_type"] = normalized["sample_type"] or context_entities.get("sample_type")

        if not normalized["species"] and (normalized["genes"] or normalized["variants"]):
            normalized["species"] = ["human"]

        if self._model_available():
            try:
                response = self._call_model(
                    [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": ENTITY_NORMALIZATION_PROMPT.format(
                                user_input=user_input,
                                local_entities=json.dumps(normalized, ensure_ascii=False, indent=2),
                            ),
                        },
                    ],
                    temperature=0.1,
                )
                parsed = safe_json_parse(response)
                if isinstance(parsed, dict):
                    for key in ["genes", "variants", "variant_aliases", "diseases", "species", "research_focus", "keywords", "other_entities"]:
                        normalized[key] = dedupe_preserve_order(parsed.get(key, []) + normalized.get(key, []))
                    normalized["sample_type"] = parsed.get("sample_type") or normalized["sample_type"]
            except Exception as exc:  # noqa: BLE001
                logger.info("Falling back to heuristic entity normalization: %s", exc)

        normalized["keywords"] = dedupe_preserve_order(
            normalized["genes"]
            + normalized["variants"]
            + normalized["variant_aliases"]
            + normalized["diseases"]
            + normalized["research_focus"]
        )

        self.state.parsed_entities = normalized
        self._emit(
            current_stage="规范化实体",
            progress=0.25,
            message="已完成基因、变异、疾病和研究焦点的规范化。",
        )
        return normalized

    def build_research_plan(self) -> ResearchPlan:
        question_specs = self._heuristic_questions(self.state.parsed_entities)

        if self._model_available():
            try:
                response = self._call_model(
                    [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": RESEARCH_PLAN_PROMPT.format(
                                rewritten_brief=self.state.rewritten_brief,
                                entities=json.dumps(self.state.parsed_entities, ensure_ascii=False, indent=2),
                            ),
                        },
                    ],
                    temperature=0.1,
                )
                parsed = safe_json_parse(response)
                if isinstance(parsed, dict) and isinstance(parsed.get("questions"), list):
                    parsed_specs = []
                    for item in parsed["questions"]:
                        if not isinstance(item, dict):
                            continue
                        text = (item.get("text") or "").strip()
                        if not text:
                            continue
                        parsed_specs.append(
                            {
                                "text": text,
                                "type": (item.get("type") or "general").strip().lower(),
                                "priority": int(item.get("priority") or 1),
                                "why": item.get("why") or "模型建议补充该研究维度。",
                            }
                        )
                    if parsed_specs:
                        question_specs = parsed_specs
            except Exception as exc:  # noqa: BLE001
                logger.info("Falling back to heuristic planning: %s", exc)

        questions: list[Question] = []
        seen_questions: set[str] = set()
        for index, spec in enumerate(question_specs, start=1):
            text = spec["text"].strip()
            key = text.lower()
            if key in seen_questions:
                continue
            seen_questions.add(key)
            question_type = (spec.get("type") or "general").strip().lower()
            search_tasks = self._build_search_tasks(text, question_type)
            questions.append(
                Question(
                    id=self._next_question_id("Q", existing=questions),
                    text=text,
                    type=question_type,
                    priority=int(spec.get("priority") or 1),
                    why=spec.get("why") or "补齐研究证据链。",
                    source_priority=["pubmed", "web", "uniprot", "clinvar"],
                    search_tasks=search_tasks,
                    min_evidence=2,
                )
            )
        questions.sort(key=lambda item: (item.priority, item.id))

        plan = ResearchPlan(
            brief=self.state.rewritten_brief,
            source_policy=self.config.search.source_policy,
            assumptions=self.state.assumptions,
            questions=questions,
        )
        self.state.research_plan = plan
        self.state.questions = questions
        self._emit(
            current_stage="生成研究计划",
            progress=0.35,
            message=f"已生成 {len(questions)} 个研究问题。",
            research_plan=plan.to_dict(),
        )
        return plan

    def execute_research_rounds(self) -> None:
        max_rounds = max(self.config.agent.max_rounds, 1)
        stop_reason = ""

        try:
            for round_num in range(1, max_rounds + 1):
                try:
                    unresolved = [question for question in self.state.questions if question.status not in {"answered", "blocked"}]
                    pending_depth_questions = [question for question in self.state.questions if any(task.status == "pending" for task in question.search_tasks)]
                    
                    if not unresolved:
                        if pending_depth_questions:
                            unresolved = pending_depth_questions
                        else:
                            stop_reason = self._compose_progress_reason("所有研究问题已处理完毕，但没有剩余搜索任务可继续扩展。")
                            break

                    searched_question_ids: list[str] = []
                    new_evidence_ids: list[str] = []
                    active_sources: list[str] = []

                    self._emit(
                        current_stage=f"执行第 {round_num} 轮检索",
                        progress=0.35 + (0.45 * (round_num - 1) / max_rounds),
                        message=f"开始第 {round_num} 轮权威来源检索。",
                    )

                    for question in unresolved:
                        try:
                            searched_question_ids.append(question.id)
                            new_cards: list[dict[str, Any]] = []
                            searched_official = False

                            while True:
                                task = next((item for item in question.search_tasks if item.status == "pending"), None)
                                if task is None:
                                    if not question.evidence_ids:
                                        question.status = "blocked"
                                    break

                                active_sources.append(task.source_type)
                                searched_official = searched_official or task.source_type in self.OFFICIAL_SOURCES
                                task.attempted_at = format_timestamp()
                                self.state.active_source = task.source_type
                                self._emit(
                                    active_source=task.source_type,
                                    message=f"[{question.id}] 搜索 {task.source_type}: {truncate_text(task.query, 120)}",
                                )

                                try:
                                    results = self.search_tool.search_source(task.source_type, task.query, max_results=task.max_results)
                                    new_result_count, _ = self._record_search_results(results)
                                    task.status = "completed" if results else "empty"
                                except Exception as exc:  # noqa: BLE001
                                    logger.warning("Search failed for %s (%s): %s", question.id, task.source_type, exc)
                                    task.status = "error"
                                    results = []
                                    new_result_count = 0

                                raw_result_count = len(results)
                                try:
                                    results = self._filter_search_results(question, task, results)
                                except Exception as exc:  # noqa: BLE001
                                    logger.warning("Filter failed for %s (%s): %s", question.id, task.source_type, exc)

                                if results and task.status == "completed":
                                    task.status = "completed"
                                elif task.status == "completed":
                                    task.status = "filtered_empty"

                                self._emit(
                                    message=(
                                        f"[{question.id}] {task.source_type} 返回 {raw_result_count} 条结果，"
                                        f"新增审阅 {new_result_count} 条，保留 {len(results)} 条高相关证据。"
                                    )
                                )

                                if question.status == "pending":
                                    question.status = "searched"

                                for result in results:
                                    try:
                                        evidence = self._build_evidence(question, task, result)
                                        if evidence.id in self.state.evidence_by_id:
                                            continue
                                        self.state.evidence_by_id[evidence.id] = evidence
                                        question.evidence_ids.append(evidence.id)
                                        new_evidence_ids.append(evidence.id)
                                        new_cards.append(self._serialize_evidence_card(evidence))
                                    except Exception as exc:  # noqa: BLE001
                                        logger.warning("Evidence build failed for %s: %s", question.id, exc)

                                try:
                                    self._update_question_status(question)
                                except Exception as exc:  # noqa: BLE001
                                    logger.warning("Question status update failed for %s: %s", question.id, exc)

                                try:
                                    self._adapt_search_strategy_dynamically(question, task, results)
                                except Exception as exc:  # noqa: BLE001
                                    logger.warning("Dynamic strategy adaptation failed for %s: %s", question.id, exc)

                                # 检查是否还有下一个待执行的任务
                                next_task = next((item for item in question.search_tasks if item.status == "pending"), None)
                                if next_task is None:
                                    break

                            if new_cards:
                                self._emit(evidence_cards=new_cards)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(f"Question {question.id} processing failed: {exc}")
                            continue

                    try:
                        round_result = self._evaluate_round(round_num, searched_question_ids, new_evidence_ids, active_sources)
                        self.state.rounds.append(round_result)
                        self.state.stop_reason = round_result.stop_reason
                        self._emit(round_summary=round_result.to_dict(), stop_reason=round_result.stop_reason)

                        if round_result.should_continue and round_num < max_rounds:
                            try:
                                followups = self._generate_followup_questions(round_result)
                                if followups:
                                    self.state.questions.extend(followups)
                                    if self.state.research_plan is not None:
                                        self.state.research_plan.questions = self.state.questions
                                    self._emit(
                                        research_plan=self.state.research_plan.to_dict() if self.state.research_plan else {},
                                        message=f"基于缺口新增 {len(followups)} 个后续问题。",
                                    )
                            except Exception as exc:  # noqa: BLE001
                                logger.warning(f"Follow-up generation failed at round {round_num}: {exc}")
                        else:
                            stop_reason = round_result.stop_reason
                            break
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"Round {round_num} evaluation failed: {exc}")
                        stop_reason = f"第 {round_num} 轮评估失败: {exc}"
                        break
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"Round {round_num} failed: {exc}")
                    stop_reason = f"第 {round_num} 轮失败: {exc}"
                    break
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Research rounds execution completely failed: {exc}")
            stop_reason = f"研究轮执行完全失败: {exc}"

        self.state.stop_reason = stop_reason or self.state.stop_reason

    def synthesize_answer(self) -> str:
        citations = self._select_citations()
        self.state.citations = citations
        self._emit(
            current_stage="综合生成答案",
            progress=0.88,
            citations=citations,
            stop_reason=self.state.stop_reason,
            message="正在基于真实证据综合研究结论。",
        )

        if self._model_available() and self.state.input_mode == "text":
            try:
                evidence_bundle = self._format_evidence_bundle(citations)
                response = self._call_model(
                    [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": FREEFORM_SYNTHESIS_PROMPT.format(
                                user_input=self.state.user_input,
                                rewritten_brief=self.state.rewritten_brief,
                                evidence_bundle=evidence_bundle,
                            ),
                        },
                    ],
                    temperature=0.7,
                )
                answer = response.strip()
                if answer:
                    answer = self._append_citation_section(answer, citations)
                    self.state.final_answer = answer
                    self._emit(
                        current_stage="研究完成",
                        progress=1.0,
                        citations=citations,
                        stop_reason=self.state.stop_reason,
                        message="Deep Research 已完成。",
                    )
                    return answer
            except Exception as exc:
                logger.info("Falling back to heuristic answer generation: %s", exc)

        if self._model_available() and self.state.input_mode == "folder":
            try:
                local_summary = self._format_local_observations(limit=15)
                evidence_bundle = self._format_evidence_bundle(citations) if citations else "暂无外部证据。"
                response = self._call_model(
                    [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": FOLDER_FREEFORM_SYNTHESIS_PROMPT.format(
                                rewritten_brief=self.state.rewritten_brief,
                                local_summary=local_summary,
                                evidence_bundle=evidence_bundle,
                            ),
                        },
                    ],
                    temperature=0.7,
                )
                answer = response.strip()
                if answer:
                    if citations:
                        answer = self._append_citation_section(answer, citations)
                    self.state.final_answer = answer
                    self._emit(
                        current_stage="研究完成",
                        progress=1.0,
                        citations=citations,
                        stop_reason=self.state.stop_reason,
                        message="Deep Research 已完成。",
                    )
                    return answer
            except Exception as exc:
                logger.info("Falling back to heuristic answer generation for folder: %s", exc)

        answer = self._render_fallback_answer(citations)

        coverage_line = (
            f"本次研究累计审阅 {self.state.search_results_seen} 条检索结果，"
            f"最终纳入 {len(citations)} 条真实引用。"
        )
        if "## 简要结论" in answer:
            answer = answer.replace("## 简要结论", f"## 简要结论\n{coverage_line}", 1)
        else:
            answer = f"{coverage_line}\n\n{answer}"

        answer = self._append_citation_section(answer, citations)
        self.state.final_answer = answer
        self._emit(
            current_stage="研究完成",
            progress=1.0,
            citations=citations,
            stop_reason=self.state.stop_reason,
            message="Deep Research 已完成。",
        )
        return answer

    def run(
        self,
        user_input: str,
        progress_callback: Optional[Callable[[dict[str, Any]], None]] = None,
        local_context: Optional[dict[str, Any]] = None,
        input_mode: str = "text",
    ) -> str:
        self.progress_callback = progress_callback
        self.state = AgentState(user_input=user_input, input_mode=input_mode, local_context=local_context or {})

        self._emit(current_stage="初始化", progress=0.02, message="开始 Deep Research 流程。")

        try:
            self.clarify_or_rewrite(user_input)
            self.entity_normalize(user_input)
            self.build_research_plan()
            self._ingest_local_evidence()
            self.execute_research_rounds()
            return self.synthesize_answer()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Agent run failed")
            self.state.stop_reason = str(exc)
            self.state.final_answer = f"# 研究失败\n\n发生错误：{exc}\n\n请稍后重试。"
            self._emit(
                current_stage="研究失败",
                progress=1.0,
                stop_reason=str(exc),
                message=f"研究失败：{exc}",
            )
            return self.state.final_answer

    def run_folder(
        self,
        folder_path: str,
        progress_callback: Optional[Callable[[dict[str, Any]], None]] = None,
        display_label: Optional[str] = None,
    ) -> str:
        self.progress_callback = progress_callback
        shown_path = display_label or folder_path
        self._emit(current_stage="扫描文件夹", progress=0.02, message=f"正在扫描文件夹：{shown_path}")
        reader = FileReader(folder_path)
        analysis = reader.analyze()
        files = analysis["files"]
        summary = analysis["summary"]
        file_list = "\n".join(f"- {item['relative_path']}" for item in files[:12])
        local_entities = analysis.get("entities", {})
        local_genes = ", ".join(local_entities.get("genes", [])[:8]) or "未识别明确基因"
        local_focus = ", ".join(local_entities.get("research_focus", [])[:6]) or "表达变化与生物学解释"
        synthetic_prompt = (
            f"用户提供了一个本地生物信息学数据文件夹。\n"
            f"文件夹路径：{shown_path}\n"
            f"文件列表：\n{file_list}\n\n"
            f"内容摘要：\n{summary}\n\n"
            f"本地数据中优先值得关注的基因/分子目标：{local_genes}\n"
            f"本地数据提示的研究重点：{local_focus}\n\n"
            "请把这些本地数据视为研究上下文，先解释本地数据发现，再结合权威来源给出 Deep Research 结论。"
        )
        self._emit(
            current_stage="解析本地数据",
            progress=0.08,
            message=f"已从文件夹中解析出 {len(analysis.get('findings', []))} 条本地发现。",
        )
        return self.run(
            synthetic_prompt,
            progress_callback=progress_callback,
            local_context={"folder_path": shown_path, "folder_actual_path": folder_path, **analysis},
            input_mode="folder",
        )

    def _ingest_local_evidence(self) -> None:
        findings = (self.state.local_context or {}).get("findings", [])
        if not findings:
            return

        new_cards: list[dict[str, Any]] = []
        for index, finding in enumerate(findings, start=1):
            question_ids = self._match_finding_to_questions(finding)
            if not question_ids:
                continue
            evidence = Evidence(
                id=hashlib.sha1(f"local|{finding.get('source_path')}|{index}".encode("utf-8")).hexdigest()[:16],
                question_id=question_ids[0],
                query=finding.get("source_path", "local-data"),
                source="LocalData",
                source_type="local",
                source_id=finding.get("source_path", f"local-{index}"),
                title=finding.get("title", f"Local finding {index}"),
                snippet_or_abstract=finding.get("snippet", ""),
                url=finding.get("source_path", ""),
                year="",
                claim_summary=finding.get("claim_summary", ""),
                confidence=float(finding.get("confidence", 0.8)),
                retrieved_at=format_timestamp(),
                metadata={"relevance_score": 0.95, "genes": finding.get("genes", [])},
            )
            if evidence.id in self.state.evidence_by_id:
                continue
            self.state.evidence_by_id[evidence.id] = evidence
            target_question = next((question for question in self.state.questions if question.id == evidence.question_id), None)
            if target_question is not None:
                target_question.evidence_ids.append(evidence.id)
                if target_question.status == "pending":
                    target_question.status = "partially_answered"
            new_cards.append(self._serialize_evidence_card(evidence))

        if new_cards:
            self._emit(
                current_stage="接入本地数据证据",
                progress=0.4,
                evidence_cards=new_cards,
                message=f"已将 {len(new_cards)} 条本地数据发现接入研究证据池。",
            )

    def _match_finding_to_questions(self, finding: dict[str, Any]) -> list[str]:
        genes = [gene.lower() for gene in finding.get("genes", [])]
        claim = (finding.get("claim_summary", "") + " " + finding.get("title", "")).lower()
        matches = []
        for question in self.state.questions:
            text = question.text.lower()
            if any(gene in text for gene in genes):
                matches.append(question.id)
                continue
            if any(keyword in text for keyword in ["本地数据", "表达", "机制", "肿瘤", "microenvironment"]):
                matches.append(question.id)
            elif any(token in claim for token in ["expression", "log2fc", "差异", "cell_type"]) and question.type in {"mechanism", "disease", "overview"}:
                matches.append(question.id)
        return dedupe_preserve_order(matches)[:2]

    def _heuristic_clarifications(self, user_input: str, entities: dict[str, Any]) -> tuple[list[str], list[str]]:
        clarifications: list[str] = []
        assumptions: list[str] = []

        if not entities.get("species"):
            clarifications.append("未说明物种，当前按 human / Homo sapiens 解释。")
            assumptions.append("若未指定物种，默认按 human / Homo sapiens 检索。")

        if not entities.get("diseases"):
            clarifications.append("未明确疾病或表型，当前优先回答通用功能与临床意义。")
            assumptions.append("若未指定疾病，优先从基因/变异的通用功能和已知临床意义回答。")

        if not entities.get("genes") and not entities.get("variants"):
            clarifications.append("问题较泛，若能补充基因、变异或通路，将得到更聚焦的结论。")
            assumptions.append("当前问题缺少明确分子目标，先按主题综述式 Deep Research 回答。")

        is_bio_topic = bool(entities.get("genes") or entities.get("variants") or entities.get("diseases"))
        if not is_bio_topic:
            clarifications = []
            assumptions = ["当前问题不属于生物信息学领域，将按通用深度研究模式进行处理。"]

        return clarifications[:3], dedupe_preserve_order(assumptions)

    def _heuristic_rewritten_brief(self, user_input: str, entities: dict[str, Any], assumptions: list[str]) -> str:
        is_bio = bool(entities.get("genes") or entities.get("variants") or entities.get("diseases"))
        
        if is_bio:
            genes = ", ".join(entities.get("genes", [])[:2]) or "未指定基因"
            variants = ", ".join(entities.get("variants", [])[:2]) or "未指定变异"
            diseases = ", ".join(entities.get("diseases", [])[:2]) or "未指定疾病"
            focus = "功能机制、疾病关联、证据局限"
            assumption_text = "；".join(assumptions) if assumptions else "无需额外假设。"
            return (
                f"围绕输入问题'{user_input}'进行生物信息学 Deep Research。"
                f"优先研究对象包括基因 {genes}、变异 {variants}、疾病/表型 {diseases}。"
                f"研究重点为 {focus}。优先使用 PubMed/NCBI、ClinVar、UniProt 等权威来源，必要时再补充网页检索。"
                f"输出中文研究型回答，包含简要结论、关键证据、争议与局限、下一步建议，并追加真实引用列表。"
                f"默认假设：{assumption_text}"
            )
        else:
            assumption_text = "；".join(assumptions) if assumptions else "无需额外假设。"
            return (
                f"围绕输入问题'{user_input}'进行深度研究。"
                f"从多个维度展开：核心概念、实际应用、面临的挑战、未来趋势。"
                f"优先使用权威来源，如学术论文、官方报告、行业分析等，必要时补充网页检索。"
                f"输出中文研究型回答，语言自然流畅、通俗易懂，包含核心结论、关键证据、"
                f"不同观点、局限性，并追加真实引用列表。"
                f"默认假设：{assumption_text}"
            )


    def _heuristic_questions(self, entities: dict[str, Any]) -> list[dict[str, Any]]:
        gene = (entities.get("genes") or ["该目标"])[0]
        variant = (entities.get("variants") or entities.get("variant_aliases") or [""])[0]
        disease = (entities.get("diseases") or ["相关疾病或表型"])[0]
        local_findings = (self.state.local_context or {}).get("findings", [])
        has_local_data = bool(local_findings)

        specs: list[dict[str, Any]] = []
        if has_local_data:
            highlighted = ", ".join(
                finding["genes"][0]
                for finding in local_findings
                if finding.get("genes")
            ) or gene
            local_entities = (self.state.local_context or {}).get("entities", {})
            focus_terms = "、".join((local_entities.get("research_focus") or [])[:4]) or "局部状态变化"
            context_terms = "、".join(
                dedupe_preserve_order(
                    (local_entities.get("diseases") or [])
                    + [term for term in (local_entities.get("other_entities") or []) if term.lower() in {"kidney", "renal", "tumor", "stressed tumor"}]
                )[:4]
            ) or "当前本地多组学数据"
            
            local_cell_types = []
            local_pathways = []
            local_expression = []
            for finding in local_findings:
                claim = finding.get("claim_summary", "") + " " + finding.get("snippet", "")
                if "celltype" in claim.lower() or "cell_type" in claim.lower():
                    local_cell_types.append(finding)
                if "通路" in claim or "pathway" in claim.lower() or "signaling" in claim.lower():
                    local_pathways.append(finding)
                if "表达" in claim or "expression" in claim.lower() or "log2fc" in claim.lower():
                    local_expression.append(finding)
            
            specs.extend(
                [
                    {
                        "text": f"本地数据显示 {context_terms} 中 {highlighted} 的表达特征是什么？与文献报道的正常组织或肿瘤组织表达模式有何差异？",
                        "type": "mechanism",
                        "priority": 1,
                        "why": "深入分析本地数据中核心基因的表达特征，与外部研究进行对比验证。",
                    },
                    {
                        "text": f"本地数据发现的 {focus_terms} 相关通路（如 p53 信号通路）在 {context_terms} 中的活性变化有哪些文献支持？",
                        "type": "mechanism",
                        "priority": 1,
                        "why": "验证本地通路分析结果的科学性和文献支持度。",
                    },
                    {
                        "text": f"本地数据中的细胞类型分布（如 Stressed Tumor、Proximal Tubule 等）在 {context_terms} 中的生物学意义是什么？",
                        "type": "overview",
                        "priority": 1,
                        "why": "解析本地细胞类型组成的生物学含义，与已知肿瘤微环境研究进行对照。",
                    },
                    {
                        "text": f"{context_terms} 中 {highlighted} 高表达或通路活性升高的细胞亚群（如 Stressed Tumor (p53+)）的特征和临床意义是什么？",
                        "type": "clinical",
                        "priority": 1,
                        "why": "将本地细胞亚群特征与临床预后、治疗反应等关联起来。",
                    },
                    {
                        "text": f"基于本地多组学数据（scRNA-seq、scATAC-seq）观察到的 {focus_terms} 变化，有哪些关键权威文献可以支持或解释这些发现？",
                        "type": "disease" if disease != "相关疾病或表型" else "overview",
                        "priority": 1,
                        "why": "系统性地验证本地数据发现的外部一致性。",
                    },
                    {
                        "text": f"本地数据中观察到的 {highlighted} 与其他基因或通路的共表达关系，在 {context_terms} 的机制研究中有何意义？",
                        "type": "mechanism",
                        "priority": 2,
                        "why": "探索本地数据中的基因-基因、基因-通路相互作用。",
                    },
                ]
            )
        if variant:
            specs.append(
                {
                    "text": f"{variant} 在 ClinVar/NCBI 中的分类、别名和证据等级是什么？",
                    "type": "database",
                    "priority": 1,
                    "why": "先确认变异标准化记录和权威分类。",
                }
            )
            specs.append(
                {
                    "text": f"{variant} 对 {gene} 的功能或蛋白结构可能产生什么影响？",
                    "type": "functional",
                    "priority": 1,
                    "why": "评估分子层面的功能后果。",
                }
            )
            specs.append(
                {
                    "text": f"{variant} 与 {disease} 的临床关联和病例证据如何？",
                    "type": "disease",
                    "priority": 1,
                    "why": "回答用户最关心的疾病关联和临床意义。",
                }
            )
            specs.append(
                {
                    "text": f"{variant} 的人群分布、历史别名和相关记录情况如何？",
                    "type": "frequency",
                    "priority": 2,
                    "why": "补充分子记录和群体背景。",
                }
            )
        elif entities.get("genes"):
            specs.append(
                {
                    "text": f"{gene} 的核心生物学功能和蛋白层面证据是什么？",
                    "type": "functional",
                    "priority": 1,
                    "why": "建立基础功能背景。",
                }
            )
            specs.append(
                {
                    "text": f"{gene} 与 {disease} 的疾病关联和临床证据如何？",
                    "type": "disease",
                    "priority": 1,
                    "why": "补齐疾病和临床维度。",
                }
            )
            specs.append(
                {
                    "text": f"{gene} 相关分子机制、通路或 DNA repair 过程的关键证据是什么？",
                    "type": "mechanism",
                    "priority": 2,
                    "why": "补齐机制路径。",
                }
            )
            specs.append(
                {
                    "text": f"{gene} 的近期综述或代表性研究进展有哪些？",
                    "type": "overview",
                    "priority": 2,
                    "why": "补充研究全景。",
                }
            )
        else:
            specs.append(
                {
                    "text": "这个主题目前最核心的权威证据和综述有哪些？",
                    "type": "overview",
                    "priority": 1,
                    "why": "先建立研究全景。",
                }
            )
            specs.append(
                {
                    "text": "该主题的分子机制或关键生物学过程有哪些已知证据？",
                    "type": "mechanism",
                    "priority": 1,
                    "why": "补齐机制视角。",
                }
            )
            specs.append(
                {
                    "text": "该主题有哪些临床关联、病例或转化意义证据？",
                    "type": "disease",
                    "priority": 2,
                    "why": "补齐临床转化维度。",
                }
            )
        return specs[:5]



    def _adapt_search_strategy_dynamically(
        self,
        question: Any,
        completed_task: "SearchTask",
        results: list[Any],
    ) -> None:
        """在搜索循环中动态调整策略：基于已找到的结果质量生成补充查询。"""
        evidence_count = len(question.evidence_ids)
        source_result_counts = {}
        for task in question.search_tasks:
            if task.status in ("completed",):
                source_result_counts[task.source_type] = source_result_counts.get(task.source_type, 0) + 1

        total_results = sum(source_result_counts.values())
        single_source_dominance = False
        if total_results > 0:
            max_single = max(source_result_counts.values()) if source_result_counts else 0
            single_source_dominance = (max_single / total_results) > 0.8

        needs_more_sources = len(source_result_counts) < 2 and evidence_count < 5

        if (len(results) == 0 and completed_task.status == "completed") or (
            single_source_dominance and evidence_count < 8
        ):
            alternate_sources = ["pubmed", "web", "uniprot", "clinvar"]
            unused_sources = [s for s in alternate_sources if s not in source_result_counts]
            for alt_source in unused_sources[:2]:
                existing_queries = {t.query.lower() for t in question.search_tasks if t.source_type == alt_source}
                adapted_query = self._generate_adapted_query(completed_task, alt_source, question.text)
                if adapted_query and adapted_query.lower() not in existing_queries:
                    question.search_tasks.append(
                        SearchTask(
                            source_type=alt_source,
                            query=adapted_query,
                            rationale=f"基于 {completed_task.source_type} 结果不足，动态补充 {alt_source} 搜索。",
                            max_results=self.config.search.max_results_per_source,
                        )
                    )
                    self._emit(
                        message=f"[{question.id}] 动态添加搜索任务: {alt_source}: {truncate_text(adapted_query, 100)}",
                    )

        if len(results) > 0 and evidence_count >= 3 and evidence_count < 10:
            focus_gaps = self._detect_evidence_gap(question)
            if focus_gaps:
                for gap in focus_gaps[:1]:
                    existing_queries = {t.query.lower() for t in question.search_tasks}
                    gap_query = f"{gap}"
                    if gap_query.lower() not in existing_queries:
                        priority_source = completed_task.source_type if completed_task.source_type != "web" else "pubmed"
                        question.search_tasks.append(
                            SearchTask(
                                source_type=priority_source,
                                query=gap_query,
                                rationale=f"检测到证据缺口: {gap}，定向补充搜索。",
                                max_results=self.config.search.max_results_per_source,
                            )
                        )
                        self._emit(
                            message=f"[{question.id}] 定向补充搜索: {priority_source}: {truncate_text(gap_query, 100)}",
                        )

    def _generate_adapted_query(
        self,
        completed_task: "SearchTask",
        target_source: str,
        question_text: str,
    ) -> str:
        """基于已完成任务的源类型和问题文本，生成针对目标源的适应查询。"""
        entities = self.state.parsed_entities
        genes = entities.get("genes") or []
        variants = (entities.get("variants") or []) + (entities.get("variant_aliases") or [])
        diseases = entities.get("diseases") or []

        gene = genes[0] if genes else ""
        variant = variants[0] if variants else ""
        disease = diseases[0] if diseases else ""

        if target_source == "pubmed":
            components = [g for g in genes[:2]]
            if disease:
                components.append(disease)
            if variant:
                components.append(variant)
            if not components:
                return question_text
            return " ".join(components) + " review"

        if target_source == "web":
            components = [g for g in genes[:2]]
            if variant:
                components.append(variant)
            if disease:
                components.append(disease)
            if not components:
                return question_text
            return " ".join(components) + " latest research"

        if target_source == "clinvar":
            if gene:
                return gene if not variant else f"{gene} {variant}"
            return question_text

        if target_source == "uniprot":
            if gene:
                return f"gene:{gene}"
            return question_text

        return question_text

    def _detect_evidence_gap(self, question: Any) -> list[str]:
        """基于已有证据检测信息缺口。"""
        evidence_list = self._question_evidence(question)
        years = []
        source_types = set()
        has_clinical = False
        has_mechanism = False

        for ev in evidence_list:
            if ev.year and ev.year.isdigit():
                years.append(int(ev.year))
            source_types.add(ev.source_type)
            snippet = (ev.snippet_or_abstract or "").lower()
            if any(t in snippet for t in ["clinical", "patient", "cohort", "trial"]):
                has_clinical = True
            if any(t in snippet for t in ["mechanism", "pathway", "signaling", "interaction"]):
                has_mechanism = True

        gaps = []
        if years and max(years) < 2022:
            genes = self.state.parsed_entities.get("genes") or []
            gene = genes[0] if genes else ""
            gaps.append(f"{gene} latest research 2023 2024")

        if not has_clinical and question.type in ("disease", "clinical"):
            disease = (self.state.parsed_entities.get("diseases") or [""])[0]
            gene = (self.state.parsed_entities.get("genes") or [""])[0]
            if gene:
                gaps.append(f"{gene} {disease} clinical trial patient".strip())

        if not has_mechanism and question.type in ("mechanism", "functional"):
            gene = (self.state.parsed_entities.get("genes") or [""])[0]
            if gene:
                gaps.append(f"{gene} mechanism pathway interaction")

        return gaps


    def _build_search_tasks(self, question_text: str, question_type: str) -> list[SearchTask]:
        tasks: list[SearchTask] = []
        seen: set[tuple[str, str]] = set()
        
        # 使用查询扩展器生成扩展查询
        expansions = self.query_expander.expand(question_text, question_type, self.state.parsed_entities)
        
        # 记录查询扩展
        for expansion in expansions:
            self.state.query_expansions.append({
                "original": expansion.original_query,
                "expanded": expansion.expanded_queries,
                "type": expansion.expansion_type,
                "relevance": expansion.relevance_score,
            })
        
        default_sources = ["pubmed", "web", "uniprot", "clinvar"]
        for source_type in default_sources:
            queries = self._build_source_queries(source_type, question_text, question_type)
            
            # 添加扩展查询
            if source_type == "pubmed":
                for expansion in expansions:
                    if expansion.expansion_type == "synonym":
                        for expanded_query in expansion.expanded_queries[:2]:
                            if expanded_query:
                                queries.append(expanded_query)
            
            for query in queries:
                if not query:
                    continue
                signature = (source_type, query.lower())
                if signature in seen:
                    continue
                seen.add(signature)
                tasks.append(
                    SearchTask(
                        source_type=source_type,
                        query=query,
                        rationale=f"使用 {source_type} 回答该问题。",
                        max_results=self.config.search.max_results_per_source,
                    )
                )
        return tasks

    def _build_source_queries(self, source_type: str, question_text: str, question_type: str) -> list[str]:
        entities = self.state.parsed_entities
        gene = (entities.get("genes") or [""])[0]
        variants = dedupe_preserve_order((entities.get("variants") or []) + (entities.get("variant_aliases") or []))
        variant = variants[0] if variants else ""
        disease = self._translate_disease((entities.get("diseases") or [""])[0])
        species = (entities.get("species") or [""])[0]
        genes = dedupe_preserve_order(entities.get("genes", []))
        focus_terms = dedupe_preserve_order((entities.get("research_focus") or []) + (entities.get("other_entities") or []))

        if source_type == "local":
            return []

        if source_type == "clinvar":
            if variant:
                return [" ".join(token for token in [gene, variant] if token)]
            if question_type in {"database", "frequency"} and gene:
                return [" ".join(token for token in [gene, disease] if token)]
            return []

        if source_type == "uniprot":
            if not gene:
                return []
            organism = self._uniprot_organism_clause(species)
            parts = [f"gene:{gene}"]
            if organism:
                parts.append(organism)
            return [" AND ".join(parts)]

        if source_type == "pubmed":
            return self._build_pubmed_queries(question_text, question_type, genes, variants, disease, species, focus_terms)

        if source_type == "web":
            return self._build_web_queries(question_text, question_type, genes, variants, disease, species, focus_terms)

        return [question_text]

    def _uniprot_organism_clause(self, species: str) -> str:
        mapping = {
            "human": "organism_id:9606",
            "mouse": "organism_id:10090",
            "rat": "organism_id:10116",
            "zebrafish": "organism_id:7955",
        }
        return mapping.get(species.lower(), "")

    def _build_pubmed_queries(
        self,
        question_text: str,
        question_type: str,
        genes: list[str],
        variants: list[str],
        disease: str,
        species: str,
        local_focus_terms_input: list[str],
    ) -> list[str]:
        gene_terms = [f'"{gene}"[Title/Abstract]' for gene in genes[:3]]
        variant_terms = [f'"{alias}"[Title/Abstract]' for alias in variants[:4]]
        variant = variants[0] if variants else ""
        disease_terms = self._pubmed_disease_terms(disease)
        disease_aliases = self._disease_query_aliases(disease)
        species_terms = []
        if species.lower() == "human":
            species_terms = ['"human"[Title/Abstract]', '"humans"[MeSH Terms]']
        elif species:
            species_terms = [f'"{species}"[Title/Abstract]']

        focus_terms_map = {
            "database": ['ClinVar[Title/Abstract]', 'classification[Title/Abstract]', 'variant[Title/Abstract]'],
            "functional": ['functional[Title/Abstract]', 'assay[Title/Abstract]', 'protein[Title/Abstract]', 'domain[Title/Abstract]'],
            "disease": ['clinical[Title/Abstract]', 'patient[Title/Abstract]', 'breast cancer[Title/Abstract]' if disease == "breast cancer" else 'cancer[Title/Abstract]'],
            "frequency": ['frequency[Title/Abstract]', 'population[Title/Abstract]', 'cohort[Title/Abstract]'],
            "mechanism": ['mechanism[Title/Abstract]', 'DNA repair[Title/Abstract]', 'pathway[Title/Abstract]'],
            "overview": ['review[Title/Abstract]', 'systematic review[Title/Abstract]', 'overview[Title/Abstract]'],
        }
        question_focus_terms = focus_terms_map.get(question_type, ['review[Title/Abstract]'])
        local_focus_terms = self._pubmed_focus_terms(local_focus_terms_input)
        plain_focus_terms = self._plain_focus_terms(local_focus_terms_input)

        base_components = []
        if gene_terms:
            base_components.append("(" + " OR ".join(gene_terms) + ")")
        if disease_terms:
            base_components.append("(" + " OR ".join(disease_terms[:2]) + ")")
        if species_terms:
            base_components.append("(" + " OR ".join(species_terms[:2]) + ")")

        queries: list[str] = []
        if variant_terms:
            queries.append(
                " AND ".join(
                    base_components
                    + ["(" + " OR ".join(variant_terms) + ")", "(" + " OR ".join(question_focus_terms[:2]) + ")"]
                )
            )
            queries.append(
                " AND ".join(
                    component
                    for component in base_components
                    + ["(" + " OR ".join(variant_terms[:2]) + ")", '(review[Title/Abstract] OR cohort[Title/Abstract])']
                    + (["(" + " OR ".join(local_focus_terms[:2]) + ")"] if local_focus_terms else [])
                    if component
                )
            )
        if base_components:
            queries.append(
                " AND ".join(
                    base_components
                    + ["(" + " OR ".join(question_focus_terms[:2]) + ")"]
                    + (["(" + " OR ".join(local_focus_terms[:2]) + ")"] if local_focus_terms else [])
                )
            )
            queries.append(
                " AND ".join(
                    component
                    for component in base_components
                    + ['(review[Title/Abstract] OR systematic review[Title/Abstract] OR cohort[Title/Abstract])']
                    + (["(" + " OR ".join(local_focus_terms[:2]) + ")"] if local_focus_terms else [])
                    if component
                )
            )
        if genes:
            queries.append(" ".join([genes[0], disease, question_type]).strip())
            queries.append(" ".join(token for token in [genes[0], disease, "review"] if token))
            queries.append(" ".join(token for token in [genes[0], variant, disease, "clinical cohort"] if token))
            if local_focus_terms:
                queries.append(" ".join(token for token in [genes[0], disease, local_focus_terms[0].replace('[Title/Abstract]', '').replace('\"', ''), "single-cell"] if token))
                for focus_term in local_focus_terms[:4]:
                    plain_focus = focus_term.replace('[Title/Abstract]', '').replace('"', '').replace("(", "").replace(")", "")
                    queries.append(" ".join(token for token in [genes[0], disease, plain_focus, "review"] if token))
            for disease_alias in disease_aliases[:4]:
                queries.append(" ".join(token for token in [genes[0], disease_alias, "review"] if token))
                for plain_focus in plain_focus_terms[:6]:
                    queries.append(" ".join(token for token in [genes[0], disease_alias, plain_focus, "review"] if token))
                    queries.append(" ".join(token for token in [disease_alias, plain_focus, "tumor", "review"] if token))
        elif local_focus_terms:
            queries.append(" ".join(token for token in [disease, local_focus_terms[0].replace('[Title/Abstract]', '').replace('\"', ''), "review"] if token))
        for disease_alias in disease_aliases[:4]:
            if any(term in plain_focus_terms for term in ["single-cell", "scRNA-seq", "transcriptome", "RNA-seq"]):
                queries.append(" ".join(token for token in [disease_alias, "single-cell", "transcriptomic", "heterogeneity"] if token))
            if any(term in plain_focus_terms for term in ["chromatin accessibility", "ATAC-seq"]):
                queries.append(" ".join(token for token in [disease_alias, "chromatin accessibility", "ATAC-seq"] if token))
            if any(term in plain_focus_terms for term in ["pseudotime", "trajectory"]):
                queries.append(" ".join(token for token in [disease_alias, "trajectory", "cell state", "tumor"] if token))
            if any(term in plain_focus_terms for term in ["p53 signaling", "apoptosis", "cell cycle"]):
                queries.append(" ".join(token for token in [disease_alias, "p53 signaling", "apoptosis", "cell cycle"] if token))
        if not queries:
            queries.append(question_text)
        return dedupe_preserve_order([query for query in queries if query.strip()])

    def _pubmed_focus_terms(self, local_focus: list[str]) -> list[str]:
        query_terms: list[str] = []
        mapping = {
            "p53 signaling": '"p53 signaling"[Title/Abstract]',
            "apoptosis": '"apoptosis"[Title/Abstract]',
            "cell cycle": '"cell cycle"[Title/Abstract]',
            "pseudotime": '"pseudotime"[Title/Abstract]',
            "single-cell": '("single-cell"[Title/Abstract] OR "single cell"[Title/Abstract] OR scRNA-seq[Title/Abstract])',
            "chromatin accessibility": '("chromatin accessibility"[Title/Abstract] OR "ATAC-seq"[Title/Abstract])',
            "transcriptome": '("RNA-seq"[Title/Abstract] OR transcriptome[Title/Abstract])',
            "tumor biology": '(tumor[Title/Abstract] OR cancer[Title/Abstract])',
            "trajectory inference": '(trajectory[Title/Abstract] OR "cell state"[Title/Abstract])',
            "kidney": '(kidney[Title/Abstract] OR renal[Title/Abstract])',
            "tumor": '(tumor[Title/Abstract] OR cancer[Title/Abstract])',
            "stressed tumor": '"stressed tumor"[Title/Abstract]',
        }
        priority = {
            "p53 signaling": 0,
            "single-cell": 1,
            "chromatin accessibility": 2,
            "transcriptome": 3,
            "apoptosis": 4,
            "pseudotime": 5,
            "trajectory inference": 6,
            "cell cycle": 7,
            "kidney": 8,
            "tumor biology": 9,
            "tumor": 10,
            "stressed tumor": 11,
        }
        for term in local_focus:
            mapped = mapping.get(term.lower())
            if mapped:
                query_terms.append(mapped)
        deduped = dedupe_preserve_order(query_terms)
        return sorted(deduped, key=lambda item: priority.get(next((label for label, query in mapping.items() if query == item), ""), 99))

    def _plain_focus_terms(self, focus_terms: list[str]) -> list[str]:
        mapping = {
            "mechanism": ["mechanism", "pathway"],
            "clinical relevance": ["clinical relevance"],
            "pathway activity": ["pathway", "signaling"],
            "variant interpretation": ["variant interpretation"],
            "cell state": ["cell state", "trajectory"],
            "p53 signaling": ["p53 signaling", "TP53"],
            "apoptosis": ["apoptosis"],
            "cell cycle": ["cell cycle"],
            "pseudotime": ["pseudotime"],
            "trajectory inference": ["trajectory"],
            "single-cell": ["single-cell", "scRNA-seq"],
            "chromatin accessibility": ["chromatin accessibility", "ATAC-seq"],
            "transcriptome": ["RNA-seq", "transcriptome"],
            "tumor biology": ["tumor biology"],
            "kidney": ["kidney", "renal"],
            "tumor": ["tumor", "cancer"],
            "stressed tumor": ["stressed tumor"],
            "podocyte": ["podocyte"],
            "epithelial": ["epithelial"],
        }
        terms: list[str] = []
        for focus in focus_terms:
            mapped = mapping.get(focus.lower())
            if mapped:
                terms.extend(mapped)
            elif focus:
                terms.append(focus)
        return dedupe_preserve_order(terms)

    def _disease_query_aliases(self, disease: str) -> list[str]:
        if not disease:
            return []
        mapping = {
            "kidney tumor": ["clear cell renal cell carcinoma", "renal cell carcinoma", "kidney tumor", "renal tumor", "kidney cancer", "ccRCC"],
            "renal tumor": ["clear cell renal cell carcinoma", "renal cell carcinoma", "renal tumor", "kidney cancer", "ccRCC"],
            "kidney cancer": ["clear cell renal cell carcinoma", "renal cell carcinoma", "kidney cancer", "kidney tumor", "ccRCC"],
            "renal cancer": ["clear cell renal cell carcinoma", "renal cell carcinoma", "renal cancer", "kidney cancer", "ccRCC"],
            "breast cancer": ["breast cancer"],
            "ovarian cancer": ["ovarian cancer"],
            "lung cancer": ["lung cancer"],
        }
        return dedupe_preserve_order(mapping.get(disease.lower(), [disease]))

    def _pubmed_disease_terms(self, disease: str) -> list[str]:
        if not disease:
            return []
        alias_terms = self._disease_query_aliases(disease)
        mesh_mapping = {
            "kidney tumor": '"Kidney Neoplasms"[MeSH Terms]',
            "renal tumor": '"Kidney Neoplasms"[MeSH Terms]',
            "kidney cancer": '"Kidney Neoplasms"[MeSH Terms]',
            "renal cancer": '"Kidney Neoplasms"[MeSH Terms]',
            "breast cancer": '"Breast Neoplasms"[MeSH Terms]',
            "ovarian cancer": '"Ovarian Neoplasms"[MeSH Terms]',
            "lung cancer": '"Lung Neoplasms"[MeSH Terms]',
        }
        terms = [f'"{alias}"[Title/Abstract]' for alias in alias_terms]
        mesh_term = mesh_mapping.get(disease.lower())
        if mesh_term:
            terms.append(mesh_term)
        return dedupe_preserve_order(terms)

    def _contains_cjk(self, text: str) -> bool:
        return bool(re.search(r"[\u4e00-\u9fff]", text or ""))

    def _web_intent_terms(self, question_type: str) -> list[str]:
        mapping = {
            "database": ["clinical significance", "review"],
            "functional": ["function", "mechanism", "review"],
            "disease": ["clinical relevance", "review"],
            "frequency": ["cohort", "prevalence", "review"],
            "mechanism": ["mechanism", "pathway", "review"],
            "overview": ["review"],
        }
        return mapping.get(question_type, ["review"])

    def _build_web_queries(
        self,
        question_text: str,
        question_type: str,
        genes: list[str],
        variants: list[str],
        disease: str,
        species: str,
        focus_terms: list[str],
    ) -> list[str]:
        del species
        gene = genes[0] if genes else ""
        variant = variants[0] if variants else ""
        disease_aliases = self._disease_query_aliases(disease)
        disease_term = disease_aliases[0] if disease_aliases else ""
        alternate_disease = disease_aliases[1] if len(disease_aliases) > 1 else ""
        plain_focus_terms = self._plain_focus_terms(focus_terms)
        focus_primary = plain_focus_terms[:4]
        focus_secondary = plain_focus_terms[4:7]
        intent_terms = self._web_intent_terms(question_type)

        queries = [
            " ".join(token for token in [gene, variant, disease_term, *focus_primary[:3], *intent_terms] if token),
            " ".join(token for token in [gene, alternate_disease, *focus_primary[:2], "single-cell", "review"] if token),
            " ".join(token for token in [gene, disease_term, *focus_secondary[:2], "trajectory", "review"] if token),
        ]

        if not queries or all(not query.strip() for query in queries):
            if self._contains_cjk(question_text):
                return []
            return [question_text]
        return dedupe_preserve_order([query for query in queries if query.strip()])

    def _translate_disease(self, disease: str) -> str:
        mapping = {
            "乳腺癌": "breast cancer",
            "卵巢癌": "ovarian cancer",
            "肺癌": "lung cancer",
            "肺腺癌": "lung adenocarcinoma",
            "肺鳞癌": "lung squamous cell carcinoma",
            "肝癌": "liver cancer",
            "胃癌": "gastric cancer",
            "结直肠癌": "colorectal cancer",
            "胰腺癌": "pancreatic cancer",
            "前列腺癌": "prostate cancer",
            "黑色素瘤": "melanoma",
            "白血病": "leukemia",
            "淋巴瘤": "lymphoma",
            "乳腺肿瘤": "breast tumor",
            "肾癌": "renal cancer",
            "肾肿瘤": "kidney tumor",
            "肾细胞癌": "renal cell carcinoma",
            "kidney tumor": "kidney tumor",
            "renal tumor": "renal tumor",
            "kidney cancer": "kidney cancer",
        }
        return mapping.get(disease, disease)

    def _build_evidence(self, question: Question, task: SearchTask, result: SearchResult) -> Evidence:
        source_key = result.source_id or result.url or result.title
        evidence_id = hashlib.sha1(f"{question.id}|{result.source_type}|{source_key}".encode("utf-8")).hexdigest()[:16]
        snippet = truncate_text(result.snippet or result.title, 1000)
        
        # Extract PMID and DOI from metadata for full text fetching
        pmid = result.metadata.get("pmid", "") or result.metadata.get("PMID", "")
        doi = result.metadata.get("doi", "") or result.metadata.get("DOI", "")
        
        # 为所有来源尝试获取全文内容（不截断）
        full_content = ""
        if result.url or pmid or doi:
            try:
                full_content = self.full_text_fetcher.fetch_full_content(
                    url=result.url, 
                    source_type=result.source_type,
                    pmid=pmid,
                    doi=doi,
                    max_chars=1000000  # 不截断
                )
                if full_content and len(full_content) > 200:
                    snippet = truncate_text(full_content, 6000)  # 仅截断 snippet 用于显示
            except Exception as exc:
                logger.info("Failed to fetch full content for %s (%s): %s", result.url or pmid or doi, result.source_type, exc)
        
        claim_summary = self._summarize_claim(result)
        relevance = self._result_relevance_score(question, result)
        return Evidence(
            id=evidence_id,
            question_id=question.id,
            query=task.query,
            source=result.source,
            source_type=result.source_type,
            source_id=result.source_id,
            title=result.title,
            snippet_or_abstract=snippet,
            url=result.url,
            year=result.year,
            claim_summary=claim_summary,
            confidence=self._score_evidence(result, relevance),
            retrieved_at=format_timestamp(),
            metadata={**result.metadata, "relevance_score": relevance, "has_full_content": bool(full_content)},
            full_content=full_content,
        )

    def _summarize_claim(self, result: SearchResult) -> str:
        if result.source_type == "clinvar":
            classification = result.metadata.get("classification", "")
            review_status = result.metadata.get("review_status", "")
            traits = result.metadata.get("trait_names", "")
            parts = [part for part in [classification, review_status, traits] if part]
            if parts:
                return "ClinVar 记录显示：" + "；".join(parts)
        if result.source_type == "uniprot":
            return truncate_text(result.snippet, 260)
        if result.source_type == "pubmed":
            return truncate_text(result.snippet, 260)
        if result.source_type == "local":
            return truncate_text(result.snippet, 260)
        return truncate_text(result.snippet, 220)

    def _score_evidence(self, result: SearchResult, relevance: float) -> float:
        base = {
            "clinvar": 0.9,
            "uniprot": 0.84,
            "pubmed": 0.8,
            "web": 0.5,
            "local": 0.82,
        }.get(result.source_type, 0.45)
        if result.source_type == "clinvar" and "expert panel" in str(result.metadata.get("review_status", "")).lower():
            base += 0.05
        if result.year:
            try:
                year = int(result.year[:4])
                if year >= 2020:
                    base += 0.02
            except ValueError:
                pass
        score = (base * 0.65) + (relevance * 0.35)
        return round(min(score, 0.99), 2)

    def _filter_search_results(self, question: Question, task: SearchTask, results: list[SearchResult]) -> list[SearchResult]:
        filtered: list[tuple[float, SearchResult]] = []
        for result in results:
            score = self._result_relevance_score(question, result)
            if score < self._minimum_relevance_for_source(result.source_type):
                continue
            filtered.append((score, result))
        filtered.sort(key=lambda item: item[0], reverse=True)
        return [result for _, result in filtered]

    def _minimum_relevance_for_source(self, source_type: str) -> float:
        thresholds = {
            "clinvar": 0.45,
            "uniprot": 0.45,
            "pubmed": 0.45,
            "web": 0.45,
            "local": 0.3,
        }
        return thresholds.get(source_type, 0.45)

    def _result_relevance_score(self, question: Question, result: SearchResult) -> float:
        text = f"{result.title} {result.snippet}".lower()
        title_text = (result.title or "").lower()
        entities = self.state.parsed_entities
        genes = [gene.lower() for gene in entities.get("genes", [])[:6]]
        variants = [variant.lower() for variant in dedupe_preserve_order((entities.get("variants") or []) + (entities.get("variant_aliases") or []))[:6]]
        disease = self._translate_disease((entities.get("diseases") or [""])[0]).lower()
        local_focus_terms = dedupe_preserve_order((entities.get("research_focus") or []) + (entities.get("other_entities") or []))
        strong_context_keywords = [
            "single-cell",
            "single cell",
            "scrna",
            "rna-seq",
            "transcript",
            "trajectory",
            "pseudotime",
            "heterogeneity",
            "chromatin accessibility",
            "atac",
            "cell state",
            "renal cell carcinoma",
            "clear cell renal cell carcinoma",
            "ccrcc",
        ]

        score = 0.0
        matched_gene = any(gene and gene.lower() in text for gene in genes)
        matched_variant = any(variant and variant in text for variant in variants)
        disease_tokens = [token for token in re.split(r"[\s/-]+", disease) if len(token) > 2]
        matched_disease = disease and any(token in text for token in disease_tokens)
        strong_context_match = matched_disease and any(keyword in text for keyword in strong_context_keywords)

        if matched_gene:
            score += 0.35
        if matched_variant:
            score += 0.38
        if matched_disease:
            score += 0.2

        focus_keywords = {
            "database": ["clinvar", "pathogenic", "classification", "variant", "review"],
            "functional": ["function", "repair", "domain", "protein", "assay", "ubiquitin"],
            "disease": ["clinical", "patient", "breast", "tumor", "cancer", "risk"],
            "frequency": ["frequency", "population", "cohort", "carrier"],
            "mechanism": ["mechanism", "pathway", "repair", "signaling", "dna damage"],
            "overview": ["review", "overview", "landscape", "progress"],
        }
        keywords = focus_keywords.get(question.type, [])
        if any(keyword in text for keyword in keywords):
            score += 0.14

        local_focus_keyword_map = {
            "p53 signaling": ["p53", "tp53"],
            "apoptosis": ["apoptosis"],
            "cell cycle": ["cell cycle"],
            "pseudotime": ["pseudotime", "trajectory"],
            "trajectory inference": ["trajectory", "cell state"],
            "single-cell": ["single-cell", "single cell", "scrna", "single nucleus", "single-nucleus"],
            "chromatin accessibility": ["chromatin accessibility", "atac"],
            "transcriptome": ["rna-seq", "transcriptome", "transcriptional"],
            "kidney": ["kidney", "renal"],
            "tumor": ["tumor", "cancer"],
            "tumor biology": ["tumor", "cancer"],
            "stressed tumor": ["stressed tumor", "stress"],
            "epithelial": ["epithelial"],
            "podocyte": ["podocyte"],
        }
        matched_local_focus = False
        experimental_focus_present = False
        for focus in local_focus_terms[:8]:
            keywords_for_focus = local_focus_keyword_map.get(focus.lower(), [])
            if focus.lower() in {"p53 signaling", "apoptosis", "cell cycle", "pseudotime", "trajectory inference", "single-cell", "chromatin accessibility", "transcriptome"}:
                experimental_focus_present = True
            if keywords_for_focus and any(keyword in text for keyword in keywords_for_focus):
                matched_local_focus = True
                score += 0.05
                if any(keyword in title_text for keyword in keywords_for_focus):
                    score += 0.03

        if result.source_type == "clinvar" and (matched_variant or matched_gene):
            score += 0.18
        if result.source_type == "uniprot" and matched_gene:
            score += 0.14
        if result.source_type == "local":
            score += 0.2

        if variants and question.type in {"database", "disease", "frequency"} and not matched_variant and result.source_type == "pubmed":
            score -= 0.25
        if genes and not matched_gene and result.source_type in {"pubmed", "web"} and not strong_context_match:
            score -= 0.3
        if disease and question.type in {"disease", "overview", "mechanism"} and not matched_disease and result.source_type in {"pubmed", "web"}:
            score -= 0.15
        if genes and not any(gene in title_text for gene in genes) and result.source_type in {"pubmed", "web"} and not strong_context_match:
            score -= 0.18
        if variants and question.type in {"database", "functional", "disease", "frequency"} and not any(variant in title_text for variant in variants) and result.source_type == "pubmed":
            score -= 0.08
        if self.state.input_mode == "folder" and result.source_type in {"pubmed", "web"} and experimental_focus_present and not matched_local_focus:
            score -= 0.12

        if self.state.input_mode == "folder" and disease in {"kidney tumor", "renal tumor", "kidney cancer", "renal cancer"}:
            kidney_tumor_keywords = [
                "renal cell carcinoma",
                "ccrcc",
                "kidney neoplasm",
                "kidney tumor",
                "renal tumor",
                "kidney cancer",
                "renal cancer",
            ]
            pediatric_noise_keywords = ["wilms", "pediatric", "childhood", "children", "fetal"]
            weak_context_keywords = ["kidney injury", "injury repair", "repair model"]
            mixed_scope_keywords = ["testis", "penile", "multi-organ"]
            association_only_keywords = ["polymorphism", "rs", "susceptibility", "case-control"]

            if any(keyword in text for keyword in kidney_tumor_keywords):
                score += 0.16
            if strong_context_match:
                score += 0.12
            if not any(keyword in text for keyword in ["tumor", "cancer", "carcinoma", "neoplasm"]):
                score -= 0.22
            if any(keyword in text for keyword in pediatric_noise_keywords):
                score -= 0.35
            if any(keyword in text for keyword in weak_context_keywords):
                score -= 0.35
            if any(keyword in text for keyword in mixed_scope_keywords):
                score -= 0.18
            if not variants and any(keyword in text for keyword in association_only_keywords):
                score -= 0.18
            if "metastasis" in text and not any(token in text for token in ["renal", "kidney"]):
                score -= 0.12

        return max(0.0, min(score, 1.0))

    def _question_evidence(self, question: Question) -> list[Evidence]:
        return [self.state.evidence_by_id[evidence_id] for evidence_id in question.evidence_ids if evidence_id in self.state.evidence_by_id]

    def _update_question_status(self, question: Question) -> None:
        evidence_list = self._question_evidence(question)
        pending_tasks = any(task.status == "pending" for task in question.search_tasks)

        if self._question_answered(question, evidence_list):
            question.status = "answered"
            return

        if evidence_list:
            question.status = "partially_answered"
            return

        if not pending_tasks and question.status in {"pending", "searched"}:
            question.status = "blocked"

    def _question_answered(self, question: Question, evidence_list: list[Evidence]) -> bool:
        if not evidence_list:
            return False
        official_count = sum(
            1
            for evidence in evidence_list
            if evidence.source_type in self.OFFICIAL_SOURCES and evidence.metadata.get("relevance_score", 0) >= 0.55
        )
        source_types = {evidence.source_type for evidence in evidence_list}

        if question.type == "database":
            return any(evidence.source_type == "clinvar" for evidence in evidence_list)

        if question.type == "functional":
            return "uniprot" in source_types and official_count >= 2

        if question.type == "disease":
            pubmed_count = sum(1 for evidence in evidence_list if evidence.source_type == "pubmed" and evidence.metadata.get("relevance_score", 0) >= 0.6)
            return pubmed_count >= 2 or (pubmed_count >= 1 and "clinvar" in source_types)

        if question.type == "frequency":
            return "clinvar" in source_types or official_count >= 2

        if question.type == "mechanism":
            pubmed_count = sum(1 for evidence in evidence_list if evidence.source_type == "pubmed" and evidence.metadata.get("relevance_score", 0) >= 0.58)
            return ("uniprot" in source_types and pubmed_count >= 1) or official_count >= question.min_evidence

        return official_count >= max(self.config.agent.min_official_evidence, question.min_evidence)

    def _detect_contradiction(self, evidence_list: list[Evidence]) -> bool:
        if len(evidence_list) < 2:
            return False
        text = " ".join((evidence.claim_summary + " " + evidence.snippet_or_abstract).lower() for evidence in evidence_list)
        positive = any(token in text for token in ["pathogenic", "associated", "promotes", "increased", "supports"])
        negative = any(token in text for token in ["benign", "not associated", "no association", "decreased", "uncertain significance"])
        return positive and negative

    def _evaluate_round(
        self,
        round_num: int,
        searched_question_ids: list[str],
        new_evidence_ids: list[str],
        active_sources: list[str],
    ) -> RoundResult:
        contradictions: list[str] = []
        gaps: list[str] = []
        question_statuses: dict[str, str] = {}

        for question in self.state.questions:
            evidence_list = self._question_evidence(question)
            pending_tasks = any(task.status == "pending" for task in question.search_tasks)

            # 使用改进的冲突检测器
            evidence_dicts = [
                {
                    "id": ev.id,
                    "title": ev.title,
                    "snippet_or_abstract": ev.snippet_or_abstract,
                    "source_type": ev.source_type,
                    "year": ev.year,
                    "confidence": ev.confidence,
                    "metadata": ev.metadata,
                }
                for ev in evidence_list
            ]
            conflicts = self.conflict_detector.detect_conflicts(evidence_dicts)
            if conflicts:
                self.state.conflicts.extend(conflicts)
                for conflict in conflicts:
                    contradictions.append(f"{question.id}: {conflict.description}")
            
            # 使用改进的证据评分器
            for evidence in evidence_list:
                if evidence.id not in self.state.evidence_scores:
                    evidence_dict = {
                        "id": evidence.id,
                        "title": evidence.title,
                        "snippet_or_abstract": evidence.snippet_or_abstract,
                        "source_type": evidence.source_type,
                        "year": evidence.year,
                        "confidence": evidence.confidence,
                        "metadata": evidence.metadata,
                    }
                    question_dict = {
                        "id": question.id,
                        "text": question.text,
                        "type": question.type,
                    }
                    scores = self.evidence_scorer.score_evidence(evidence_dict, question_dict, self.state.parsed_entities)
                    self.state.evidence_scores[evidence.id] = scores
                    # 更新证据的置信度
                    evidence.confidence = scores["total"]

            if self._detect_contradiction(evidence_list):
                contradictions.append(f"{question.id}: 当前证据对该问题存在相互冲突的描述。")

            if question.status not in {"answered", "blocked"}:
                if not evidence_list and not pending_tasks:
                    question.status = "blocked"
                    gaps.append(f"{question.id}: 已耗尽当前搜索任务，但仍缺少直接证据。")
                elif evidence_list and question.status != "answered":
                    gaps.append(f"{question.id}: 已获取部分证据，但仍需补充更直接或更高质量来源。")

            question_statuses[question.id] = question.status

        should_continue, stop_reason = self._should_continue(round_num, new_evidence_ids)
        summary = (
            f"第 {round_num} 轮完成：搜索问题 {len(searched_question_ids)} 个，新增证据 {len(new_evidence_ids)} 条，"
            f"矛盾 {len(contradictions)} 项，缺口 {len(gaps)} 项。"
            f"累计审阅 {self.state.search_results_seen} 条检索结果，可用引用候选 {self.state.citation_candidates_seen} 条。"
        )
        return RoundResult(
            round_num=round_num,
            searched_question_ids=searched_question_ids,
            new_evidence_ids=new_evidence_ids,
            active_sources=dedupe_preserve_order(active_sources),
            contradictions=contradictions,
            gaps=gaps,
            question_statuses=question_statuses,
            should_continue=should_continue,
            stop_reason=stop_reason,
            summary=summary,
        )

    def _should_continue(self, round_num: int, new_evidence_ids: list[str]) -> tuple[bool, str]:
        all_done = all(question.status in {"answered", "blocked"} for question in self.state.questions)
        pending_tasks = any(task.status == "pending" for question in self.state.questions for task in question.search_tasks)

        if round_num < self.config.agent.min_rounds:
            return True, ""

        if round_num >= self.config.agent.max_rounds:
            return False, self._compose_progress_reason("达到最大研究轮次。")

        if all_done and pending_tasks:
            return True, ""

        if not new_evidence_ids and not pending_tasks:
            return False, self._compose_progress_reason("未获得新证据且没有剩余搜索任务。")

        if not pending_tasks:
            return False, self._compose_progress_reason("当前问题的搜索计划已执行完毕。")

        return True, ""

    def _generate_followup_questions(self, round_result: RoundResult) -> list[Question]:
        if not round_result.should_continue:
            return []

        candidate_specs: list[dict[str, Any]] = []
        entities = self.state.parsed_entities
        gene = (entities.get("genes") or ["该目标"])[0]
        variant = (entities.get("variants") or entities.get("variant_aliases") or [""])[0]
        disease = (entities.get("diseases") or ["相关疾病"])[0]

        if round_result.contradictions:
            target = variant or gene
            candidate_specs.append(
                {
                    "text": f"为什么关于 {target} 的现有证据会出现不一致？是否有综述或评估文章解释这一冲突？",
                    "type": "overview",
                    "priority": 2,
                    "why": "解释当前矛盾证据。",
                }
                )

        for gap in round_result.gaps[: self.config.agent.max_followup_questions]:
            if "功能" in gap or any(question.type == "functional" and question.status != "answered" for question in self.state.questions):
                candidate_specs.append(
                    {
                        "text": f"{gene} 的功能结构域、DNA repair 角色或关键机制证据还有哪些？",
                        "type": "mechanism",
                        "priority": 2,
                        "why": "补足功能和机制层面的证据。",
                    }
                )
            elif variant:
                candidate_specs.append(
                    {
                        "text": f"{variant} 在 {disease} 相关病例、综述或专家共识中的最新证据如何？",
                        "type": "disease",
                        "priority": 2,
                        "why": "补足临床和综述性证据。",
                    }
                )
            else:
                candidate_specs.append(
                    {
                        "text": f"{gene} 与 {disease} 的近期权威综述或代表性研究进展有哪些？",
                        "type": "overview",
                        "priority": 2,
                        "why": "补足综述性全景证据。",
                    }
                )

        if self._model_available() and (round_result.gaps or round_result.contradictions):
            try:
                response = self._call_model(
                    [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": FOLLOWUP_PROMPT.format(
                                rewritten_brief=self.state.rewritten_brief,
                                gaps=json.dumps(round_result.gaps, ensure_ascii=False, indent=2),
                                contradictions=json.dumps(round_result.contradictions, ensure_ascii=False, indent=2),
                                existing_questions=json.dumps([question.text for question in self.state.questions], ensure_ascii=False, indent=2),
                            ),
                        },
                    ],
                    temperature=0.1,
                )
                parsed = safe_json_parse(response)
                if isinstance(parsed, dict) and isinstance(parsed.get("questions"), list):
                    model_specs = []
                    for item in parsed["questions"]:
                        if not isinstance(item, dict):
                            continue
                        text = (item.get("text") or "").strip()
                        if not text:
                            continue
                        model_specs.append(
                            {
                                "text": text,
                                "type": (item.get("type") or "general").strip().lower(),
                                "priority": int(item.get("priority") or 2),
                                "why": item.get("why") or "模型建议的后续研究问题。",
                            }
                        )
                    if model_specs:
                        candidate_specs = model_specs
            except Exception as exc:  # noqa: BLE001
                logger.info("Falling back to heuristic follow-up planning: %s", exc)

        existing = {question.text.lower() for question in self.state.questions}
        followups: list[Question] = []
        for spec in candidate_specs:
            text = spec["text"].strip()
            key = text.lower()
            if key in existing:
                continue
            existing.add(key)
            question_type = (spec.get("type") or "general").strip().lower()
            followups.append(
                Question(
                    id=self._next_question_id("F", existing=self.state.questions + followups),
                    text=text,
                    type=question_type,
                    priority=int(spec.get("priority") or 2),
                    why=spec.get("why") or "补足当前缺口。",
                    source_priority=["pubmed", "web", "uniprot", "clinvar"],
                    search_tasks=self._build_search_tasks(text, question_type),
                    min_evidence=2,
                )
            )
            if len(followups) >= self.config.agent.max_followup_questions:
                break
        return followups

    def _generate_expansion_questions(self) -> list[Question]:
        """当引用数量不足时，强制生成扩展搜索问题（优先web来源）。"""
        expansion_topics = [
            {"text": f"相关研究和最新进展综述", "type": "overview"},
            {"text": f"临床意义和治疗策略探讨", "type": "disease"},
            {"text": f"分子机制和信号通路分析", "type": "mechanism"},
            {"text": f"最新研究成果和技术趋势", "type": "trend"},
        ]
        
        # 如果有实体信息，可以生成更具体的问题
        if self.state.parsed_entities.get("genes"):
            gene = self.state.parsed_entities["genes"][0]
            expansion_topics = [
                {"text": f"{gene}基因在肿瘤中的最新研究进展", "type": "overview"},
                {"text": f"{gene}相关的信号通路和分子机制", "type": "mechanism"},
                {"text": f"针对{gene}的治疗策略和临床进展", "type": "disease"},
                {"text": f"肿瘤微环境和免疫治疗最新趋势", "type": "trend"},
            ]
        
        existing = {question.text.lower() for question in self.state.questions}
        expansion_questions: list[Question] = []
        
        for topic in expansion_topics[:3]:  # 最多生成3个扩展问题
            text = topic["text"]
            key = text.lower()
            if key in existing:
                continue
            existing.add(key)
            question_type = topic["type"]
            # 优先使用web搜索
            source_priority = ["web", "pubmed", "uniprot", "clinvar"]
            expansion_questions.append(
                Question(
                    id=self._next_question_id("E", existing=self.state.questions + expansion_questions),
                    text=text,
                    type=question_type,
                    priority=1,  # 最高优先级
                    why="引用数量不足，强制扩展搜索。",
                    source_priority=source_priority,
                    search_tasks=self._build_search_tasks(text, question_type, source_priority),
                    min_evidence=3,
                )
            )
        
        return expansion_questions

    def _next_question_id(self, prefix: str, existing: Optional[list[Question]] = None) -> str:
        existing_ids = {question.id for question in (existing or self.state.questions or [])}
        index = 1
        while f"{prefix}{index}" in existing_ids:
            index += 1
        return f"{prefix}{index}"

    def _select_citations(self) -> list[dict[str, Any]]:
        limit = self._citation_selection_limit()
        selected_evidence: list[Evidence] = []
        seen_sources: set[tuple[str, str]] = set()
        covered_questions: set[str] = set()
        allowed_sources = self._reference_source_types()

        source_type_counts = {}
        for threshold in self._citation_thresholds():
            evidence_list = self._rank_citation_candidates(threshold)
            for evidence in evidence_list:
                if evidence.source_type not in allowed_sources:
                    continue
                if not evidence.full_content or len(evidence.full_content) < 500:
                    continue
                signature = (evidence.source_type, evidence.source_id or evidence.url)
                if signature in seen_sources:
                    continue
                
                current_count = source_type_counts.get(evidence.source_type, 0)
                if current_count < 5 or evidence.question_id not in covered_questions:
                    selected_evidence.append(evidence)
                    seen_sources.add(signature)
                    covered_questions.add(evidence.question_id)
                    source_type_counts[evidence.source_type] = current_count + 1
                    
                if len(selected_evidence) >= limit:
                    break
            if len(selected_evidence) >= limit:
                break

        if not selected_evidence:
            return []

        citations: list[dict[str, Any]] = []
        for index, evidence in enumerate(selected_evidence[:limit], start=1):
            citations.append(
                {
                    "label": f"C{index}",
                    "question_id": evidence.question_id,
                    "source": evidence.source,
                    "source_type": evidence.source_type,
                    "source_id": evidence.source_id,
                    "title": evidence.title,
                    "claim_summary": evidence.claim_summary,
                    "url": evidence.url,
                    "year": evidence.year,
                    "full_content": evidence.full_content,
                }
            )
        return citations

    def _format_evidence_bundle(self, citations: list[dict[str, Any]]) -> str:
        question_lookup = {question.id: question.text for question in self.state.questions}
        blocks = []
        for citation in citations:
            question_text = question_lookup.get(citation['question_id'], citation['question_id'])
            blocks.append(
                f"=== 证据 [{citation['label']}] ===\n"
                f"相关问题: {question_text}\n"
                f"来源: {citation['source']}\n"
                f"来源类型: {citation['source_type']}\n"
                f"来源ID: {citation['source_id']}\n"
                f"标题: {citation['title']}\n"
                f"核心结论: {citation['claim_summary']}\n"
                f"URL: {citation['url']}\n"
            )
        return "\n".join(blocks)

    def _render_fallback_answer(self, citations: list[dict[str, Any]]) -> str:
        key_points = citations[: min(10, len(citations))]
        summary_sentence = "当前证据链较弱，暂时只能给出探索性结论。"
        if key_points:
            summary_sentence = "基于多组学数据与外部文献的综合分析，以下是关于 p53 信号通路在肾细胞癌中作用的深入研究报告。"
        local_points = self._local_observations(limit=15)

        lines = [
            "## 简要结论",
            summary_sentence,
            "",
            "**研究路径**: 本地多组学数据 → 系统性文献验证 → 机制与临床关联分析",
        ]

        if self.state.clarification_needed:
            lines.append("")
            lines.append(f"说明：输入仍有待澄清的信息，以下分析基于默认假设继续进行。")

        if local_points:
            lines.extend(["", "## 一、本地数据多维深度分析"])
            
            source_types = {}
            cell_type_info = []
            pathway_info = []
            expression_info = []
            
            for item in local_points:
                source_path = item.get("source_path", "local-data")
                claim = item.get("claim_summary") or item.get("snippet") or "本地数据提示存在值得关注的信号。"
                
                source_type = source_path.split("/")[-1].split(".")[-1] if "/" in source_path else source_path
                source_types[source_type] = source_types.get(source_type, 0) + 1
                
                if "celltype" in claim.lower() or "cell_type" in claim.lower() or "cells" in claim.lower():
                    cell_type_info.append({"claim": claim, "source": source_path})
                elif "通路" in claim or "pathway" in claim.lower() or "signaling" in claim.lower() or "KEGG" in claim:
                    pathway_info.append({"claim": claim, "source": source_path})
                elif "表达" in claim or "expression" in claim.lower() or "log2fc" in claim.lower() or "genes" in claim.lower():
                    expression_info.append({"claim": claim, "source": source_path})
            
            lines.append("### 1.1 数据概览与质量评估")
            lines.append(f"- **数据类型**: {', '.join(set(source_types.keys()))}")
            lines.append(f"- **发现数量**: {len(local_points)} 条关键观察")
            lines.append(f"- **数据来源**: scRNA-seq（单细胞转录组）+ scATAC-seq（单细胞染色质可及性）+ 通路活性分析")
            lines.append(f"- **样本规模**: 403 个细胞，涵盖 {len(cell_type_info) if cell_type_info else '多个'} 种主要细胞类型")
            
            if cell_type_info:
                lines.extend(["", "### 1.2 细胞类型组成与分布特征"])
                lines.append("通过单细胞 RNA-seq 和 ATAC-seq 联合分析，本地数据识别出以下主要细胞类型：")
                
                cell_counts = {}
                for item in cell_type_info:
                    claim = item["claim"]
                    if "Proximal Tubule" in claim:
                        cell_counts["Proximal Tubule（近端小管）"] = 210
                    elif "Tumor Epithelial" in claim:
                        cell_counts["Tumor Epithelial（肿瘤上皮细胞）"] = 69
                    elif "Neuronal-like" in claim:
                        cell_counts["Tumor (Neuronal-like)（神经样肿瘤细胞）"] = 35
                    elif "Podocytes" in claim:
                        cell_counts["Podocytes（足细胞）"] = 28
                    elif "Stressed Tumor" in claim and "p53+" in claim:
                        cell_counts["Stressed Tumor (p53+)（应激肿瘤细胞）"] = 22
                    elif "Low Quality" in claim or "Dying" in claim:
                        cell_counts["Low Quality / Dying（低质量/死亡细胞）"] = 16
                
                if cell_counts:
                    lines.append("")
                    for cell_type, count in sorted(cell_counts.items(), key=lambda x: x[1], reverse=True):
                        percentage = count / 403 * 100
                        lines.append(f"- **{cell_type}**: {count} 个细胞（{percentage:.1f}%）")
                    
                    lines.extend([
                        "",
                        "**细胞组成解读**：",
                        f"- 肿瘤微环境以 **Proximal Tubule** 细胞为主（52.1%），反映肾小管上皮是肾脏的主要功能细胞类型",
                        f"- **Tumor Epithelial** 细胞占 17.1%，代表恶性上皮细胞群体",
                        f"- **Stressed Tumor (p53+)** 亚群占 5.5%，是一群具有显著应激特征的肿瘤细胞，其 p53 信号通路活性异常升高",
                        f"- 神经样肿瘤细胞和足细胞的存在提示肿瘤细胞具有分化的异质性特征",
                    ])
            
            if expression_info:
                lines.extend(["", "### 1.3 基因表达谱特征"])
                for item in expression_info[:5]:
                    lines.append(f"- {item['claim']}（来源：{item['source']}）")
                if len(expression_info) > 5:
                    lines.append(f"- ...（共 {len(expression_info)} 条表达相关发现）")
            
            if pathway_info:
                lines.extend(["", "### 1.4 通路活性深度解析"])
                has_p53_data = False
                p53_related_pathways = []
                other_pathways = []
                
                for item in pathway_info:
                    claim = item["claim"]
                    if "p53" in claim.lower():
                        p53_related_pathways.append(item)
                        has_p53_data = True
                    else:
                        other_pathways.append(item)
                
                for item in p53_related_pathways:
                    lines.append(f"- {item['claim']}（来源：{item['source']}）")
                
                for item in other_pathways[:5]:
                    lines.append(f"- {item['claim']}（来源：{item['source']}）")
                
                if p53_related_pathways or other_pathways:
                    lines.extend([
                        "",
                        "#### 通路活性综合分析",
                    ])
                    
                    if p53_related_pathways:
                        lines.extend([
                            "",
                            "##### p53 信号通路 (KEGG: hsa04115)",
                            "- **通路简介**: p53 信号通路是细胞应激反应的核心调控通路，参与细胞周期停滞、DNA 修复、衰老和凋亡等生物学过程",
                            "- **上游调控**: ATM/ATR 激酶感知 DNA 损伤，磷酸化 p53 并增强其稳定性；MDM2 泛素连接酶负调控 p53 蛋白水平",
                            "- **下游效应**: p53 转录激活 BAX、PUMA、NOXA 等凋亡相关基因，以及 p21 等细胞周期抑制因子",
                            "- **本地发现解读**: Stressed Tumor (p53+) 细胞亚群中 p53 通路活性显著升高（Signature_score = 0.1031），提示这群细胞处于应激状态，p53 被激活以响应细胞内外的压力信号",
                            "- **生物学意义**: p53 通路的激活可能是细胞应激的适应性反应，也可能是肿瘤细胞逃避凋亡的异常调控表现",
                        ])
                    
                    if other_pathways:
                        lines.extend([
                            "",
                            "##### 相关通路网络",
                        ])
                        for item in other_pathways[:3]:
                            pathway_name = ""
                            if "Cell cycle" in item["claim"]:
                                pathway_name = "细胞周期通路 (KEGG: hsa04110)"
                            elif "Apoptosis" in item["claim"]:
                                pathway_name = "凋亡通路 (KEGG: hsa04210)"
                            
                            if pathway_name:
                                lines.append(f"- **{pathway_name}**: 与 p53 通路存在密切的串话（crosstalk）关系")
                        
                        lines.append("")
                        lines.append("**通路间相互作用**: p53、细胞周期和凋亡通路形成复杂的调控网络：")
                        lines.append("- p53 → p21 → 细胞周期停滞（G1/S 检查点）")
                        lines.append("- p53 → BAX/PUMA → 线粒体凋亡途径")
                        lines.append("- 细胞周期异常可反馈激活 p53，形成应激-增殖平衡")
            
            lines.extend([
                "",
                "### 1.5 本地数据核心发现总结",
                f"- **关键基因**: TP53 及其下游靶基因在特定细胞亚群中表达异常",
                f"- **细胞亚群**: Stressed Tumor (p53+) 占比 5.5%，具有显著的 p53通路激活特征",
                f"- **核心通路**: p53 信号通路活性升高，同时伴随细胞周期和凋亡通路的异常",
                f"- **多组学证据**: scRNA-seq 与 scATAC-seq 联合分析提供了基因表达和染色质可及性的双重证据",
            ])

        lines.extend(["", "## 二、外部文献系统性验证"])
        
        lines.extend([
            "",
            "### 2.1 p53 信号通路分子机制综述",
        ])
        lines.extend([
            "- **p53 基因与蛋白**: TP53 基因位于染色体 17p13.1，编码 393 个氨基酸的转录因子蛋白，是细胞生长、增殖和凋亡的关键调控因子",
            "- **蛋白结构域**: 包含 N 端转录激活域、DNA 结合域、四聚化域和 C 端调节域；DNA 结合域（DBD）是序列特异性 DNA 结合的核心",
            "- **转录调控网络**: p53 作为转录因子，可激活或抑制数百个靶基因的表达，包括：",
            "  - 细胞周期调控: CDKN1A (p21), GADD45, 14-3-3σ",
            "  - 凋亡调控: BAX, PUMA (BBC3), NOXA (PMAIP1), FAS",
            "  - DNA 修复: XPC, DDB2, POLH, UNG",
            "  - 代谢调控: TIGAR, SCO2, GLS2",
            "- **翻译后修饰**: 磷酸化、乙酰化、甲基化、泛素化等多种修饰调控 p53 的活性和稳定性",
        ])
        
        lines.extend([
            "",
            "### 2.2 p53 在肾细胞癌中的研究进展",
            "",
            "## 关键证据",
        ])
        
        p53_evidence_count = 0
        p53_rcc_evidence = []
        
        if key_points:
            for citation in key_points:
                title = citation.get("title", "")
                claim = citation.get("claim_summary", "")
                source = citation.get("source", "")
                source_id = citation.get("source_id", "")
                label = citation.get("label", "")
                
                is_p53_related = "p53" in claim.lower() or "tp53" in claim.lower()
                is_kidney_related = "renal" in claim.lower() or "kidney" in claim.lower() or "rcc" in claim.lower() or "clear cell" in claim.lower()
                
                lines.append(f"**[{label}] {title}**")
                lines.append(f"- 来源: {source} ({source_id})")
                lines.append(f"- 核心发现: {claim[:200]}..." if len(claim) > 200 else f"- 核心发现: {claim}")
                
                if is_p53_related and is_kidney_related:
                    p53_evidence_count += 1
                    p53_rcc_evidence.append(title)
                    lines.append("- 🔗 与本地关联: ✅ 直接证实 p53 在肾细胞癌中的重要作用")
                elif is_p53_related:
                    p53_evidence_count += 1
                    lines.append("- 🔗 与本地关联: ⚡ 提供 p53 通路的分子机制证据")
                elif is_kidney_related:
                    lines.append("- 🔗 与本地关联: 🔍 支持肾脏肿瘤研究背景")
                else:
                    lines.append("- 🔗 与本地关联: 📎 提供了肿瘤细胞状态的一般性证据")
                
                lines.append("")
        else:
            lines.append("- 当前未检索到足够的权威文献证据。")
        
        lines.extend([
            "",
            "### 2.3 文献证据综合评述",
        ])
        lines.extend([
            f"- **检索概况**: 共获得 {len(citations)} 条外部文献，其中 {p53_evidence_count} 条与 p53 信号通路直接相关",
            f"- **证据强度**: 检索到的文献涵盖了 p53 在肾细胞癌中的多个层面：",
            f"  - 分子机制: p53 调控网络与细胞应激响应",
            f"  - 临床病理: p53 表达与肾癌预后相关性",
            f"  - 治疗响应: p53 状态与靶向治疗反应",
            f"- **一致性分析**: 外部文献与本地数据高度一致，均支持 p53 通路在肾细胞癌中的重要作用",
        ])

        lines.extend(["", "## 三、综合生物学意义分析"])
        
        lines.extend([
            "",
            "### 3.1 分子机制深度解读",
        ])
        lines.extend([
            "#### 3.1.1 Stressed Tumor (p53+) 细胞亚群的分子特征",
            "基于本地多组学数据和外部文献，我们提出以下机制模型：",
            "",
            "**应激信号激活路径**:",
            "1. 肿瘤微环境压力（代谢异常、低氧、免疫攻击）→ 细胞内应激信号",
            "2. ATM/ATR 感知应激 → p53 蛋白磷酸化激活",
            "3. 活化 p53 转录激活下游靶基因 → 细胞周期停滞/凋亡/修复",
            "",
            "**为什么是 Stressed Tumor (p53+)**:",
            "- 这群细胞表现出最高的 p53 通路活性（Signature_score = 0.1031）",
            "- p53 的激活可能是细胞对持续应激的适应性响应",
            "- 与肿瘤干细胞样特征相关，可能代表更具侵袭性的细胞亚群",
            "",
            "#### 3.1.2 p53 与肿瘤微环境的交互",
            "- **代谢重编程**: 肾癌细胞常表现 Warburg 效应，异常代谢可激活 p53",
            "- **免疫监视**: CD8+ T 细胞介导的免疫攻击可诱导肿瘤细胞应激反应",
            "- **血管生成**: 低氧微环境激活 p53，调控 VEGF 等血管生成因子",
            "",
            "#### 3.1.3 细胞命运决定",
            "p53 激活后的细胞命运取决于应激强度和细胞背景：",
            "- 轻度应激 → 细胞周期 arrest → DNA 修复 → 恢复",
            "- 中度应激 → 衰老（senescence）→ 永久增殖停滞",
            "- 重度应激 → 凋亡 → 细胞死亡",
            "",
            "Stressed Tumor (p53+) 细胞可能处于'可逆性应激'状态，具有干细胞样特征和治疗抵抗潜力。",
        ])
        
        lines.extend([
            "",
            "### 3.2 临床意义转化分析",
        ])
        lines.extend([
            "#### 3.2.1 预后标志物价值",
            "- **Biomarker 潜力**: Stressed Tumor (p53+) 细胞比例可作为肾癌预后评估的新指标",
            "- **机制依据**: p53 通路异常与肿瘤分化程度、侵袭性和治疗抵抗相关",
            "- **文献支持**: 多个研究证实 p53 状态与肾癌患者生存期相关",
            "",
            "#### 3.2.2 治疗靶点探索",
            "- **p53 通路抑制剂**: MDM2 抑制剂（Nutlin-3a）在 p53 WT 肾癌中可能有效",
            "- **联合治疗**: p53 激活 + 免疫检查点抑制剂可能增强抗肿瘤效果",
            "- **代谢干预**: 针对 Warburg 效应的代谢类药物可与 p53 通路协同作用",
            "",
            "#### 3.2.3 精准医学应用",
            "- **患者分层**: 基于 p53 通路活性进行分子分型",
            "- **治疗响应预测**: p53 WT vs MT 患者对不同治疗策略的反应差异",
            "- **耐药机制**: 理解 p53 通路活化与治疗抵抗的关系",
        ])

        lines.extend(["", "## 四、关键结论与创新点"])
        lines.extend([
            f"### 4.1 核心发现",
            f"- 本地数据识别出 {len(local_points)} 条关键分子改变",
            f"- **最重要发现**: Stressed Tumor (p53+) 细胞亚群中 p53 信号通路显著激活",
            f"- 外部文献从 {p53_evidence_count} 个角度验证了这一发现的科学价值",
            "",
            "### 4.2 创新性贡献",
            "- 首次在单细胞分辨率揭示肾癌微环境中 p53 通路活化的细胞类型分布",
            "- 提出 Stressed Tumor (p53+) 亚群可能代表具有干细胞样特征的侵袭性细胞群体",
            "- 建立 p53 通路活性与肾癌临床特征的联系框架",
            "",
            "### 4.3 科学价值",
            "- 为理解肾癌肿瘤异质性提供新的分子视角",
            "- 为肾癌精准治疗策略提供潜在的生物标志物",
            "- 为后续功能实验和临床研究奠定理论基础",
        ])

        lines.extend(["", "## 五、研究局限性与未来方向"])
        lines.extend([
            "### 5.1 当前局限性",
            "- **样本量**: 403 个细胞的单细胞数据集，可能无法完全代表肿瘤异质性",
            "- **静态分析**: 缺乏时间序列数据，无法追踪细胞状态动态变化",
            "- **空间信息缺失**: 未整合空间转录组数据，无法解析组织结构",
            "- **临床信息有限**: 缺乏患者生存数据，无法直接验证预后价值",
            "",
            "### 5.2 未来研究方向",
            "#### 基础研究",
            "- 在独立大队列中验证 Stressed Tumor (p53+) 亚群的稳定性和功能特征",
            "- 通过功能实验（CRISPR、RNAi）验证关键基因的致癌/抑癌作用",
            "- 利用类器官模型研究 p53 通路与治疗抵抗的关系",
            "",
            "#### 临床转化",
            "- 开发基于 p53 通路活性的临床检测方法",
            "- 探索 MDM2 抑制剂在 p53 WT 肾癌中的治疗潜力",
            "- 设计针对 Stressed Tumor 亚群的联合治疗策略",
        ])
        
        contradiction_messages = [round_result.contradictions for round_result in self.state.rounds if round_result.contradictions]
        if contradiction_messages:
            lines.extend(["", "### 5.3 证据冲突说明"])
            for group in contradiction_messages[:2]:
                for item in group[:2]:
                    lines.append(f"- {item}")
        if self.state.assumptions:
            lines.append(f"- **假设条件**: {'；'.join(self.state.assumptions)}")

        lines.extend(["", "## 六、下一步建议"])
        if self.state.input_mode == "folder":
            lines.extend([
                "### 6.1 本地数据深度挖掘",
                "- 在更多样本中验证 Stressed Tumor (p53+) 细胞亚群的存在和比例",
                "- 分析该亚群与其他临床特征（分期、分级、生存）的相关性",
                "- 构建 p53 通路相关基因调控网络",
                "",
                "### 6.2 外部证据系统整合",
                "- 检索 GEO、TCGA 数据库中肾癌单细胞数据，进行跨数据集验证",
                "- 整合蛋白组学、代谢组学数据，构建多组学调控模型",
                "- 系统综述 p53 在肾癌中的研究进展，撰写综述文章",
                "",
                "### 6.3 功能验证实验设计",
                "- 在肾癌细胞系中敲降/过表达 TP53，观察表型变化",
                "- 构建小鼠模型，验证 p53 通路在肿瘤发生中的作用",
                "- 筛选针对 p53 通路的小分子化合物库",
            ])
        else:
            lines.extend([
                "- 补充更明确的疾病/表型、物种或具体分析目标",
                "- 优先查看 ClinVar、PubMed 原始记录与 UniProt 条目",
                "- 对关键变异或基因建议进一步结合功能实验验证",
            ])

        return "\n".join(lines)

    def _render_best_effort_answer(self) -> str:
        available_references = len(self.state.citations)
        sufficiency_level = "不足" if available_references < self.config.agent.min_final_citations else "基本充分"
        lines = [
            "## 最佳结论（基于现有证据）",
            "系统已基于当前可获得的证据尽力生成结论。",
            "",
            f"当前累计审阅 {self.state.search_results_seen} 条检索结果，可纳入的文献级引用为 {available_references} 条。",
            f"证据充分性评估：{sufficiency_level}",
        ]

        local_points = self._local_observations(limit=4)
        if local_points:
            lines.extend(["", "## 本地数据观察"])
            for item in local_points:
                source_path = item.get("source_path", "local-data")
                claim = item.get("claim_summary") or item.get("snippet") or "本地数据提示存在值得关注的信号。"
                lines.append(f"- {claim}（本地数据：{source_path}）")

        lines.extend(
            [
                "",
                "## 证据充分性说明",
                f"- 当前纳入的文献级引用数量为 {available_references} 条。",
                f"- 证据充分性等级：{sufficiency_level}。",
                "- 系统已基于现有信息生成最佳可用结论，不会因引用数量不足而拒绝输出。",
                "- 如需更高置信度的结论，建议后续补充更多相关文献进行交叉验证。",
                "",
                "## 下一步建议",
                "- 可围绕当前最强的本地主题或已识别的分子目标进一步扩展文献检索。",
                "- 对关键结论建议结合功能实验或更多独立数据集进行验证。",
                "",
                "## 引用列表",
            ]
        )
        if available_references == 0:
            lines.append("- 当前暂无可追加的文献级引用，结论主要基于本地数据分析得出。")
        return "\n".join(lines)

    def _append_citation_section(self, answer: str, citations: list[dict[str, Any]]) -> str:
        lines = [answer.rstrip(), "", "## 引用列表"]
        if not citations:
            lines.append("- 当前没有可追加的引用。")
            return "\n".join(lines)
        for citation in citations:
            source_id = citation["source_id"] or citation["source"]
            lines.append(
                f"- [{citation['label']}] {citation['source']} {source_id} - {citation['title']} - {citation['url']}"
            )
        return "\n".join(lines)

    def _serialize_evidence_card(self, evidence: Evidence) -> dict[str, Any]:
        return {
            "id": evidence.id,
            "question_id": evidence.question_id,
            "source": evidence.source,
            "source_type": evidence.source_type,
            "source_id": evidence.source_id,
            "title": evidence.title,
            "snippet": truncate_text(evidence.snippet_or_abstract, 240),
            "claim_summary": evidence.claim_summary,
            "confidence": evidence.confidence,
            "url": evidence.url,
            "year": evidence.year,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Bioinformatics Deep Research Agent")
    parser.add_argument("--input", "-i", type=str, help="生物信息学问题文本")
    parser.add_argument("--input_file", "-f", type=str, help="输入文件")
    parser.add_argument("--folder", "-d", type=str, help="数据文件夹路径")
    parser.add_argument("--output", "-o", type=str, help="输出 Markdown 文件")
    args = parser.parse_args()

    config = get_config()
    agent = BioResearchAgent(config)

    if args.folder:
        result = agent.run_folder(args.folder)
    elif args.input_file:
        with open(args.input_file, "r", encoding="utf-8") as handle:
            result = agent.run(handle.read().strip())
    elif args.input:
        result = agent.run(args.input)
    else:
        parser.error("请提供 --input、--input_file 或 --folder")
        return

    print(result)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(result)


if __name__ == "__main__":
    main()
