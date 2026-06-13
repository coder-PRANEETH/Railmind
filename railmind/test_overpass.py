import requests
q = """[out:json][timeout:90];
area["name"="India"]->.searchArea;
(
  node["railway"="station"](area.searchArea);
);
out body;"""
r = requests.get('https://overpass-api.de/api/interpreter', params={'data': q}, headers={'User-Agent': 'RailMind/1.0'})
print(r.status_code)
if r.status_code == 200:
    import json
    data = r.json()
    print("Nodes:", len(data.get("elements", [])))
