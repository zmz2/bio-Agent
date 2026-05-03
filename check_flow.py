"""
检查实际执行流程
"""
import requests
import time

# 先检查服务器状态
response = requests.get('http://localhost:8000/api/status')
status = response.json()
print("当前服务器状态:")
print(f"  status: {status['status']}")
print(f"  final_conclusion: {status['final_conclusion'][:200] if status['final_conclusion'] else '空'}")

# 打印 synthesize_answer 方法的关键部分
print("\n检查 synthesize_answer 方法的逻辑:")
with open('/Users/zmz/Desktop/bio_agent/agent.py', 'r') as f:
    content = f.read()
    
# 查找 synthesize_answer 方法
start = content.find('def synthesize_answer')
end = content.find('def run(', start)
print(f"\nsynthesize_answer 方法位置: {start}-{end}")

# 检查是否使用了正确的 fallback 方法调用
if '_render_fallback_answer' in content[start:end]:
    print("✅ synthesize_answer 方法中调用了 _render_fallback_answer")
else:
    print("❌ synthesize_answer 方法中没有调用 _render_fallback_answer")

# 检查 _render_fallback_answer 方法中的关键内容
fallback_start = content.find('def _render_fallback_answer')
fallback_end = content.find('def _render_incomplete_research_answer', fallback_start)
fallback_content = content[fallback_start:fallback_end]

print("\n_render_fallback_answer 方法内容摘要:")
print("-" * 80)
lines = fallback_content.split('\n')[:30]
for i, line in enumerate(lines):
    print(f"{fallback_start + 1 + i}: {line}")
