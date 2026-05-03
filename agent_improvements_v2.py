"""
Agent 系统第二阶段改进模块 - ReAct 反馈循环、事实核查、标准化评估
对标业界标准：OpenAI Deep Research, Perplexity, Jina AI
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


# ============================================================================
# ReAct 反馈循环
# ============================================================================

class ResearchStrategy(Enum):
    """研究策略"""
    CONTINUE = "continue"
    REFINE = "refine"
    BACKTRACK = "backtrack"
    EXPAND = "expand"
    FOCUS = "focus"


@dataclass
class ObservationResult:
    """观察结果"""
    round_quality: float  # 0-1 评分
    evidence_quality: float
    coverage_score: float
    contradiction_count: int
    gap_count: int
    recommended_strategy: ResearchStrategy
    reasoning: str


class ReActObserver:
    """ReAct 观察者 - 评估研究状态并推荐策略调整"""
    
    def observe(self, round_result: dict[str, Any]) -> ObservationResult:
        """观察当前研究轮次并评估质量"""
        # 多维度评估
        evidence_quality = self._evaluate_evidence_quality(round_result)
        coverage_score = self._evaluate_coverage(round_result)
        contradiction_count = round_result.get("contradiction_count", 0)
        gap_count = round_result.get("gap_count", 0)
        
        # 综合质量评分
        round_quality = (
            evidence_quality * 0.4 +
            coverage_score * 0.3 +
            max(0, 1 - contradiction_count * 0.2) * 0.2 +
            max(0, 1 - gap_count * 0.15) * 0.1
        )
        
        # 推荐策略
        strategy = self._recommend_strategy(
            round_quality, evidence_quality, coverage_score,
            contradiction_count, gap_count
        )
        
        return ObservationResult(
            round_quality=round(round_quality, 3),
            evidence_quality=round(evidence_quality, 3),
            coverage_score=round(coverage_score, 3),
            contradiction_count=contradiction_count,
            gap_count=gap_count,
            recommended_strategy=strategy,
            reasoning=self._generate_reasoning(round_quality, strategy)
        )
    
    def _evaluate_evidence_quality(self, round_result: dict[str, Any]) -> float:
        """评估证据质量"""
        evidence_scores = round_result.get("evidence_scores", [])
        if not evidence_scores:
            return 0.0
        return sum(evidence_scores) / len(evidence_scores)
    
    def _evaluate_coverage(self, round_result: dict[str, Any]) -> float:
        """评估覆盖率"""
        questions = round_result.get("questions", [])
        answered = sum(1 for q in questions if q.get("status") == "answered")
        return answered / max(len(questions), 1)
    
    def _recommend_strategy(
        self,
        quality: float,
        evidence_quality: float,
        coverage: float,
        contradictions: int,
        gaps: int
    ) -> ResearchStrategy:
        """推荐研究策略"""
        if quality < 0.4:
            return ResearchStrategy.BACKTRACK
        elif quality < 0.6:
            if evidence_quality < 0.5:
                return ResearchStrategy.EXPAND
            else:
                return ResearchStrategy.REFINE
        elif quality < 0.8:
            if gaps > 2:
                return ResearchStrategy.EXPAND
            elif contradictions > 1:
                return ResearchStrategy.FOCUS
            else:
                return ResearchStrategy.CONTINUE
        else:
            return ResearchStrategy.CONTINUE
    
    def _generate_reasoning(self, quality: float, strategy: ResearchStrategy) -> str:
        """生成策略推荐理由"""
        reasonings = {
            ResearchStrategy.CONTINUE: f"研究质量良好 ({quality:.2f})，继续当前策略",
            ResearchStrategy.REFINE: f"研究质量中等 ({quality:.2f})，需要优化搜索查询",
            ResearchStrategy.BACKTRACK: f"研究质量较低 ({quality:.2f})，需要回溯调整策略",
            ResearchStrategy.EXPAND: "证据不足，需要扩展搜索范围",
            ResearchStrategy.FOCUS: "存在冲突证据，需要聚焦关键问题"
        }
        return reasonings.get(strategy, "未知策略")


# ============================================================================
# 事实核查系统
# ============================================================================

@dataclass
class VerificationResult:
    """验证结果"""
    claim: str
    verified: bool
    confidence: float
    supporting_sources: list[str]
    contradicting_sources: list[str]
    reasoning: str


class FactChecker:
    """事实核查器 - 多源验证关键声明"""
    
    SUPPORT_PATTERNS = [
        r"support[s]?", r"confirm[s]?", r"validat[e]s?", r"demonstrat[e]s?",
        r"show[s]?", r"indicate[s]?", r"evidence", r"consistent with"
    ]
    
    CONTRADICT_PATTERNS = [
        r"contradict[s]?", r"refute[s]?", r"disprove[s]?", r"challenge[s]?",
        r"inconsistent", r"not support", r"no evidence", r"failed to"
    ]
    
    def verify_claim(
        self,
        claim: str,
        evidence_list: list[dict[str, Any]]
    ) -> VerificationResult:
        """验证声明"""
        supporting = []
        contradicting = []
        
        for evidence in evidence_list:
            text = f"{evidence.get('title', '')} {evidence.get('snippet_or_abstract', '')}".lower()
            claim_lower = claim.lower()
            
            # 检查实体匹配
            if self._entities_match(claim, evidence):
                # 检查支持或矛盾
                if self._supports_claim(text, claim_lower):
                    supporting.append(evidence.get("id", ""))
                elif self._contradicts_claim(text, claim_lower):
                    contradicting.append(evidence.get("id", ""))
        
        # 计算可信度
        confidence = self._calculate_confidence(supporting, contradicting, evidence_list)
        
        return VerificationResult(
            claim=claim,
            verified=confidence > 0.6,
            confidence=round(confidence, 3),
            supporting_sources=supporting,
            contradicting_sources=contradicting,
            reasoning=self._generate_verification_reasoning(confidence, supporting, contradicting)
        )
    
    def _entities_match(self, claim: str, evidence: dict[str, Any]) -> bool:
        """检查实体是否匹配"""
        text = f"{evidence.get('title', '')} {evidence.get('snippet_or_abstract', '')}".lower()
        
        # 提取 claim 中的关键实体（简化实现）
        entities = re.findall(r'[A-Z][a-zA-Z0-9]+', claim)
        return any(entity.lower() in text for entity in entities if len(entity) > 2)
    
    def _supports_claim(self, text: str, claim: str) -> bool:
        """检查证据是否支持声明"""
        # 简单关键词匹配
        for pattern in self.SUPPORT_PATTERNS:
            if re.search(pattern, text):
                # 检查是否有共同的关键词
                claim_words = set(claim.split())
                text_words = set(text.split())
                overlap = len(claim_words & text_words)
                if overlap > 2:
                    return True
        return False
    
    def _contradicts_claim(self, text: str, claim: str) -> bool:
        """检查证据是否矛盾声明"""
        for pattern in self.CONTRADICT_PATTERNS:
            if re.search(pattern, text):
                claim_words = set(claim.split())
                text_words = set(text.split())
                overlap = len(claim_words & text_words)
                if overlap > 2:
                    return True
        return False
    
    def _calculate_confidence(
        self,
        supporting: list[str],
        contradicting: list[str],
        all_evidence: list[dict[str, Any]]
    ) -> float:
        """计算可信度"""
        if not supporting and not contradicting:
            return 0.5  # 无证据支持或反对
        
        # 基于来源数量和权威性
        support_score = len(supporting) * 0.3
        contradict_score = len(contradicting) * 0.3
        
        # 权威性加权
        evidence_map = {ev["id"]: ev for ev in all_evidence}
        for source_id in supporting:
            ev = evidence_map.get(source_id)
            if ev:
                authority = {"clinvar": 1.0, "pubmed": 0.8, "uniprot": 0.7, "web": 0.4}.get(
                    ev.get("source_type", ""), 0.5
                )
                support_score *= authority
        
        for source_id in contradicting:
            ev = evidence_map.get(source_id)
            if ev:
                authority = {"clinvar": 1.0, "pubmed": 0.8, "uniprot": 0.7, "web": 0.4}.get(
                    ev.get("source_type", ""), 0.5
                )
                contradict_score *= authority
        
        # 计算净可信度
        net_score = support_score - contradict_score
        confidence = 0.5 + net_score * 0.5
        
        return max(0.0, min(1.0, confidence))
    
    def _generate_verification_reasoning(
        self,
        confidence: float,
        supporting: list[str],
        contradicting: list[str]
    ) -> str:
        """生成验证推理"""
        if confidence > 0.8:
            return f"强支持：{len(supporting)} 个来源支持，{len(contradicting)} 个来源反对"
        elif confidence > 0.6:
            return f"中等支持：{len(supporting)} 个来源支持，{len(contradicting)} 个来源反对"
        elif confidence > 0.4:
            return f"证据不足：{len(supporting)} 个来源支持，{len(contradicting)} 个来源反对"
        else:
            return f"可能矛盾：{len(supporting)} 个来源支持，{len(contradicting)} 个来源反对"


# ============================================================================
# 标准化评估框架 (RACE/FACT)
# ============================================================================

@dataclass
class RACEResult:
    """RACE 评估结果"""
    coverage: float  # 覆盖率
    insight: float  # 洞察力
    instruction_following: float  # 指令遵循
    overall: float  # 总体评分


@dataclass
class FACTResult:
    """FACT 评估结果"""
    factual_abundance: float  # 事实丰富度
    citation_trustworthiness: float  # 引用可信度
    overall: float  # 总体评分


@dataclass
class EvaluationResult:
    """综合评估结果"""
    race: RACEResult
    fact: FACTResult
    citation_accuracy: float
    key_point_recall: float
    overall_score: float


class ResearchEvaluator:
    """研究质量评估器 - 实现 RACE/FACT 评估框架"""
    
    def evaluate(
        self,
        report: str,
        references: list[dict[str, Any]],
        original_question: str,
        evidence_list: list[dict[str, Any]]
    ) -> EvaluationResult:
        """综合评估研究报告"""
        race = self._evaluate_race(report, original_question)
        fact = self._evaluate_fact(report, references, evidence_list)
        citation_accuracy = self._evaluate_citation_accuracy(report, references)
        key_point_recall = self._evaluate_key_point_recall(report, original_question)
        
        # 综合评分
        overall_score = (
            race.overall * 0.35 +
            fact.overall * 0.30 +
            citation_accuracy * 0.20 +
            key_point_recall * 0.15
        )
        
        return EvaluationResult(
            race=race,
            fact=fact,
            citation_accuracy=round(citation_accuracy, 3),
            key_point_recall=round(key_point_recall, 3),
            overall_score=round(overall_score, 3)
        )
    
    def _evaluate_race(self, report: str, question: str) -> RACEResult:
        """RACE 评估"""
        coverage = self._evaluate_coverage(report, question)
        insight = self._evaluate_insight(report)
        instruction_following = self._evaluate_instruction_following(report, question)
        
        overall = coverage * 0.4 + insight * 0.35 + instruction_following * 0.25
        
        return RACEResult(
            coverage=round(coverage, 3),
            insight=round(insight, 3),
            instruction_following=round(instruction_following, 3),
            overall=round(overall, 3)
        )
    
    def _evaluate_coverage(self, report: str, question: str) -> float:
        """评估覆盖率"""
        # 提取问题中的关键概念
        question_concepts = set(re.findall(r'[A-Z][a-zA-Z0-9]+', question))
        question_concepts = {c for c in question_concepts if len(c) > 2}
        
        # 检查报告中是否提及这些概念
        report_lower = report.lower()
        covered = sum(1 for concept in question_concepts if concept.lower() in report_lower)
        
        coverage = covered / max(len(question_concepts), 1)
        return min(coverage * 1.2, 1.0)  # 放宽标准
    
    def _evaluate_insight(self, report: str) -> float:
        """评估洞察力"""
        # 检查分析深度指标
        insight_indicators = [
            r"mechanism", r"pathway", r"correlation", r"causation",
            r"implication", r"significance", r"limitation", r"future"
        ]
        
        count = sum(1 for pattern in insight_indicators if re.search(pattern, report, re.IGNORECASE))
        return min(count / len(insight_indicators) * 1.5, 1.0)
    
    def _evaluate_instruction_following(self, report: str, question: str) -> float:
        """评估指令遵循"""
        score = 1.0
        
        # 检查基本结构
        if "## 简要结论" not in report and "简要结论" not in report:
            score -= 0.2
        if "## 关键证据" not in report and "关键证据" not in report:
            score -= 0.2
        if "引用" not in report:
            score -= 0.2
        
        # 检查长度
        if len(report) < 500:
            score -= 0.2
        
        return max(score, 0.0)
    
    def _evaluate_fact(self, report: str, references: list[dict[str, Any]], evidence_list: list[dict[str, Any]]) -> FACTResult:
        """FACT 评估"""
        factual_abundance = self._evaluate_factual_abundance(report, evidence_list)
        citation_trustworthiness = self._evaluate_citation_trustworthiness(references, evidence_list)
        
        overall = factual_abundance * 0.5 + citation_trustworthiness * 0.5
        
        return FACTResult(
            factual_abundance=round(factual_abundance, 3),
            citation_trustworthiness=round(citation_trustworthiness, 3),
            overall=round(overall, 3)
        )
    
    def _evaluate_factual_abundance(self, report: str, evidence_list: list[dict[str, Any]]) -> float:
        """评估事实丰富度"""
        # 检查报告中引用的事实数量
        fact_count = len(re.findall(r'\[\d+\]', report))
        expected_facts = min(len(evidence_list), 10)
        
        abundance = fact_count / max(expected_facts, 1)
        return min(abundance, 1.0)
    
    def _evaluate_citation_trustworthiness(self, references: list[dict[str, Any]], evidence_list: list[dict[str, Any]]) -> float:
        """评估引用可信度"""
        if not references:
            return 0.0
        
        # 检查引用来源的权威性
        authority_scores = {
            "clinvar": 0.95,
            "pubmed": 0.85,
            "uniprot": 0.80,
            "web": 0.50,
            "local": 0.70,
        }
        
        total_score = 0.0
        for ref in references:
            source_type = ref.get("source_type", "web")
            total_score += authority_scores.get(source_type, 0.40)
        
        return total_score / max(len(references), 1)
    
    def _evaluate_citation_accuracy(self, report: str, references: list[dict[str, Any]]) -> float:
        """评估引用准确性"""
        if not references:
            return 0.0
        
        # 检查引用是否在报告中被正确引用
        citation_count = len(re.findall(r'\[\d+\]', report))
        if citation_count == 0:
            return 0.0
        
        # 简化评估：检查引用数量是否合理
        expected_citations = min(len(references), 15)
        accuracy = min(citation_count, expected_citations) / expected_citations
        
        return accuracy
    
    def _evaluate_key_point_recall(self, report: str, question: str) -> float:
        """评估关键点召回率"""
        # 提取问题中的关键点
        key_points = self._extract_key_points(question)
        
        # 检查报告中是否覆盖这些关键点
        report_lower = report.lower()
        recalled = sum(1 for point in key_points if point.lower() in report_lower)
        
        return recalled / max(len(key_points), 1)
    
    def _extract_key_points(self, question: str) -> list[str]:
        """提取关键点"""
        # 简化实现：提取名词短语
        patterns = [
            r'([A-Z][a-zA-Z0-9]+)',  # 基因/蛋白名
            r'([\u4e00-\u9fa5]{2,})',  # 中文关键词
        ]
        
        key_points = []
        for pattern in patterns:
            key_points.extend(re.findall(pattern, question))
        
        return list(set(key_points))[:10]
