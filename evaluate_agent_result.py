"""
评估 Agent 对文件夹的分析结果质量
"""
from agent import BioResearchAgent
from config import Config

def run_agent_on_folder(folder_path):
    """让 Agent 自己探索文件夹并生成结论"""
    config = Config()
    agent = BioResearchAgent(config)
    
    print(f"Agent 正在探索文件夹: {folder_path}")
    print("=" * 80)
    
    result = agent.run_folder(folder_path)
    
    print("\n" + "=" * 80)
    print("Agent 生成的研究结论:")
    print("=" * 80)
    print(result)
    
    return result

def evaluate_result_quality(result):
    """评估结果质量"""
    scores = {
        'has_conclusion': 0,
        'has_evidence': 0,
        'has_interpretation': 0,
        'has_clinical_significance': 0,
        'has_citations': 0,
        'has_local_data_analysis': 0,
        'has_mechanism_explanation': 0,
    }
    
    # 检查是否有简要结论
    if '## 简要结论' in result:
        scores['has_conclusion'] = 1
    
    # 检查是否有关键证据
    if '## 关键证据' in result or '关键证据' in result:
        scores['has_evidence'] = 1
    
    # 检查是否有解释性内容
    if '解释' in result or '机制' in result or '作用' in result:
        scores['has_interpretation'] = 1
    
    # 检查是否有临床意义
    if '临床' in result or '治疗' in result or '预后' in result:
        scores['has_clinical_significance'] = 1
    
    # 检查是否有引用
    if '引用' in result or '[C' in result:
        scores['has_citations'] = 1
    
    # 检查是否分析了本地数据
    if '本地' in result or 'Stressed' in result or 'p53' in result:
        scores['has_local_data_analysis'] = 1
    
    # 检查是否有机制解释
    if '机制' in result or '通路' in result or '信号' in result:
        scores['has_mechanism_explanation'] = 1
    
    total_score = sum(scores.values()) / len(scores)
    
    print("\n" + "=" * 80)
    print("结果质量评估:")
    print("=" * 80)
    for key, value in scores.items():
        status = "✅" if value == 1 else "❌"
        print(f"  {status} {key}: {value}")
    print(f"\n  综合评分: {total_score:.2f}")
    
    return total_score, scores

if __name__ == '__main__':
    folder_path = '/Users/zmz/Desktop/LLM辅助'
    result = run_agent_on_folder(folder_path)
    quality_score, details = evaluate_result_quality(result)
    
    # 保存结果
    with open('/Users/zmz/Desktop/LLM辅助/agent_result_evaluation.txt', 'w', encoding='utf-8') as f:
        f.write("# Agent 结果评估\n\n")
        f.write("## 原始结果\n")
        f.write(result)
        f.write("\n\n## 质量评估\n")
        for key, value in details.items():
            f.write(f"- {key}: {'通过' if value == 1 else '未通过'}\n")
        f.write(f"\n综合评分: {quality_score:.2f}")
    
    print(f"\n结果已保存到: /Users/zmz/Desktop/LLM辅助/agent_result_evaluation.txt")
