import json, gzip, urllib.request, shutil, os
req = urllib.request.Request("https://api.scryfall.com/bulk-data", headers={"User-Agent":"mtga-watcher/1.0","Accept":"*/*"})
data = json.load(urllib.request.urlopen(req))
item = next(d for d in data["data"] if d["type"] == "default_cards")
url = item["jsonl_download_uri"]
print("downloading:", url)
tmp = "/home/rr/mtga/cards.jsonl.gz"
urllib.request.urlretrieve(url, tmp)
cards = []
with gzip.open(tmp, "rt", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line: continue
        c = json.loads(line)
        if "arena_id" in c:
            cards.append({"arena_id": c["arena_id"], "name": c["name"]})
json.dump(cards, open("/home/rr/mtga/cards.json", "w"))
os.remove(tmp)
print(f"saved {len(cards)} cards to cards.json")
