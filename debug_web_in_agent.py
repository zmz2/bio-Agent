"""Debug web search in agent"""
import asyncio
import httpx

async def debug_web():
    async with httpx.AsyncClient(base_url="http://localhost:8000", timeout=300.0) as client:
        print("Starting research...")
        await client.post("/api/research", json={
            "input_text": "",
            "folder_path": "/Users/zmz/Desktop/LLM辅助"
        })
        
        # Wait for completion
        max_wait = 300
        waited = 0
        while waited < max_wait:
            await asyncio.sleep(5)
            waited += 5
            
            status_response = await client.get("/api/status")
            result = status_response.json()
            status = result.get('status', 'unknown')
            
            # Check logs for web-related entries
            logs = result.get('logs', [])
            web_logs = [l for l in logs if 'web' in l.lower() or 'Web' in l]
            if web_logs:
                print(f"\n[{waited}s] Web-related logs:")
                for log in web_logs[-5:]:
                    print(f"  {log}")
            
            # Check evidence cards
            cards = result.get('evidence_cards', [])
            if cards:
                web_cards = [c for c in cards if c.get('source_type') == 'web']
                print(f"[{waited}s] Evidence: {len(cards)} total, {len(web_cards)} web")
            
            if status in ['completed', 'failed']:
                citations = result.get('citations', [])
                web_cit = [c for c in citations if c.get('source_type') == 'web']
                print(f"\nDone! Citations: {len(citations)} total, {len(web_cit)} web")
                
                if web_cit:
                    for c in web_cit[:3]:
                        print(f"  - {c.get('label')}: {c.get('title', 'N/A')[:80]}")
                
                break

if __name__ == "__main__":
    asyncio.run(debug_web())
