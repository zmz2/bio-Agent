"""
实际运行示例：展示改进后的 Agent 系统
"""
from agent import BioResearchAgent
from config import Config
import json

def run_example():
    """运行一个完整的研究示例"""
    print("=" * 80)
    print("Agent 系统改进示例")
    print("=" * 80)
    
    # 创建 Agent
    config = Config()
    agent = BioResearchAgent(config)
    
    # 研究问题
    question = "BRCA1 c.185delAG 在乳腺癌中的临床意义是什么？"
    
    print(f"\n研究问题: {question}")
    print("\n开始研究流程...\n")
    
    # 运行研究（不使用 LLM，只展示改进功能）
    result = agent.run(question)
    
    # 展示改进功能的效果
    print("\n" + "=" * 80)
    print("改进功能效果展示")
    print("=" * 80)
    
    # 1. 查询扩展
    print("\n1. 查询扩展记录:")
    if agent.state.query_expansions:
        for i, exp in enumerate(agent.state.query_expansions[:3], 1):
            print(f"\n   扩展 {i}:")
            print(f"   类型: {exp['type']}")
            print(f"   原始: {exp['original'][:50]}...")
            print(f"   扩展: {', '.join(exp['expanded'][:2])}")
    else:
        print("   无查询扩展记录")
    
    # 2. 证据评分
    print("\n2. 证据评分示例:")
    if agent.state.evidence_scores:
        sample_id = list(agent.state.evidence_scores.keys())[0]
        scores = agent.state.evidence_scores[sample_id]
        print(f"\n   证据ID: {sample_id}")
        print(f"   多维度评分:")
        for dim, score in scores.items():
            bar = "█" * int(score * 20)
            print(f"     {dim:12s}: {score:.3f} {bar}")
    else:
        print("   无证据评分记录")
    
    # 3. 冲突检测
    print("\n3. 冲突检测:")
    if agent.state.conflicts:
        for conflict in agent.state.conflicts[:2]:
            print(f"\n   冲突: {conflict.description}")
            print(f"   严重程度: {conflict.severity}")
            print(f"   解决方案: {conflict.resolution or '待解决'}")
    else:
        print("   未检测到冲突")
    
    # 4. 研究统计
    print("\n4. 研究统计:")
    print(f"   审阅结果数: {agent.state.search_results_seen}")
    print(f"   证据数量: {len(agent.state.evidence_by_id)}")
    print(f"   引用数量: {len(agent.state.citations)}")
    print(f"   研究轮次: {len(agent.state.rounds)}")
    
    print("\n" + "=" * 80)
    print("示例完成！")
    print("=" * 80)
    
    return result

if __name__ == "__main__":
    result = run_example()
