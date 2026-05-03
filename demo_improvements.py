"""
Agent 系统改进演示脚本
展示改进后的功能：查询扩展、证据评分、冲突检测
"""
from agent import BioResearchAgent
from config import Config
from agent_improvements import QueryExpander, EvidenceScorer, ConflictDetector

def demo_query_expansion():
    """演示查询扩展功能"""
    print("=" * 80)
    print("查询扩展功能演示")
    print("=" * 80)
    
    expander = QueryExpander()
    
    # 示例问题
    question_text = "BRCA1 在乳腺癌中的临床意义是什么？"
    entities = {
        "genes": ["BRCA1"],
        "variants": ["c.185delAG"],
        "diseases": ["乳腺癌"],
        "research_focus": ["clinical relevance"]
    }
    
    expansions = expander.expand(question_text, "disease", entities)
    
    print(f"\n原始问题: {question_text}")
    print(f"\n扩展结果:")
    for exp in expansions:
        print(f"\n  类型: {exp.expansion_type}")
        print(f"  相关性: {exp.relevance_score}")
        print(f"  扩展查询:")
        for i, query in enumerate(exp.expanded_queries, 1):
            print(f"    {i}. {query}")

def demo_evidence_scoring():
    """演示证据评分功能"""
    print("\n" + "=" * 80)
    print("证据评分功能演示")
    print("=" * 80)
    
    scorer = EvidenceScorer()
    
    # 示例证据
    evidence = {
        "id": "test_evidence_1",
        "title": "BRCA1 mutations in breast cancer risk",
        "snippet_or_abstract": "BRCA1 mutations significantly increase breast cancer risk. Pathogenic variants show high penetrance.",
        "source_type": "pubmed",
        "year": "2023",
        "confidence": 0.85,
        "metadata": {"journal": "Nature Genetics"}
    }
    
    question = {
        "id": "Q1",
        "text": "BRCA1 在乳腺癌中的临床意义是什么？",
        "type": "disease"
    }
    
    entities = {
        "genes": ["BRCA1"],
        "variants": ["c.185delAG"],
        "diseases": ["乳腺癌"],
        "research_focus": ["clinical relevance"]
    }
    
    scores = scorer.score_evidence(evidence, question, entities)
    
    print(f"\n证据: {evidence['title']}")
    print(f"问题: {question['text']}")
    print(f"\n多维度评分:")
    for dimension, score in scores.items():
        print(f"  {dimension}: {score:.3f}")

def demo_conflict_detection():
    """演示冲突检测功能"""
    print("\n" + "=" * 80)
    print("冲突检测功能演示")
    print("=" * 80)
    
    detector = ConflictDetector()
    
    # 示例冲突证据
    evidence_list = [
        {
            "id": "ev1",
            "title": "BRCA1 c.185delAG is pathogenic",
            "snippet_or_abstract": "This variant is classified as pathogenic with high penetrance for breast cancer.",
            "source_type": "clinvar",
            "year": "2022",
            "confidence": 0.95,
            "metadata": {}
        },
        {
            "id": "ev2",
            "title": "BRCA1 c.185delAG shows benign characteristics",
            "snippet_or_abstract": "Recent studies suggest this variant may be benign in certain populations.",
            "source_type": "pubmed",
            "year": "2023",
            "confidence": 0.70,
            "metadata": {}
        }
    ]
    
    conflicts = detector.detect_conflicts(evidence_list)
    
    print(f"\n检测到 {len(conflicts)} 个冲突:")
    for conflict in conflicts:
        print(f"\n  冲突ID: {conflict.id}")
        print(f"  类型: {conflict.conflict_type}")
        print(f"  严重程度: {conflict.severity}")
        print(f"  描述: {conflict.description}")
        
        # 尝试解决冲突
        evidence_map = {ev["id"]: ev for ev in evidence_list}
        resolution = detector.resolve_conflict(conflict, evidence_map)
        print(f"  解决方案: {resolution}")

def demo_agent_integration():
    """演示 Agent 集成效果"""
    print("\n" + "=" * 80)
    print("Agent 集成演示")
    print("=" * 80)
    
    config = Config()
    agent = BioResearchAgent(config)
    
    print("\nAgent 已初始化，具备以下改进功能:")
    print("  ✓ 查询扩展器 - 生成同义词、缩写和相关概念扩展")
    print("  ✓ 证据评分器 - 多维度证据质量评估")
    print("  ✓ 冲突检测器 - 自动检测证据间矛盾")
    print("  ✓ 记忆管理器 - 短期和长期记忆管理")
    
    print("\n改进效果:")
    print("  • 检索覆盖率提升 - 通过查询扩展获取更多相关证据")
    print("  • 证据质量提升 - 通过多维度评分筛选高质量证据")
    print("  • 冲突识别提升 - 自动检测并尝试解决证据冲突")
    print("  • 可解释性提升 - 记录完整的推理和决策过程")

if __name__ == "__main__":
    demo_query_expansion()
    demo_evidence_scoring()
    demo_conflict_detection()
    demo_agent_integration()
    
    print("\n" + "=" * 80)
    print("演示完成！")
    print("=" * 80)
