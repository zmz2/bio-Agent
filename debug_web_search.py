"""调试web搜索问题"""
from search_tool import SearchTool, WebFallbackClient
from config import get_config

config = get_config()
config.search.allow_web_fallback = True

tool = SearchTool(config)

# 测试不同来源的搜索
query = "p53 kidney cancer renal cell carcinoma"
print(f"测试查询: {query}")
print(f"允许web搜索: {config.search.allow_web_fallback}")

# 测试web搜索
print("\n=== 测试 web 搜索 ===")
try:
    results = tool.search_source("web", query, max_results=5)
    print(f"Web搜索结果: {len(results)} 条")
    for i, r in enumerate(results[:3], 1):
        print(f"  [{i}] {r.title}")
        print(f"      来源: {r.source}")
        print(f"      URL: {r.url}")
        print(f"      摘要: {(r.snippet or '')[:100]}")
        print()
except Exception as e:
    print(f"搜索失败: {e}")
    import traceback
    traceback.print_exc()

# 测试PubMed搜索
print("\n=== 测试 PubMed 搜索 ===")
try:
    results = tool.search_source("pubmed", query, max_results=5)
    print(f"PubMed搜索结果: {len(results)} 条")
    for i, r in enumerate(results[:3], 1):
        print(f"  [{i}] {r.title}")
        print(f"      来源: {r.source}")
        print(f"      URL: {r.url}")
        print(f"      摘要: {(r.snippet or '')[:100]}")
        print()
except Exception as e:
    print(f"搜索失败: {e}")
