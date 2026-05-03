import requests
import json

r = requests.get('http://localhost:9000/api/status')
d = r.json()
citations = d.get('citations', [])
print('Total citations:', len(citations))
print('\nSource types:')
source_types = {}
for c in citations:
    source_types[c['source_type']] = source_types.get(c['source_type'], 0) + 1
print(source_types)
print('\nDetailed citations:')
for c in citations:
    print(f"{c['label']}: {c['source_type']} - {c['title'][:80]}")
