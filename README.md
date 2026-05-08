# 生物信息学 Deep Research Agent

一个面向生物信息学问题的深度研究智能体（Deep Research Agent），基于 LLM 与多源权威数据库，实现自动澄清问题、多轮检索、全文获取、证据评估与冲突检测，并输出带真实引用的研究型报告。

## 核心亮点

- **权威来源优先**：自动按 PubMed/NCBI → ClinVar → UniProt → Web 的优先级检索，确保证据来源可靠
- **全文自动获取**：支持 PubMed、PMC、DOI、bioRxiv、medRxiv、arXiv、ClinicalTrials.gov、Unpaywall 等渠道的全文抓取
- **多维度证据评估**：5 维评分体系（相关性、权威性、时效性、特异性、一致性），科学量化证据质量
- **冲突检测与解决**：自动识别 8 种证据矛盾模式，提供冲突严重度评估与解决建议
- **查询自动扩展**：通过同义词、缩写映射、概念扩展生成查询变体，检索覆盖率提升约 30%
- **真实引用约束**：所有结论均映射到真实检索到的证据，绝不虚构 PMID 或数据库 ID
- **结构化过程可视化**：Web 端实时展示澄清项、研究 brief、计划、来源、证据卡片、引用与停止原因
- **本地文件夹研究**：支持将本地数据文件夹（CSV、H5AD 等）自动解析为研究上下文
- **研究成果一键导出**：支持导出完整研究报告（Markdown）及参考文献索引与详情

## 系统架构

```
主链路: clarify_or_rewrite -> entity_normalize -> build_research_plan -> execute_research_rounds -> synthesize_answer
```

每一轮研究包含：
1. 问题分解与查询扩展
2. 多源权威检索
3. 全文内容获取
4. 证据评分与冲突检测
5. 证据综合与下一轮规划

## 目录结构

```text
bio-agent/
├── agent.py                  # Deep Research 主编排逻辑
├── search_tool.py            # PubMed / ClinVar / UniProt / Web 适配器
├── full_text_fetcher.py      # 全文内容获取器（多源支持）
├── web_app.py                # FastAPI + SSE Web 服务
├── agent_improvements.py     # 查询扩展 / 证据评分 / 冲突检测模块
├── prompts.py                # Deep Research 提示词模板
├── utils.py                  # 实体解析与通用工具函数
├── config.py                 # 配置与环境变量加载
├── start_server.py           # Web 服务器启动脚本
├── templates/index.html      # Web 前端页面
├── requirements.txt          # 运行依赖
├── requirements-dev.txt      # 开发与测试依赖
└── tests/                    # 自动化测试套件
```

## 环境变量

### 核心 LLM 配置

```bash
export DASHSCOPE_API_KEY="your-dashscope-key"
```

说明：
- 不配置 `DASHSCOPE_API_KEY` 亦可运行，Agent 将回退至启发式规划与总结
- 配置后 Agent 会优先使用 Qwen 模型进行问题改写、计划生成与答案综合

### 检索策略配置

```bash
export TAVILY_API_KEY="your-tavily-key"            # 可选，Tavily Web 搜索
export SEARCH_SOURCE_POLICY="authority_first"      # 默认 authority_first
export ALLOW_WEB_FALLBACK="true"                   # 默认 true
export MAX_RESULTS_PER_SOURCE="25"                 # 单源最大结果数
export MIN_RESULTS_TO_REVIEW="100"                 # 最低审阅结果数
export MAX_ROUNDS="6"                              # 最大研究轮次
export MIN_ROUNDS="1"                              # 最小研究轮次
export MIN_OFFICIAL_EVIDENCE="1"                   # 最低官方证据数
export MIN_FINAL_CITATIONS="10"                    # 最低引用数（软指标）
export SYNTHESIS_EVIDENCE_LIMIT="16"               # 合成阶段证据上限
```

### NCBI 官方接口配置

```bash
export NCBI_EMAIL="you@example.com"
export NCBI_TOOL="bio-agent"
export NCBI_API_KEY="optional-ncbi-api-key"        # 可选
```

建议：使用真实的 `NCBI_EMAIL` 与 `NCBI_TOOL`，配置 `NCBI_API_KEY` 可获得更宽松的限流策略。

## 安装

```bash
cd bio-Agent
python -m venv .venv
source .venv/bin/activate   # Linux/macOS
# 或 .venv\Scripts\activate  # Windows
pip install -r requirements.txt
```

如需运行测试：

```bash
pip install -r requirements-dev.txt
```

## 使用方式

### 命令行

```bash
# 直接输入问题
python agent.py --input "BRCA1 c.185delAG 在乳腺癌中的临床意义是什么？"

# 从文件读取问题
python agent.py --input_file example/input_brca1.txt

# 基于本地文件夹进行研究
python agent.py --folder /path/to/data/folder

# 指定输出文件
python agent.py --input "TP53 在肺癌中的功能机制和临床证据如何？" --output result.md
```

### Web 界面

```bash
python web_app.py
# 或
python start_server.py
```

然后访问 [http://localhost:8000](http://localhost:8000)。

Web 界面支持：
- 手动输入研究问题
- 选择本地文件夹上传
- 实时查看研究进度与过程
- 一键导出完整研究报告

## 输出报告结构

最终回答固定包含以下部分：

```text
## 简要结论
## 关键证据
## 争议与局限
## 下一步建议
## 引用列表
```

其中：
- 结论部分优先回答用户原始问题
- 所有实质性结论均映射到真实引用
- 引用列表仅来自实际检索到的证据记录
- 正文会说明累计审阅结果数与最终纳入引用数

## 搜索策略

默认策略为 `authority_first`：

1. **PubMed / NCBI**：文献、摘要、元数据
2. **ClinVar**：变异分类、review status、相关 PMIDs
3. **UniProt**：蛋白功能、结构域、功能注释
4. **全文获取**：PubMed → PMC → DOI → bioRxiv/medRxiv → arXiv → ClinicalTrials.gov → Unpaywall → 通用网页
5. **Web 补充检索**：Tavily / DuckDuckGo（官方来源不足时回退）

## 核心改进模块

### 查询扩展系统

通过同义词映射、缩写映射与概念扩展，自动生成多个查询变体，显著提升检索覆盖率。

### 多维度证据评分

- **相关性** (35%)：基因、变异、疾病匹配度
- **权威性** (25%)：来源可信度
- **时效性** (15%)：证据新旧程度
- **特异性** (15%)：问题针对性
- **一致性** (10%)：与其他证据一致性

### 冲突检测与解决

支持 8 种矛盾模式识别：
- contradictory_claims（矛盾主张）
- temporal_discrepancy（时间差异）
- population_specificity（人群特异性）
- 等

提供严重度评估与自动/人工解决建议。

## 测试

运行离线测试：

```bash
pytest -q
```

可选在线 smoke test：

```bash
RUN_ONLINE_SMOKE=1 pytest -q tests/test_online_smoke.py
```

默认情况下在线 smoke test 会跳过。

## 技术栈

- **LLM**：Alibaba DashScope (Qwen)
- **Web 框架**：FastAPI + uvicorn
- **前端**：原生 HTML/JS + SSE 流式更新
- **数据库**：PubMed、ClinVar、UniProt、Tavily、DuckDuckGo
- **全文获取**：NCBI E-utilities、PMC、Crossref、Unpaywall、bioRxiv、arXiv

## 已知限制

- 不抓取任意网页全文，仅使用官方 API 返回的结构化记录与摘要
- 文件夹研究将本地文件摘要转换为文本 Deep Research 主链路处理
- 对于非常宽泛的输入，Agent 会继续给出分析，但会保留澄清项和默认假设

## 版本历史

- **v2.0.0**：集成全文获取系统、查询扩展、多维度证据评分、冲突检测模块
- **v1.0.0**：初始版本，基础 Deep Research 主链路

## License

MIT
