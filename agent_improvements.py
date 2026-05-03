"""
Agent 系统改进模块 - 包含检索优化、冲突检测和记忆管理
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from search_tool import SearchResult


@dataclass
class QueryExpansion:
    """查询扩展记录"""
    original_query: str
    expanded_queries: list[str]
    expansion_type: str  # "synonym", "abbreviation", "related_concept"
    relevance_score: float = 0.0


@dataclass
class Conflict:
    """证据冲突记录"""
    id: str
    evidence_ids: list[str]
    conflict_type: str  # "contradictory_claims", "different_methods", "population_difference", "temporal_change"
    severity: str  # "minor", "moderate", "severe"
    description: str
    resolution: Optional[str] = None
    detected_at: str = ""


@dataclass
class MemoryItem:
    """记忆项"""
    id: str
    type: str  # "fact", "assumption", "finding", "contradiction", "entity"
    content: str
    source_ids: list[str]
    confidence: float
    created_at: str
    importance: float = 0.5
    access_count: int = 0


class QueryExpander:
    """查询扩展器 - 生成多个相关查询变体"""
    
    SYNONYM_MAP = {
        "breast cancer": ["breast carcinoma", "breast neoplasm", "mammary cancer"],
        "lung cancer": ["lung carcinoma", "pulmonary neoplasm"],
        "kidney cancer": ["renal cell carcinoma", "kidney neoplasm", "renal cancer"],
        "mutation": ["variant", "polymorphism", "alteration"],
        "function": ["role", "activity", "mechanism"],
        "clinical significance": ["clinical relevance", "prognostic value", "predictive value"],
        "pathogenic": ["disease-causing", "deleterious", "damaging"],
        "benign": ["non-pathogenic", "harmless", "neutral"],
    }
    
    ABBREVIATION_MAP = {
        "breast cancer": ["BC"],
        "lung cancer": ["LC", "NSCLC", "SCLC"],
        "kidney cancer": ["RCC", "CCRCC"],
        "human": ["Homo sapiens"],
        "mouse": ["Mus musculus"],
        "single-cell": ["scRNA-seq", "single cell"],
        "chromatin accessibility": ["ATAC-seq"],
    }
    
    def expand(self, question_text: str, question_type: str, entities: dict[str, Any]) -> list[QueryExpansion]:
        """为问题生成查询扩展"""
        expansions = []
        
        # 1. 同义词扩展
        synonym_queries = self._expand_synonyms(question_text)
        if synonym_queries:
            expansions.append(QueryExpansion(
                original_query=question_text,
                expanded_queries=synonym_queries,
                expansion_type="synonym",
                relevance_score=0.9
            ))
        
        # 2. 缩写扩展
        abbr_queries = self._expand_abbreviations(question_text, entities)
        if abbr_queries:
            expansions.append(QueryExpansion(
                original_query=question_text,
                expanded_queries=abbr_queries,
                expansion_type="abbreviation",
                relevance_score=0.85
            ))
        
        # 3. 概念扩展
        concept_queries = self._expand_concepts(question_text, question_type, entities)
        if concept_queries:
            expansions.append(QueryExpansion(
                original_query=question_text,
                expanded_queries=concept_queries,
                expansion_type="related_concept",
                relevance_score=0.75
            ))
        
        return expansions
    
    def _expand_synonyms(self, query: str) -> list[str]:
        """同义词扩展"""
        expanded = []
        query_lower = query.lower()
        
        for term, synonyms in self.SYNONYM_MAP.items():
            if term in query_lower:
                for synonym in synonyms:
                    expanded_query = query_lower.replace(term, synonym)
                    expanded.append(expanded_query)
        
        return expanded[:3]
    
    def _expand_abbreviations(self, query: str, entities: dict[str, Any]) -> list[str]:
        """缩写扩展"""
        expanded = []
        query_lower = query.lower()
        
        # 扩展疾病缩写
        for term, abbrs in self.ABBREVIATION_MAP.items():
            if term in query_lower:
                for abbr in abbrs:
                    expanded_query = query_lower.replace(term, abbr)
                    expanded.append(expanded_query)
        
        # 扩展基因名
        genes = entities.get("genes", [])
        for gene in genes[:3]:
            gene_lower = gene.lower()
            if gene_lower in query_lower:
                # 添加基因别名
                aliases = entities.get("variant_aliases", [])
                for alias in aliases[:2]:
                    if gene_lower in alias.lower():
                        expanded.append(query_lower.replace(gene_lower, alias.lower()))
        
        return expanded[:3]
    
    def _expand_concepts(self, question_text: str, question_type: str, entities: dict[str, Any]) -> list[str]:
        """相关概念扩展"""
        expanded = []
        
        # 根据问题类型添加相关概念
        if question_type == "mechanism":
            expanded.append(f"{question_text} pathway signaling")
            expanded.append(f"{question_text} molecular mechanism")
        elif question_type == "disease":
            expanded.append(f"{question_text} clinical trial")
            expanded.append(f"{question_text} patient outcome")
        elif question_type == "functional":
            expanded.append(f"{question_text} protein domain structure")
            expanded.append(f"{question_text} functional assay")
        
        return expanded[:2]


class EvidenceScorer:
    """证据评分器 - 多维度证据质量评估"""
    
    def score_evidence(
        self, 
        evidence_dict: dict[str, Any], 
        question_dict: dict[str, Any],
        entities: dict[str, Any]
    ) -> dict[str, float]:
        """多维度证据评分"""
        scores = {
            "relevance": self._score_relevance(evidence_dict, question_dict, entities),
            "authority": self._score_authority(evidence_dict),
            "recency": self._score_recency(evidence_dict),
            "specificity": self._score_specificity(evidence_dict, question_dict),
            "consistency": self._score_consistency(evidence_dict),
        }
        
        # 加权总分
        weights = {
            "relevance": 0.35,
            "authority": 0.25,
            "recency": 0.15,
            "specificity": 0.15,
            "consistency": 0.10,
        }
        
        total_score = sum(scores[key] * weights[key] for key in scores)
        scores["total"] = round(total_score, 3)
        
        return scores
    
    def _score_relevance(self, evidence: dict[str, Any], question: dict[str, Any], entities: dict[str, Any]) -> float:
        """相关性评分"""
        text = f"{evidence.get('title', '')} {evidence.get('snippet_or_abstract', '')}".lower()
        score = 0.0
        
        # 基因匹配
        genes = [g.lower() for g in entities.get("genes", [])]
        if any(gene in text for gene in genes):
            score += 0.4
        
        # 变异匹配
        variants = [v.lower() for v in entities.get("variants", []) + entities.get("variant_aliases", [])]
        if any(variant in text for variant in variants):
            score += 0.3
        
        # 疾病匹配
        diseases = [d.lower() for d in entities.get("diseases", [])]
        if any(disease in text for disease in diseases):
            score += 0.2
        
        # 研究焦点匹配
        focus = entities.get("research_focus", [])
        if any(f in text.lower() for f in focus):
            score += 0.1
        
        return min(score, 1.0)
    
    def _score_authority(self, evidence: dict[str, Any]) -> float:
        """权威性评分"""
        authority_scores = {
            "clinvar": 0.95,
            "pubmed": 0.85,
            "uniprot": 0.80,
            "web": 0.50,
            "local": 0.70,
        }
        
        base_score = authority_scores.get(evidence.get("source_type", ""), 0.40)
        
        # ClinVar 专家评审加分
        if evidence.get("source_type") == "clinvar":
            review_status = evidence.get("metadata", {}).get("review_status", "").lower()
            if "expert panel" in review_status:
                base_score += 0.05
            if "practice guideline" in review_status:
                base_score += 0.08
        
        # PubMed 高影响期刊加分（简化判断）
        if evidence.get("source_type") == "pubmed":
            journal = evidence.get("metadata", {}).get("journal", "").lower()
            high_impact_journals = ["nature", "science", "cell", "nejm", "lancet"]
            if any(j in journal for j in high_impact_journals):
                base_score += 0.05
        
        return min(base_score, 1.0)
    
    def _score_recency(self, evidence: dict[str, Any]) -> float:
        """时效性评分"""
        year = evidence.get("year", "")
        if not year or not year.isdigit():
            return 0.5  # 未知年份给中等分数
        
        try:
            year_int = int(year[:4])
            current_year = datetime.now().year
            age = current_year - year_int
            
            if age <= 2:
                return 0.95
            elif age <= 5:
                return 0.85
            elif age <= 10:
                return 0.70
            else:
                return 0.50
        except ValueError:
            return 0.5
    
    def _score_specificity(self, evidence: dict[str, Any], question: dict[str, Any]) -> float:
        """特异性评分 - 证据与问题的匹配程度"""
        text = f"{evidence.get('title', '')} {evidence.get('snippet_or_abstract', '')}".lower()
        question_text = question.get("text", "")
        question_words = set(question_text.lower().split())
        
        # 计算问题关键词在证据中的覆盖率
        matched_words = sum(1 for word in question_words if word in text and len(word) > 3)
        coverage = matched_words / max(len(question_words), 1)
        
        return min(coverage * 1.5, 1.0)
    
    def _score_consistency(self, evidence: dict[str, Any]) -> float:
        """一致性评分 - 与其他证据的一致性"""
        # 简化实现：基于置信度
        return evidence.get("confidence", 0.5)


class ConflictDetector:
    """冲突检测器 - 检测证据间的矛盾"""
    
    CONTRADICTORY_PATTERNS = [
        (r"pathogenic", r"benign"),
        (r"disease-causing", r"harmless"),
        (r"increased risk", r"decreased risk"),
        (r"upregulated", r"downregulated"),
        (r"activates", r"inhibits"),
        (r"promotes", r"suppresses"),
        (r"associated with", r"not associated with"),
        (r"significant", r"not significant"),
    ]
    
    def detect_conflicts(self, evidence_list: list[dict[str, Any]]) -> list[Conflict]:
        """检测证据列表中的冲突"""
        conflicts = []
        
        for i, ev1 in enumerate(evidence_list):
            for j, ev2 in enumerate(evidence_list[i+1:], i+1):
                conflict = self._check_pairwise_conflict(ev1, ev2)
                if conflict:
                    conflicts.append(conflict)
        
        return conflicts
    
    def _check_pairwise_conflict(self, ev1: dict[str, Any], ev2: dict[str, Any]) -> Optional[Conflict]:
        """检查两个证据之间是否存在冲突"""
        text1 = f"{ev1.get('title', '')} {ev1.get('snippet_or_abstract', '')}".lower()
        text2 = f"{ev2.get('title', '')} {ev2.get('snippet_or_abstract', '')}".lower()
        
        for pattern1, pattern2 in self.CONTRADICTORY_PATTERNS:
            if re.search(pattern1, text1) and re.search(pattern2, text2):
                return Conflict(
                    id=hashlib.sha1(f"{ev1.get('id', '')}|{ev2.get('id', '')}|{pattern1}".encode()).hexdigest()[:12],
                    evidence_ids=[ev1.get("id", ""), ev2.get("id", "")],
                    conflict_type="contradictory_claims",
                    severity="severe",
                    description=f"证据 {ev1.get('id', '')} 和 {ev2.get('id', '')} 在 {pattern1}/{pattern2} 上存在矛盾",
                    detected_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                )
            
            if re.search(pattern2, text1) and re.search(pattern1, text2):
                return Conflict(
                    id=hashlib.sha1(f"{ev1.get('id', '')}|{ev2.get('id', '')}|{pattern2}".encode()).hexdigest()[:12],
                    evidence_ids=[ev1.get("id", ""), ev2.get("id", "")],
                    conflict_type="contradictory_claims",
                    severity="severe",
                    description=f"证据 {ev1.get('id', '')} 和 {ev2.get('id', '')} 在 {pattern2}/{pattern1} 上存在矛盾",
                    detected_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                )
        
        return None
    
    def resolve_conflict(self, conflict: Conflict, evidence_map: dict[str, dict[str, Any]]) -> str:
        """尝试解决冲突"""
        ev1 = evidence_map.get(conflict.evidence_ids[0])
        ev2 = evidence_map.get(conflict.evidence_ids[1])
        
        if not ev1 or not ev2:
            return "无法解决：证据缺失"
        
        # 基于权威性解决
        auth1 = {"clinvar": 3, "pubmed": 2, "uniprot": 2, "web": 1}.get(ev1.get("source_type", ""), 1)
        auth2 = {"clinvar": 3, "pubmed": 2, "uniprot": 2, "web": 1}.get(ev2.get("source_type", ""), 1)
        
        if auth1 > auth2:
            return f"优先采纳 {ev1.get('source_type', '')} 来源的证据 {ev1.get('id', '')}"
        elif auth2 > auth1:
            return f"优先采纳 {ev2.get('source_type', '')} 来源的证据 {ev2.get('id', '')}"
        else:
            # 基于时效性
            year1 = int(ev1.get("year", "0")[:4]) if ev1.get("year", "0") and ev1.get("year", "0").isdigit() else 0
            year2 = int(ev2.get("year", "0")[:4]) if ev2.get("year", "0") and ev2.get("year", "0").isdigit() else 0
            
            if year1 > year2:
                return f"优先采纳更新的证据 {ev1.get('id', '')} ({ev1.get('year', '')})"
            elif year2 > year1:
                return f"优先采纳更新的证据 {ev2.get('id', '')} ({ev2.get('year', '')})"
            else:
                return "冲突无法自动解决，需要人工判断"


class MemoryManager:
    """记忆管理器 - 管理 Agent 的短期和长期记忆"""
    
    def __init__(self, max_short_term: int = 20, max_long_term: int = 100):
        self.short_term: list[MemoryItem] = []
        self.long_term: list[MemoryItem] = []
        self.max_short_term = max_short_term
        self.max_long_term = max_long_term
    
    def add(self, item: MemoryItem) -> None:
        """添加记忆项"""
        self.short_term.append(item)
        
        # 保持短期记忆大小限制
        if len(self.short_term) > self.max_short_term:
            # 将最重要的项转移到长期记忆
            self._consolidate_to_long_term()
    
    def retrieve_relevant(self, query: str, top_k: int = 5) -> list[MemoryItem]:
        """检索相关记忆"""
        all_memories = self.short_term + self.long_term
        query_lower = query.lower()
        
        # 简单关键词匹配
        scored_memories = []
        for memory in all_memories:
            score = self._memory_relevance_score(memory, query_lower)
            if score > 0.3:
                scored_memories.append((score, memory))
        
        # 按分数排序并返回 top_k
        scored_memories.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in scored_memories[:top_k]]
    
    def _consolidate_to_long_term(self) -> None:
        """将重要记忆转移到长期记忆"""
        # 选择重要性最高的项
        important_items = [m for m in self.short_term if m.importance > 0.7]
        important_items.sort(key=lambda m: m.importance, reverse=True)
        
        for item in important_items[:5]:
            if len(self.long_term) < self.max_long_term:
                self.long_term.append(item)
        
        # 保留最近的短期记忆
        self.short_term = self.short_term[-10:]
    
    def _memory_relevance_score(self, memory: MemoryItem, query: str) -> float:
        """计算记忆与查询的相关性"""
        score = 0.0
        
        # 内容匹配
        if any(word in memory.content.lower() for word in query.split()):
            score += 0.4
        
        # 类型匹配
        if memory.type in query:
            score += 0.2
        
        # 重要性和访问频率
        score += memory.importance * 0.2
        score += min(memory.access_count * 0.05, 0.2)
        
        return min(score, 1.0)
