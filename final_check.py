import requests
import json
import time

r = requests.get('http://localhost:9000/api/status', timeout=10)
d = r.json()
print('=== FINAL STATUS ===')
print('Status:', d['status'])
print('Stage:', d.get('current_stage', 'N/A'))
sr = d.get('stop_reason', 'N/A')
print('Stop reason:', sr[:300] if sr else 'N/A')
fc = d.get('final_conclusion', '')
print('Has conclusion:', bool(fc))
print('Citations count:', len(d.get('citations', [])))
web_count = len([c for c in d.get('citations', []) if c.get('source_type') == 'web'])
print('Web citations:', web_count)
print('\nCitation details:')
for c in d.get('citations', []):
    print(f"  {c['label']}: {c['source_type']} - {c['title'][:80]}")
if fc:
    print('\nFinal conclusion:')
    print(fc[:1000])
