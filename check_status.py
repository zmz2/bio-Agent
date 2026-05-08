import requests
import json
import time

# Wait a bit for processing
time.sleep(10)

r = requests.get('http://localhost:9000/api/status')
d = r.json()
print('Status:', d['status'])
print('Stage:', d.get('current_stage', 'N/A'))
sr = d.get('stop_reason', 'N/A')
print('Stop reason:', sr[:300] if sr else 'N/A')
fc = d.get('final_conclusion', '')
print('Has conclusion:', bool(fc))
print('Citations count:', len(d.get('citations', [])))
print('Evidence seen:', d.get('search_results_seen', 0))
web_count = len([c for c in d.get('citations', []) if c.get('source_type') == 'web'])
print('Web citations:', web_count)
print('\nCitation details:')
for c in d.get('citations', []):
    print(f"  {c['label']}: {c['source_type']} - {c['title'][:80]}")
