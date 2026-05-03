"""调试证据池"""
import asyncio
import httpx
import json

async def debug_evidence():
    async with httpx.AsyncClient(base_url="http://localhost:8000", timeout=600.0) as client:
        print("启动研究...")
        await client.post("/api/research", json={
            "input_text": "",
            "folder_path": "/Users/zmz/Desktop/LLM辅助"
        })
        
        max_wait = 180
        waited = 0
        while waited < max_wait:
            await asyncio.sleep(5)
            waited += 5
            
            status_response = await client.get("/api/status")
            result = status_response.json()
            
            status = result.get('status', 'unknown')
            
            # 检查证据卡片
            cards = result.get('evidence_cards', [])
            if cards:
                web_cards = [c for c in cards if c.get('source_type') == 'web']
                pubmed_cards = [c for c in cards if c.get('source_type') == 'pubmed']
                print(f"[{waited}s] 证据: {len(pubmed_cards)} pubmed, {len(web_cards)} web")
                if web_cards:
                    print(f"  Web示例: {web_cards[0].get('title', 'N/A')[:80]}")
            
            if status in ['completed', 'failed']:
                citations = result.get('citations', [])
                web_cit = [c for c in citations if c.get('source_type') == 'web']
                print(f"\n完成! 引用: {len(citations)} total, {len(web_cit)} web")
                break

if __name__ == "__main__":
    asyncio.run(debug_evidence())
