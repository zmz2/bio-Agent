"""调试搜索过程"""
import asyncio
import httpx
import json

async def debug_search():
    async with httpx.AsyncClient(base_url="http://localhost:8000", timeout=600.0) as client:
        print("正在启动研究...")
        response = await client.post("/api/research", json={
            "input_text": "LLM辅助",
            "folder_path": ""
        })
        
        # 轮询并显示详细信息
        max_wait = 300
        waited = 0
        while waited < max_wait:
            await asyncio.sleep(3)
            waited += 3
            
            status_response = await client.get("/api/status")
            result = status_response.json()
            
            status = result.get('status', 'unknown')
            stage = result.get('current_stage', 'unknown')
            
            # 显示研究计划
            plan = result.get('research_plan', {})
            if plan and plan.get('questions'):
                print(f"\n研究问题:")
                for q in plan['questions'][:3]:
                    print(f"  - {q.get('text', 'N/A')[:100]}... (类型: {q.get('type', 'N/A')})")
            
            # 显示证据卡片
            cards = result.get('evidence_cards', [])
            if cards:
                print(f"\n证据卡片 (共 {len(cards)} 条):")
                for i, card in enumerate(cards[:5], 1):
                    print(f"  [{i}] 来源: {card.get('source', 'N/A')}")
                    print(f"      类型: {card.get('source_type', 'N/A')}")
                    print(f"      标题: {card.get('title', 'N/A')[:80]}...")
                    print(f"      URL: {card.get('url', 'N/A')[:80]}...")
            
            # 显示引用
            citations = result.get('citations', [])
            if citations:
                print(f"\n引用 (共 {len(citations)} 条):")
                for i, cit in enumerate(citations[:3], 1):
                    print(f"  [{i}] 类型: {cit.get('source_type', 'N/A')}")
                    print(f"      标题: {cit.get('title', 'N/A')[:80]}...")
            
            print(f"\n[{waited}s] 状态: {status}, 阶段: {stage}")
            
            if status in ['completed', 'failed']:
                print(f"\n=== 研究 {status} ===")
                break

if __name__ == "__main__":
    asyncio.run(debug_search())
