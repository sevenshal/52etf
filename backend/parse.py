import json

with open("out.json", "r") as f:
    data = json.load(f)

print(json.dumps(list(data['data'].keys()), indent=2))
print("positionItems:")
print(json.dumps(data['data'].get("positionItems", [])[:2], indent=2))
print("name:", data['data'].get('name'))
print("strategyName:", data['data'].get('strategyName'))
print("cash:", data['data'].get('availableBalance'))
print("total:", data['data'].get('totalMarketValue'))

