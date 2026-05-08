import requests
import json
import time

print("等待研究完成...")
for i in range(30):
    time.sleep(10)
    try:
        r = requests.get('http://localhost:9000/api/status', timeout=5)
        d = r.json()
        status = d.get('status', 'unknown')
        stage = d.get('current_stage', 'N/A')
        citations = len(d.get('citations', []))
        web_count = len([c for c in d.get('citations', []) if c.get('source_type') == 'web'])
        fc = d.get('final_conclusion', '')
        print(f"[{i*10}s] Status: {status}, Stage: {stage}, Citations: {citations} (web: {web_count}), Has conclusion: {bool(fc)}")
        
        if status in ['completed', 'failed', 'idle']:
            print("\n=== FINAL STATUS ===")
            print('Status:', d['status'])
            print('Stage:', d.get('current_stage', 'N/A'))
            sr = d.get('stop_reason', 'N/A')
            print('Stop reason:', sr[:300] if sr else 'N/A')
            print('Has conclusion:', bool(fc))
            print('Citations count:', len(d.get('citations', [])))
            web_count = len([c for c in d.get('citations', []) if c.get('source_type') == 'web'])
            print('Web citations:', web_count)
            print('\nCitation details:')
            for c in d.get('citations', []):
                print(f"  {c['label']}: {c['source_type']} - {c['title'][:80]}")
            if fc:
                print('\nFinal conclusion preview:')
                print(fc[:500])
            break
    except Exception as e:
        print(f"[{i*10}s] Error: {e}")
