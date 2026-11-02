#!/usr/bin/env python3
# Background MTGA watcher. Tails Player.log forever, logs every finished match + play-by-play.
# Output: ~/mtga/report.txt (read this), ~/mtga/history.json (raw data for dashboard.html)
import sys, os, json, glob, time, collections, urllib.request, datetime
import deck_common

D = os.path.expanduser("~/mtga"); H = os.path.expanduser("~")
CARDS, HIST, REPORT = D+"/cards.json", D+"/history.json", D+"/report.txt"
BASICS = {"Plains","Island","Swamp","Mountain","Forest"}
PUBLIC_ZONES = {"ZoneType_Battlefield","ZoneType_Stack","ZoneType_Graveyard","ZoneType_Exile"}
ROOTS = ['/mnt/games/SteamLibrary/steamapps/compatdata/2141910/pfx/drive_c/users/steamuser/AppData/LocalLow/Wizards Of The Coast/MTGA',
         H+"/.steam/steam/steamapps/compatdata/2141910/pfx/drive_c/users/steamuser/AppData/LocalLow/Wizards Of The Coast/MTGA",
         H+"/.local/share/Steam/steamapps/compatdata/2141910/pfx/drive_c/users/steamuser/AppData/LocalLow/Wizards Of The Coast/MTGA",
         H+"/AppData/LocalLow/Wizards Of The Coast/MTGA"]

def log(*a): print(*a, flush=True)

def find(name):
    for r in ROOTS:
        p = f"{r}/{name}"
        if os.path.exists(p): return p
    hits = glob.glob(H+f"/.local/share/lutris/**/MTGA/{name}", recursive=True)
    return hits[0] if hits else None

def load_names():
    old = not os.path.exists(CARDS) or time.time()-os.path.getmtime(CARDS) > 14*86400
    if old:
        try:
            import gzip
            req = lambda u: urllib.request.Request(u, headers={"User-Agent":"mtga-watcher/1.0","Accept":"*/*"})
            data = json.load(urllib.request.urlopen(req("https://api.scryfall.com/bulk-data")))
            item = next(d for d in data["data"] if d["type"] == "default_cards")
            tmp = CARDS + ".gz"
            urllib.request.urlretrieve(item["jsonl_download_uri"], tmp)
            cards = []
            with gzip.open(tmp, "rt", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line: continue
                    c = json.loads(line)
                    if "arena_id" not in c: continue
                    oracle = c.get("oracle_text")
                    if oracle is None and c.get("card_faces"):
                        oracle = " // ".join(fc.get("oracle_text", "") or "" for fc in c["card_faces"])
                    legalities = c.get("legalities", {})
                    cards.append({"arena_id": c["arena_id"], "name": c["name"], "set": c.get("set", ""),
                                  "collector_number": c.get("collector_number", ""), "type_line": c.get("type_line", ""),
                                  "oracle_text": oracle or "", "mana_cost": c.get("mana_cost", "") or "",
                                  "legalities": {k: legalities.get(k, "not_legal") for k in
                                                 ("standard", "pioneer", "historic", "timeless", "alchemy")},
                                  "color_identity": c.get("color_identity", [])})
            json.dump(cards, open(CARDS+".tmp","w")); os.replace(CARDS+".tmp", CARDS); os.remove(tmp)
            log(f"card db refreshed ({len(cards)} cards)")
        except Exception as e:
            log("card db download failed:", e)
    if not os.path.exists(CARDS): return {}
    return {c["arena_id"]: c["name"] for c in json.load(open(CARDS)) if "arena_id" in c}
hist = json.load(open(HIST)) if os.path.exists(HIST) else []
seen = {r["match"] for r in hist}
names = load_names()
ME = {"n": sys.argv[1].lower() if len(sys.argv) > 1 else None}

def walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values(): yield from walk(v)
    elif isinstance(o, list):
        for v in o: yield from walk(v)

def report():
    L = [r for r in hist if r["result"] == "LOSS"]; W = len(hist)-len(L)
    out = [f"updated {datetime.datetime.now():%Y-%m-%d %H:%M}   player: {ME['n']}",
           f"{len(hist)} matches   {W}W-{len(L)}L   winrate {100*W/max(1,len(hist)):.0f}%\n"]
    fm = collections.defaultdict(lambda: [0,0])
    for r in hist: fm[r.get("event","?")][r["result"]=="LOSS"] += 1
    out.append("By event/format (W / L):")
    for k,(w,l) in sorted(fm.items(), key=lambda x:-sum(x[1])): out.append(f"  {k}: {w} / {l}")
    lc, ac = deck_common.threat_index(hist)
    out.append("\nCards in opponent decks that beat me (losses / seen):")
    for c,n in lc.most_common(40): out.append(f"  {n:3d} / {ac[c]:3d}  {c}")
    out.append("\nLast 25 matches:")
    for r in hist[-25:][::-1]:
        out.append(f"\n[{r['result']}] {r['time']}  vs {r['opponent']}  ({r.get('event','?')})\n   " + ", ".join(r["opp_cards"]))
    open(REPORT+".tmp","w").write("\n".join(out)); os.replace(REPORT+".tmp", REPORT)

def finish(mid, m, win):
    if mid in seen or ME["n"] is None: return
    mine = [s for s,n in m["seats"].items() if ME["n"] in n.lower()]
    if not mine: return
    s = mine[0]; opp = [x for x in m["seats"] if x != s]
    if not opp: return
    plays = [{"turn": p["turn"], "mine": p["seat"] == s, "player": m["seats"].get(p["seat"], "?"),
              "card": p["card"], "zone": p["zone"], "category": p["category"],
              "hand_at_time": p["hand"], "life_mine": p["life"].get(s), "life_opp": p["life"].get(opp[0]),
              "lands_in_play": p["lands_in_play"], "mana_available": p["mana_available"]} for p in m["plays"]]
    opp_cards = sorted({p["card"] for p in plays if not p["mine"] and p["card"] not in BASICS})
    row = {"match": mid, "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "event": m["event"],
           "opponent": m["seats"][opp[0]], "result": "WIN" if win == s else "LOSS",
           "opp_cards": opp_cards, "plays": plays}
    hist.append(row); seen.add(mid)
    json.dump(hist, open(HIST,"w"), indent=1); report()
    log(f"{row['result']} vs {row['opponent']} ({len(row['opp_cards'])} opp cards, {len(plays)} plays)")

class Parser:
    def __init__(s): s.buf=""; s.depth=0; s.cur=None; s.m={}
    def feed(s, line):
        t = line.strip()
        if not s.buf and not t.startswith("{"): return
        s.buf += t; s.depth += t.count("{")-t.count("}")
        if len(s.buf) > 8_000_000: s.buf=""; s.depth=0; return
        if s.depth <= 0:
            try: s.handle(json.loads(s.buf))
            except Exception: pass
            s.buf=""; s.depth=0
    def handle(s, b):
        for d in walk(b):
            if "authenticateResponse" in d and isinstance(d["authenticateResponse"], dict):
                n = d["authenticateResponse"].get("screenName")
                if n and not ME["n"]: ME["n"] = n.lower()
            if "gameRoomConfig" in d:
                cfg = d["gameRoomConfig"]; mid = cfg.get("matchId")
                if mid:
                    s.cur = mid
                    m = s.m.setdefault(mid, {"seats":{}, "event":None, "zones":{}, "objs":{}, "tapped":{},
                                              "life":{}, "turn":0, "plays":[], "ann_seen":set()})
                    for p in cfg.get("reservedPlayers", []):
                        m["seats"][p.get("systemSeatId")] = p.get("playerName","?")
                        if p.get("eventId"): m["event"] = p.get("eventId")
            if s.cur and s.cur in s.m and any(k in d for k in ("gameObjects","zones","annotations","turnInfo","players")):
                m = s.m[s.cur]
                for z in d.get("zones", []):
                    m["zones"][z.get("zoneId")] = {"type": z.get("type"), "owner": z.get("ownerSeatId")}
                for o in d.get("gameObjects", []):
                    if o.get("type") == "GameObjectType_Card":
                        m["objs"][o.get("instanceId")] = {"grpId": o.get("grpId"), "owner": o.get("ownerSeatId"),
                                                           "controller": o.get("controllerSeatId"), "zoneId": o.get("zoneId"),
                                                           "cardTypes": o.get("cardTypes", [])}
                for pl in d.get("players", []):
                    sn = pl.get("systemSeatNumber")
                    if sn is not None and pl.get("lifeTotal") is not None:
                        m["life"][sn] = pl["lifeTotal"]
                ti = d.get("turnInfo")
                if ti and ti.get("turnNumber") is not None:
                    m["turn"] = ti["turnNumber"]
                for ann in d.get("annotations", []):
                    aid = ann.get("id")
                    if aid is not None:
                        if aid in m["ann_seen"]: continue
                        m["ann_seen"].add(aid)
                    types = ann.get("type", [])
                    det = {}
                    for kv in ann.get("details", []):
                        key = kv.get("key")
                        for vk in ("valueInt32","valueString"):
                            if vk in kv: det[key] = kv[vk][0] if kv[vk] else None
                    if "AnnotationType_ObjectIdChanged" in types:
                        orig, new = det.get("orig_id"), det.get("new_id")
                        if orig in m["objs"] and new is not None:
                            m["objs"][new] = m["objs"][orig]
                        if orig in m["tapped"] and new is not None:
                            m["tapped"][new] = m["tapped"][orig]
                    if "AnnotationType_TappedUntappedPermanent" in types:
                        for iid in ann.get("affectedIds", []):
                            m["tapped"][iid] = bool(det.get("tapped"))
                    if "AnnotationType_ZoneTransfer" in types:
                        ztype = m["zones"].get(det.get("zone_dest"), {}).get("type")
                        if ztype in PUBLIC_ZONES:
                            for iid in ann.get("affectedIds", []):
                                info = m["objs"].get(iid)
                                if not info: continue
                                name = names.get(info["grpId"])
                                if not name: continue
                                actor = info["owner"]
                                hand_zone = next((zid for zid, zv in m["zones"].items()
                                                   if zv.get("type") == "ZoneType_Hand" and zv.get("owner") == actor), None)
                                hand = sorted({names[o["grpId"]] for o in m["objs"].values()
                                               if o["zoneId"] == hand_zone and o["grpId"] in names})
                                lands = [iid2 for iid2, o in m["objs"].items()
                                         if o["controller"] == actor and "CardType_Land" in o.get("cardTypes", [])
                                         and m["zones"].get(o["zoneId"], {}).get("type") == "ZoneType_Battlefield"]
                                untapped = [iid2 for iid2 in lands if not m["tapped"].get(iid2, False)]
                                m["plays"].append({"turn": m["turn"], "seat": actor, "card": name,
                                                    "zone": ztype.replace("ZoneType_",""), "category": det.get("category"),
                                                    "hand": hand, "life": dict(m["life"]),
                                                    "lands_in_play": len(lands), "mana_available": len(untapped)})
            if s.cur and s.cur in s.m and "finalMatchResult" in d:
                for r in d["finalMatchResult"].get("resultList", []):
                    if r.get("scope")=="MatchScope_Match":
                        finish(s.cur, s.m[s.cur], r.get("winningTeamId"))

def backfill():
    p = find("Player-prev.log")
    if p:
        ps = Parser()
        for line in open(p, errors="ignore"): ps.feed(line)

def main():
    log("watcher started")
    backfill()
    ps, pos, ino, rem = Parser(), 0, None, ""
    while True:
        path = find("Player.log")
        try:
            if path:
                st = os.stat(path)
                if ino != st.st_ino or st.st_size < pos:
                    ps, pos, ino, rem = Parser(), 0, st.st_ino, ""
                if st.st_size > pos:
                    with open(path,"rb") as f:
                        f.seek(pos); data = f.read()
                    pos += len(data)
                    lines = (rem + data.decode(errors="ignore")).split("\n"); rem = lines.pop()
                    for ln in lines: ps.feed(ln)
        except Exception as e:
            log("loop error:", e)
        time.sleep(2)

if __name__ == "__main__": main()
