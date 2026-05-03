"""
肾细胞癌 p53 生物学可解释性研究脚本
"""
from agent import BioResearchAgent
from config import Config

def run_p53_research():
    # 创建 Agent
    config = Config()
    agent = BioResearchAgent(config)
    
    # 研究问题
    question = """基于本地数据观察，Stressed Tumor (p53+) 细胞中 p53 signaling (KEGG: hsa04115) 活性显著高于其他细胞类型。请深入分析：
1. p53 信号通路在肾细胞癌发生发展中的作用机制
2. p53 激活与肿瘤细胞应激反应的关系
3. p53 信号如何影响肿瘤细胞的拟时序演化轨迹
4. 这些发现的临床意义和潜在治疗靶点
"""
    
    # 本地上下文
    local_context = {
        'folder_path': '/Users/zmz/Desktop/LLM辅助',
        'local_observations': [
            'Stressed Tumor (p53+) 细胞中 p53 signaling 通路活性最高 (Signature_score=0.103)',
            '细胞类型分布：Proximal Tubule=210, Tumor Epithelial=69, Tumor (Neuronal-like)=35, Podocytes=28, Stressed Tumor (p53+)=22',
            '拟时序分析显示细胞轨迹：Tumor Epithelial → Tumor Cells(Dedifferentiated) → Stressed Tumor (p53+)',
            '同时检测了 Cell Cycle 和 Apoptosis 通路的活性变化'
        ]
    }
    
    print('正在进行 p53 在肾细胞癌中的生物学可解释性研究...')
    print('=' * 80)
    
    result = agent.run(question, local_context=local_context, input_mode='folder')
    
    print('\n' + '=' * 80)
    print('生物学可解释性研究结论:')
    print('=' * 80)
    print(result)
    
    # 保存结果
    with open('/Users/zmz/Desktop/LLM辅助/p53_research_result.txt', 'w', encoding='utf-8') as f:
        f.write(result)
    print('\n结果已保存到: /Users/zmz/Desktop/LLM辅助/p53_research_result.txt')

if __name__ == '__main__':
    run_p53_research()
