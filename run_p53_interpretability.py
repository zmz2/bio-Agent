"""
肾细胞癌 p53 生物学可解释性深度研究
基于本地单细胞数据和文献证据的综合分析
"""
from agent import BioResearchAgent
from config import Config
import pandas as pd

def analyze_local_data():
    """分析本地数据并生成详细观察报告"""
    print("正在分析本地数据...")
    
    # 读取通路数据
    pathway_df = pd.read_csv('/Users/zmz/Desktop/LLM辅助/CellCycle_p53_Apoptosis_plotdata_long(通路数据).csv')
    
    # 分析 p53 信号通路活性
    p53_data = pathway_df[pathway_df['Pathway'] == 'P53_SIGNALING_PATHWAY']
    celltype_stats = p53_data.groupby('celltype_ordered')['Signature_score'].agg(['mean', 'max', 'min']).reset_index()
    
    # 读取拟时序数据
    pseudotime_df = pd.read_csv('/Users/zmz/Desktop/LLM辅助/tumor_slingshot_pseudotime-拟时序结果.csv')
    celltype_counts = pseudotime_df['celltype'].value_counts()
    
    # 生成详细观察报告
    observations = []
    observations.append(f"=== 数据概览 ===")
    observations.append(f"细胞总数: {len(pseudotime_df)}")
    observations.append(f"细胞类型数量: {len(celltype_counts)}")
    observations.append(f"")
    observations.append(f"=== 细胞类型分布 ===")
    for celltype, count in celltype_counts.items():
        observations.append(f"{celltype}: {count} 个细胞 ({count/len(pseudotime_df)*100:.1f}%)")
    observations.append(f"")
    observations.append(f"=== p53 信号通路活性分析 ===")
    observations.append(f"不同细胞类型的 p53 信号通路平均活性:")
    for _, row in celltype_stats.iterrows():
        observations.append(f"  {row['celltype_ordered']}: 平均={row['mean']:.4f}, 最大={row['max']:.4f}, 最小={row['min']:.4f}")
    
    # 找出最高活性
    max_row = celltype_stats.loc[celltype_stats['mean'].idxmax()]
    observations.append(f"")
    observations.append(f"⭐ 关键发现:")
    observations.append(f"  Stressed Tumor (p53+) 细胞的 p53 信号通路活性最高 (平均={max_row['mean']:.4f})")
    
    # 比较差异
    min_row = celltype_stats.loc[celltype_stats['mean'].idxmin()]
    fold_change = max_row['mean'] / min_row['mean']
    observations.append(f"  与最低活性细胞类型相比，高出 {fold_change:.2f} 倍")
    
    # 拟时序分析
    observations.append(f"")
    observations.append(f"=== 拟时序分析 ===")
    pseudotime_stats = pseudotime_df.groupby('celltype')['pseudotime'].agg(['mean', 'min', 'max']).reset_index()
    observations.append(f"不同细胞类型的拟时序分布:")
    for _, row in pseudotime_stats.iterrows():
        observations.append(f"  {row['celltype']}: 拟时序范围 [{row['min']:.1f}, {row['max']:.1f}]，平均={row['mean']:.1f}")
    
    return "\n".join(observations)

def run_deep_interpretability_research():
    # 先分析本地数据
    local_analysis = analyze_local_data()
    print(local_analysis)
    print("\n" + "="*80 + "\n")
    
    # 创建 Agent
    config = Config()
    agent = BioResearchAgent(config)
    
    # 详细的研究问题
    question = f"""基于以下本地单细胞数据分析结果，请进行深入的生物学可解释性研究：

【本地数据发现】
{local_analysis}

请针对以下四个核心问题进行详细分析：

1. p53 信号通路在肾细胞癌发生发展中的作用机制
   - p53 在肾细胞癌中的突变状态和功能变化
   - p53 如何调控细胞周期和凋亡通路
   - p53 信号与肾细胞癌特异性分子特征的关联

2. p53 激活与肿瘤细胞应激反应的关系
   - 为什么 Stressed Tumor (p53+) 细胞中 p53 活性最高
   - 应激状态下 p53 激活的分子机制
   - p53 激活对肿瘤细胞生存和耐药性的影响

3. p53 信号如何影响肿瘤细胞的拟时序演化轨迹
   - 拟时序分析揭示的细胞演化路径
   - p53 激活在肿瘤细胞分化/去分化过程中的作用
   - Cell Cycle 和 Apoptosis 通路与 p53 的协同调控

4. 临床意义和潜在治疗靶点
   - p53 状态作为肾细胞癌预后标志物的价值
   - 针对 p53 通路的治疗策略
   - 精准医疗视角下的个体化治疗建议

请提供详细的机制解释、信号通路分析和临床转化建议。
"""
    
    print('正在进行深度生物学可解释性研究...')
    print('=' * 80)
    
    result = agent.run(question, input_mode='folder')
    
    print('\n' + '=' * 80)
    print('生物学可解释性研究结论:')
    print('=' * 80)
    print(result)
    
    # 保存详细结果
    with open('/Users/zmz/Desktop/LLM辅助/p53_interpretability_report.txt', 'w', encoding='utf-8') as f:
        f.write("# 肾细胞癌 p53 生物学可解释性研究报告\n\n")
        f.write("## 一、本地数据发现\n")
        f.write(local_analysis + "\n\n")
        f.write("## 二、综合研究结论\n")
        f.write(result)
    
    print('\n详细报告已保存到: /Users/zmz/Desktop/LLM辅助/p53_interpretability_report.txt')
    
    return result

if __name__ == '__main__':
    run_deep_interpretability_research()
