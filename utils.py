"""
Utility helpers for the Bio Deep Research agent.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, Iterable, Optional


logger = logging.getLogger(__name__)


def format_timestamp() -> str:
    """Return a human-readable timestamp."""

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def truncate_text(text: str, limit: int) -> str:
    """Truncate text for display."""

    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)] + "..."


def dedupe_preserve_order(items: Iterable[str]) -> list[str]:
    """De-duplicate strings while preserving order."""

    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        value = (item or "").strip()
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(value)
    return output


def safe_json_parse(text: str) -> Optional[Any]:
    """Best-effort JSON parsing for model responses."""

    if not text:
        return None

    candidates = [text.strip()]

    code_block = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text, re.IGNORECASE)
    if code_block:
        candidates.append(code_block.group(1).strip())

    object_match = re.search(r"(\{[\s\S]*\})", text)
    if object_match:
        candidates.append(object_match.group(1).strip())

    array_match = re.search(r"(\[[\s\S]*\])", text)
    if array_match:
        candidates.append(array_match.group(1).strip())

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    return None


class EntityParser:
    """Lightweight heuristic parser for common bioinformatics entities."""

    GENE_PATTERN = re.compile(r"\b([A-Z][A-Z0-9-]{1,11})\b")
    RS_PATTERN = re.compile(r"\brs\d+\b", re.IGNORECASE)
    GO_PATTERN = re.compile(r"\bGO:\d+\b", re.IGNORECASE)
    KEGG_PATTERN = re.compile(r"\bhsa\d+\b", re.IGNORECASE)
    REACTOME_PATTERN = re.compile(r"\bR-[A-Z]{2}-\d+\b", re.IGNORECASE)

    VARIANT_PATTERNS = [
        re.compile(r"\bc\.\d+(?:_\d+)?(?:del|dup|ins|delins)[A-Za-z0-9]*\b", re.IGNORECASE),
        re.compile(r"\bc\.\d+[A-Za-z]>\w\b", re.IGNORECASE),
        re.compile(r"\bp\.[A-Z][a-z]{2}\d+[A-Z][a-z]{2}(?:fs\*?\d+|Ter\d+|=|\?)?\b"),
        re.compile(r"\bp\.[A-Z][a-z]{2}\d+(?:del|dup|ins)[A-Za-z0-9]*\b"),
        re.compile(r"\bchr(?:[0-9XYMT]+):\d+(?:-\d+)?\b", re.IGNORECASE),
    ]

    EXPRESSION_PATTERNS = [
        re.compile(r"\b(?:FPKM|TPM|CPM|RPKM)\s*[><=]*\s*-?\d+(?:\.\d+)?\b", re.IGNORECASE),
        re.compile(r"\bfold\s*change\s*[><=]*\s*-?\d+(?:\.\d+)?\b", re.IGNORECASE),
        re.compile(r"\blog2FC\s*[><=]*\s*-?\d+(?:\.\d+)?\b", re.IGNORECASE),
    ]

    SAMPLE_PATTERNS = [
        re.compile(r"\b(组织|tissue|肿瘤|tumou?r|癌组织)\b", re.IGNORECASE),
        re.compile(r"\b(血液|blood|血浆|plasma|血清|serum)\b", re.IGNORECASE),
        re.compile(r"\b(细胞系|cell line|细胞|cell)\b", re.IGNORECASE),
        re.compile(r"\b(尿液|urine)\b", re.IGNORECASE),
        re.compile(r"\b(脑脊液|csf)\b", re.IGNORECASE),
    ]

    SPECIES_PATTERNS = [
        ("human", re.compile(r"\b(human|homo sapiens)\b", re.IGNORECASE)),
        ("mouse", re.compile(r"\b(mouse|mus musculus)\b", re.IGNORECASE)),
        ("rat", re.compile(r"\b(rat|rattus norvegicus)\b", re.IGNORECASE)),
        ("zebrafish", re.compile(r"\b(zebrafish|danio rerio)\b", re.IGNORECASE)),
        ("drosophila melanogaster", re.compile(r"\b(drosophila melanogaster)\b", re.IGNORECASE)),
        ("caenorhabditis elegans", re.compile(r"\b(c\.?\s*elegans|caenorhabditis elegans)\b", re.IGNORECASE)),
    ]

    DISEASE_PATTERNS = [
        re.compile(r"(乳腺癌|卵巢癌|肺癌|肺腺癌|肺鳞癌|肝癌|胃癌|结直肠癌|胰腺癌|前列腺癌|黑色素瘤|白血病|淋巴瘤|多发性骨髓瘤)"),
        re.compile(r"(肾癌|肾肿瘤|肾细胞癌)"),
        re.compile(r"\b(breast cancer|ovarian cancer|lung cancer|colorectal cancer|pancreatic cancer|prostate cancer|melanoma|leukemia|lymphoma)\b", re.IGNORECASE),
        re.compile(r"\b(kidney cancer|renal cancer|renal cell carcinoma|kidney tumor|renal tumor)\b", re.IGNORECASE),
    ]

    SPECIAL_GENE_ALIASES = [
        (re.compile(r"\bp53\b", re.IGNORECASE), "TP53"),
    ]

    FOCUS_PATTERNS = [
        (re.compile(r"\bp53\b|\bTP53\b|p53[_\s-]*signaling", re.IGNORECASE), "p53 signaling"),
        (re.compile(r"\bapoptosis\b|细胞凋亡", re.IGNORECASE), "apoptosis"),
        (re.compile(r"\bcell[_\s-]*cycle\b|细胞周期", re.IGNORECASE), "cell cycle"),
        (re.compile(r"\bpseudotime\b|拟时序|slingshot", re.IGNORECASE), "pseudotime"),
        (re.compile(r"\bsingle[-\s]*cell\b|单细胞|metacell", re.IGNORECASE), "single-cell"),
        (re.compile(r"\bATAC[-\s]*seq\b|chromatin accessibility|染色质可及性", re.IGNORECASE), "chromatin accessibility"),
        (re.compile(r"\bRNA[-\s]*seq\b|transcriptome|转录组", re.IGNORECASE), "transcriptome"),
        (re.compile(r"\btumou?r\b|肿瘤", re.IGNORECASE), "tumor biology"),
        (re.compile(r"\bDNA repair\b|DNA损伤修复|dna damage", re.IGNORECASE), "DNA repair"),
        (re.compile(r"\btrajectory\b|轨迹", re.IGNORECASE), "trajectory inference"),
    ]

    CONTEXT_PATTERNS = [
        (re.compile(r"\bkidney\b|\brenal\b|肾", re.IGNORECASE), "kidney"),
        (re.compile(r"\btumou?r\b|肿瘤", re.IGNORECASE), "tumor"),
        (re.compile(r"\bpodocyte", re.IGNORECASE), "podocyte"),
        (re.compile(r"\bmyeloid", re.IGNORECASE), "myeloid"),
        (re.compile(r"\bendothelial", re.IGNORECASE), "endothelial"),
        (re.compile(r"\bepithelial", re.IGNORECASE), "epithelial"),
        (re.compile(r"\bstressed tumor", re.IGNORECASE), "stressed tumor"),
    ]

    GENE_STOPWORDS = {
        "DNA",
        "RNA",
        "RNASEQ",
        "PCR",
        "FPKM",
        "TPM",
        "CPM",
        "RPKM",
        "GO",
        "KEGG",
        "NGS",
        "WGS",
        "WES",
        "SNP",
        "CNV",
        "SV",
        "PMID",
        "JSON",
        "CSV",
        "H5AD",
        "ATAC",
        "CELL",
        "DS",
    }

    @classmethod
    def parse(cls, text: str) -> dict[str, Any]:
        """Parse common biological entities from free text."""

        return {
            "genes": cls._extract_genes(text),
            "variants": cls._extract_variants(text),
            "variant_aliases": cls._extract_variant_aliases(text),
            "expression_values": cls._extract_expressions(text),
            "pathways": cls._extract_pathways(text),
            "diseases": cls._extract_diseases(text),
            "species": cls._extract_species(text),
            "sample_type": cls._extract_sample_type(text),
            "research_focus": cls._extract_focus_terms(text),
            "other_entities": cls._extract_context_entities(text),
        }

    @classmethod
    def _extract_genes(cls, text: str) -> list[str]:
        genes: list[str] = []
        for match in cls.GENE_PATTERN.finditer(text):
            token = match.group(1).upper()
            if token in cls.GENE_STOPWORDS:
                continue
            if len(token) < 2 or len(token) > 12:
                continue
            if token.startswith("CHR"):
                continue
            genes.append(token)
        for pattern, alias in cls.SPECIAL_GENE_ALIASES:
            if pattern.search(text):
                genes.append(alias)
        return dedupe_preserve_order(genes)

    @classmethod
    def _extract_variants(cls, text: str) -> list[str]:
        variants: list[str] = []
        for pattern in cls.VARIANT_PATTERNS:
            for match in pattern.finditer(text):
                variants.append(match.group(0))
        variants.extend(cls._extract_variant_aliases(text))
        return dedupe_preserve_order(variants)

    @classmethod
    def _extract_variant_aliases(cls, text: str) -> list[str]:
        return dedupe_preserve_order(match.group(0) for match in cls.RS_PATTERN.finditer(text))

    @classmethod
    def _extract_expressions(cls, text: str) -> list[str]:
        expressions: list[str] = []
        for pattern in cls.EXPRESSION_PATTERNS:
            expressions.extend(match.group(0) for match in pattern.finditer(text))
        return dedupe_preserve_order(expressions)

    @classmethod
    def _extract_pathways(cls, text: str) -> list[str]:
        matches: list[str] = []
        matches.extend(match.group(0) for match in cls.GO_PATTERN.finditer(text))
        matches.extend(match.group(0) for match in cls.KEGG_PATTERN.finditer(text))
        matches.extend(match.group(0) for match in cls.REACTOME_PATTERN.finditer(text))
        return dedupe_preserve_order(matches)

    @classmethod
    def _extract_focus_terms(cls, text: str) -> list[str]:
        focus: list[str] = []
        for pattern, label in cls.FOCUS_PATTERNS:
            if pattern.search(text):
                focus.append(label)
        return dedupe_preserve_order(focus)

    @classmethod
    def _extract_diseases(cls, text: str) -> list[str]:
        diseases: list[str] = []
        for pattern in cls.DISEASE_PATTERNS:
            diseases.extend(match.group(0) for match in pattern.finditer(text))
        return dedupe_preserve_order(diseases)

    @classmethod
    def _extract_species(cls, text: str) -> list[str]:
        species: list[str] = []
        for label, pattern in cls.SPECIES_PATTERNS:
            if pattern.search(text):
                species.append(label)
        return dedupe_preserve_order(species)

    @classmethod
    def _extract_context_entities(cls, text: str) -> list[str]:
        context: list[str] = []
        for pattern, label in cls.CONTEXT_PATTERNS:
            if pattern.search(text):
                context.append(label)
        return dedupe_preserve_order(context)

    @classmethod
    def _extract_sample_type(cls, text: str) -> Optional[str]:
        for pattern in cls.SAMPLE_PATTERNS:
            match = pattern.search(text)
            if match:
                return match.group(1)
        return None
