# 生物信息学 Deep Research Agent

一个面向生物信息学问题的 Deep Research Agent。它会先澄清和改写问题，再基于权威来源优先的策略规划研究问题、执行多轮检索、整理证据，并输出带真实引用的研究型回答。

## 现在具备的能力

- 文本问答优先：适合基因、变异、疾病关联、功能机制、通路和临床证据类问题
- 统一主链路：`clarify_or_rewrite -> entity_normalize -> build_research_plan -> execute_research_rounds -> synthesize_answer`
- 权威来源优先：默认依次使用 PubMed/NCBI、ClinVar、UniProt，必要时回退 Tavily / DuckDuckGo
- 深搜强度默认加深：默认至少审阅 100 条检索结果，并尽量给出不少于 10 条真实引用
- 结构化过程可视化：Web 端会展示澄清项、研究 brief、研究计划、当前来源、证据卡片、引用和停止原因
- 真实引用约束：最终回答只会引用实际检索到的证据，不能虚构 PMID 或数据库 ID
- 文件夹复用：支持把本地文件夹摘要作为研究上下文输入同一条主链路

## 目录结构

```text
bio_agent/
├── agent.py               # Deep Research 主编排
├── search_tool.py         # PubMed / ClinVar / UniProt / Web fallback 适配器
├── web_app.py             # FastAPI + SSE Web 服务
├── prompts.py             # Deep Research 提示词
├── utils.py               # 实体解析与通用工具
├── config.py              # 配置与环境变量加载
├── templates/index.html   # Web 页面
├── requirements.txt       # 运行依赖
├── requirements-dev.txt   # 测试依赖
└── tests/                 # 自动化测试
```

## 环境变量

### 核心

```bash
export DASHSCOPE_API_KEY="your-dashscope-key"
```

说明：
- 没有 `DASHSCOPE_API_KEY` 也可以运行，但会自动退回启发式规划与总结，不会调用 Qwen
- 如果配置了 DashScope，Agent 会优先用 Qwen 做问题改写、计划生成和答案综合

### 检索与策略

```bash
export TAVILY_API_KEY="your-tavily-key"       # 可选
export SEARCH_SOURCE_POLICY="authority_first" # 默认 authority_first
export ALLOW_WEB_FALLBACK="true"              # 默认 true
export MAX_RESULTS_PER_SOURCE="25"            # 默认 25
export MIN_RESULTS_TO_REVIEW="100"            # 默认 100
export MAX_ROUNDS="6"                         # 默认 6
export MIN_ROUNDS="1"                         # 默认 1
export MIN_OFFICIAL_EVIDENCE="1"              # 默认 1
export MIN_FINAL_CITATIONS="10"               # 默认 10
export SYNTHESIS_EVIDENCE_LIMIT="16"          # 默认 16，且不会低于 MIN_FINAL_CITATIONS
```

### NCBI 官方接口

```bash
export NCBI_EMAIL="you@example.com"
export NCBI_TOOL="bio-agent"
export NCBI_API_KEY="optional-ncbi-api-key"   # 可选
```

建议：
- 使用真实的 `NCBI_EMAIL` 和 `NCBI_TOOL`
- 有 `NCBI_API_KEY` 时，官方接口限流更宽松

## 安装

```bash
cd bio_agent
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

如果要跑测试：

```bash
pip install -r requirements-dev.txt
```

## 使用方式

### 命令行

```bash
python agent.py --input "BRCA1 c.185delAG 在乳腺癌中的临床意义是什么？"
python agent.py --input_file example/input_brca1.txt
python agent.py --folder /path/to/data/folder
python agent.py --input "TP53 在肺癌中的功能机制和临床证据如何？" --output result.md
```

### Web

```bash
python web_app.py
```

然后打开 [http://localhost:5000](http://localhost:5000)。

Web 页面会展示：

- 问题澄清项
- 自动改写后的研究 brief
- 研究计划与问题分解
- 当前正在使用的来源
- 实时证据卡片
- 最终回答与可点击引用
- 文件夹模式既支持手动输入本地路径，也支持直接点击“选择文件夹”从资源管理器上传目录内容

## 输出结构

最终回答固定包含这些部分：

```text
## 简要结论
## 关键证据
## 争议与局限
## 下一步建议
## 引用列表
```

其中：
- 结论部分优先回答用户原始问题
- 所有实质性结论都应能映射到真实 citation
- 引用列表只来自实际检索到的证据记录
- 默认会在正文中说明本次累计审阅了多少条检索结果，以及最终纳入了多少条真实引用

## 搜索策略

默认策略是 `authority_first`：

1. PubMed / NCBI：文献、摘要、元数据
2. ClinVar：变异分类、review status、相关 PMIDs
3. UniProt：蛋白功能、结构域、功能注释
4. Tavily / DuckDuckGo：仅在官方来源不足时补充网页发现

## 测试

运行离线测试：

```bash
pytest -q
```

可选在线 smoke test：

```bash
RUN_ONLINE_SMOKE=1 pytest -q tests/test_online_smoke.py
```

默认情况下，在线 smoke test 会跳过。

## 已知说明

- 当前 v1 不抓取任意网页全文，只使用官方 API 返回的结构化记录、摘要和必要 snippet
- 文件夹研究目前是把本地文件摘要转换为同一条文本 Deep Research 主链路来处理
- 如果输入非常宽泛，Agent 会继续给出分析，但会在结果里保留澄清项和默认假设
