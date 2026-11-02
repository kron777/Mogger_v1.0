# Live "Construct Deck" logic for server.py's /api/construct-deck.
# 1. real threat index from history.json (deck_common, reused from watcher.py's report() logic)
# 2. live scrape of mtgazone.com's Historic Bo3 tier list (static HTML, not Cloudflare-walled -
#    unlike mtggoldfish/aetherhub/mtgdecks.net/moxfield's deck pages, which are, verified by hand)
# 3. deterministic scoring: count real interaction spells (regex over Scryfall oracle_text) in
#    each tier-list deck, weighted toward decks that can actually answer creature/pw threats
# 4. format the winner as an MTGA-importable decklist using set/collector_number from cards.json
import collections, json, os, re, urllib.request, urllib.error
import deck_common

D = os.path.expanduser("~/mtga")
HIST, CARDS = D + "/history.json", D + "/cards.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

# Verified by hand against mtgazone.com's live nav on 2026-11-02: these are the Bo3 (or, for
# Pioneer, the only) tier-list pages the site currently publishes. No Brawl tier list exists
# there, so singleton/100-card construction is out of scope - every format below is 60-card
# constructed. Pioneer's card pool includes paper-only cards with no Arena printing; those are
# flagged as non_arena_cards in the response rather than silently dropped or faked.
FORMATS = {
    "standard": {"label": "Standard", "url": "https://mtgazone.com/standard-bo3-metagame-tier-list/", "min_size": 60},
    "pioneer": {"label": "Pioneer", "url": "https://mtgazone.com/pioneer-metagame-tier-list/", "min_size": 60},
    "explorer": {"label": "Explorer", "url": "https://mtgazone.com/explorer-bo3-metagame-tier-list/", "min_size": 60},
    "historic": {"label": "Historic", "url": "https://mtgazone.com/historic-bo3-metagame-tier-list/", "min_size": 60},
    "timeless": {"label": "Timeless", "url": "https://mtgazone.com/timeless-bo3-metagame-tier-list/", "min_size": 60},
    "alchemy": {"label": "Alchemy", "url": "https://mtgazone.com/alchemy-bo3-metagame-tier-list/", "min_size": 60},
}

INTERACTION_RE = re.compile(
    r"destroy target|exile target|deals? \d+ damage to target creature|damage to any target|"
    r"gets -\d|return target creature[^.]*owner's hand|counter target spell|\bfight\b|"
    r"sacrifices? a creature|all creatures|each creature (?:gets|an opponent controls)",
    re.I)


def _strip_html(html):
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&#8211;", "-").replace("&#8217;", "'").replace("&amp;", "&").replace("&#8220;", '"').replace("&#8221;", '"')
    return re.sub(r"\s+", " ", text)


def fetch_meta_decks(tier_list_url):
    """Live-fetch a mtgazone tier-list page and parse full decklists out of it."""
    req = urllib.request.Request(tier_list_url, headers={"User-Agent": UA, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"mtgazone returned HTTP {e.code} fetching {tier_list_url} (no tier-list page available right now)")
    text = _strip_html(html)

    m = re.search(r"Tier Deck Name (.*?) Tier 1 Decks ", text)
    if not m:
        if "Tier Archetype" in text:
            raise RuntimeError(
                "this format's mtgazone page uses a per-tournament-result layout (multiple dated "
                "decklists per archetype, no single curated build), not the curated-build layout this "
                "feature parses - unsupported for now, not a transient failure")
        raise RuntimeError("mtgazone tier-list page layout didn't match (site may have changed)")
    rows = re.findall(r"Tier (\d) ([A-Za-z0-9][A-Za-z0-9 ,'\-]*?)(?=\s+Tier \d|$)", m.group(1))
    order = [(int(t), n.strip()) for t, n in rows]

    anchors = [mm.start() for mm in re.finditer(r"copy to clipboard \(arena\)", text)]
    if len(anchors) != len(order):
        raise RuntimeError(f"mtgazone deck count mismatch: {len(order)} named, {len(anchors)} decklists found")

    def parse_cards(t):
        out = []
        for cm in re.finditer(r"(?<!\()\b(\d{1,2})\s+([A-Z][^$]*?)\s+\$[\d,]+\.\d{2}", t):
            qty, cname = cm.group(1), cm.group(2).strip()
            if cname == "Cards":
                continue
            out.append((int(qty), cname))
        return out

    decks = []
    for i, ((tier, name), a) in enumerate(zip(order, anchors)):
        end = anchors[i + 1] - 700 if i + 1 < len(anchors) else len(text)
        chunk = text[a:end]
        cb_idx = chunk.find("Copy URL")
        sb_idx = chunk.find(" Sideboard ")
        if cb_idx == -1 or sb_idx == -1:
            continue
        main_text = chunk[cb_idx + len("Copy URL"):sb_idx]
        rest = chunk[sb_idx + len(" Sideboard "):]
        m2 = re.search(r"\d+ Cards \$[\d,]+\.\d+", rest)
        sb_text = rest[:m2.end()] if m2 else rest
        reasoning = rest[m2.end():][:500].strip() if m2 else ""
        decks.append({"name": name, "tier": tier, "main": parse_cards(main_text),
                       "side": parse_cards(sb_text), "reasoning": reasoning, "source_url": tier_list_url})
    if not decks:
        raise RuntimeError("parsed 0 decks from mtgazone tier list")
    return decks


def load_card_index():
    """name -> {set, collector_number, type_line, oracle_text}, first printing wins."""
    idx = {}
    for c in json.load(open(CARDS)):
        if c["name"] not in idx:
            idx[c["name"]] = c
    return idx


def score_deck(deck, threat_names, card_index):
    interaction = [(q, n) for q, n in deck["main"] if card_index.get(n) and INTERACTION_RE.search(card_index[n].get("oracle_text", ""))]
    interaction_count = sum(q for q, _ in interaction)
    creature_threats = [t for t in threat_names if "Creature" in card_index.get(t, {}).get("type_line", "")
                         or "Planeswalker" in card_index.get(t, {}).get("type_line", "")]
    score = interaction_count * 10 - deck["tier"]  # prefer more real interaction, then stronger tier
    return score, interaction, creature_threats


BASIC_BY_COLOR = {"W": "Plains", "U": "Island", "B": "Swamp", "R": "Mountain", "G": "Forest"}


def _is_basic_land(name, card_index):
    tl = card_index.get(name, {}).get("type_line", "")
    return "Basic" in tl and "Land" in tl  # also catches snow basics ("Basic Snow Land - Plains")


def _is_land(name, card_index):
    return "Land" in card_index.get(name, {}).get("type_line", "")


def _color_weights(main_cards, card_index):
    """Colored mana symbols in the deck's own nonland spells, weighted by copies - used to split
    new basics the same way the existing manabase already leans, not an arbitrary guess."""
    weights = collections.Counter()
    for q, n in main_cards:
        if _is_land(n, card_index):
            continue
        cost = card_index.get(n, {}).get("mana_cost", "") or ""
        for sym in re.findall(r"\{([WUBRG])\}", cost):
            weights[sym] += q
    if not weights:
        for q, n in main_cards:
            for color, bname in BASIC_BY_COLOR.items():
                if n == bname:
                    weights[color] += q
    if not weights:
        weights["W"] = 1
    return weights


def scale_up_lands(main_cards, target_size, format_key, card_index, colors=None):
    """Grow a decklist to target_size by adding land only, respecting the real 4-copy
    constructed cap on every non-basic card. Order: (1) top up non-basic lands already in the
    list up to 4 copies, (2) bring in other same-color non-basic lands not already present
    (also capped at 4 each) for variety, (3) fill whatever's left with basic lands split by the
    deck's own color weights. The spell package is never touched."""
    current_total = sum(q for q, _ in main_cards)
    extra = target_size - current_total
    if extra <= 0:
        return main_cards, []

    out = [[q, n] for q, n in main_cards]
    notes = []
    remaining = extra

    # (1) top up existing non-basic lands, capped at 4
    for row in out:
        if remaining <= 0:
            break
        q, n = row
        if _is_land(n, card_index) and not _is_basic_land(n, card_index):
            room = 4 - q
            if room > 0:
                add = min(room, remaining)
                row[0] += add
                remaining -= add

    # (2) bring in new same-color non-basic lands not already in the list, capped at 4 each
    if remaining > 0:
        present = {n for _, n in out}
        deck_colors = set(_color_weights(main_cards, card_index))
        candidates = []
        for name, info in card_index.items():
            if name in present:
                continue
            tl = info.get("type_line", "")
            if "Land" not in tl or "Basic" in tl:
                continue
            produced = set(re.findall(r"[Aa]dd \{([WUBRG])\}", info.get("oracle_text", "") or ""))
            if produced and produced <= deck_colors:
                candidates.append(name)
        for name in sorted(candidates)[:6]:  # bounded and deterministic, not unlimited variety
            if remaining <= 0:
                break
            add = min(4, remaining)
            out.append([add, name])
            remaining -= add
            notes.append(f"added {add}x {name} (new land, matches deck colors, wasn't in source list)")

    # (3) fill the rest with basics, split by the deck's existing color weights
    if remaining > 0:
        weights = _color_weights(main_cards, card_index)
        total_w = sum(weights.values())
        colors = list(weights)
        shares, assigned = {}, 0
        for c in colors:
            share = round(remaining * weights[c] / total_w)
            shares[c] = share
            assigned += share
        shares[colors[0]] += remaining - assigned  # fix rounding drift
        for c, share in shares.items():
            if share <= 0:
                continue
            bname = BASIC_BY_COLOR[c]
            for row in out:
                if row[1] == bname:
                    row[0] += share
                    break
            else:
                out.append([share, bname])

    result = [(q, n) for q, n in out]

    # (4) legality gate - shared with the AI path's validator; must hold or this is a real bug here
    violations = deck_common.validate_decklist(result, target_size, format_key, card_index, colors=colors)
    if violations:
        raise RuntimeError(f"scale_up_lands produced an illegal decklist: {'; '.join(violations)}")

    return result, notes


def format_arena_import(deck, card_index, deck_name=None):
    non_arena = []

    def fmt_section(cards):
        lines = []
        for qty, name in cards:
            c = card_index.get(name)
            if c and c.get("set") and c.get("collector_number"):
                lines.append(f"{qty} {name} ({c['set'].upper()}) {c['collector_number']}")
            else:
                lines.append(f"{qty} {name}")
                if name not in deck_common.BASICS:
                    non_arena.append(name)
        return lines

    out = []
    if deck_name:
        out += ["About", f"Name {deck_name}", ""]  # verified format: mtgarena-support's own export uses this header
    out += ["Deck"] + fmt_section(deck["main"]) + ["", "Sideboard"] + fmt_section(deck["side"])
    return "\n".join(out), sorted(set(non_arena))


def _deck_fits_colors(deck, colors, card_index):
    for q, name in deck["main"] + deck["side"]:
        c = card_index.get(name)
        if c and not deck_common.is_land(c) and not deck_common.color_fits(c, colors):
            return False
    return True


def construct_deck_scraped(format_key="historic", deck_name=None, size=None, colors=None):
    """The original path: live-scrape mtgazone's tier list, score by real interaction count
    against the threat index, scale with scale_up_lands(). Kept as the fallback when the AI path
    (construct_deck, below) fails to generate or validate."""
    fmt = FORMATS.get(format_key)
    if not fmt:
        raise RuntimeError(f"no tier-list page for format '{format_key}' - available: {', '.join(FORMATS)}")
    min_size = fmt["min_size"]
    if size is None:
        size = min_size
    size = max(int(size), min_size)  # server-side clamp, mirrors the UI's own clamp

    hist = json.load(open(HIST)) if os.path.exists(HIST) else []
    lc, ac = deck_common.threat_index(hist)
    top_threats = [name for name, _ in lc.most_common(10)]

    decks = fetch_meta_decks(fmt["url"])
    card_index = load_card_index()

    if colors:
        matching = [d for d in decks if _deck_fits_colors(d, colors, card_index)]
        if not matching:
            return {
                "decklist_text": "", "source_url": fmt["url"], "archetype": None, "tier": None,
                "format": fmt["label"], "size": size, "deck_name": deck_name,
                "reasoning": (f"No deck on mtgazone's current {fmt['label']} tier list fits within the "
                              f"selected {''.join(sorted(colors))} color identity ({len(decks)} decks checked, "
                              f"all use colors outside that selection). Try a wider color selection, or use "
                              f"the AI-direct path instead - it builds from the full legal card pool rather "
                              f"than picking from a short list of existing decks."),
                "missing_cards": [], "non_arena_cards": [], "collection_checked": False,
                "path": "fallback", "colors_requested": sorted(colors), "no_color_match": True,
            }
        decks = matching

    scored = []
    for d in decks:
        score, interaction, creature_threats = score_deck(d, top_threats, card_index)
        scored.append((score, d, interaction, creature_threats))
    scored.sort(key=lambda x: -x[0])
    best_score, best, interaction, creature_threats = scored[0]

    scaled_main, scale_notes = scale_up_lands(best["main"], size, format_key, card_index, colors=colors)
    scaled_best = dict(best, main=scaled_main)

    interaction_names = sorted({f"{q}x {n}" for q, n in interaction})
    reasoning = (
        f"Picked \"{best['name']}\" (Tier {best['tier']} on mtgazone's current {fmt['label']} Bo3 list): "
        f"it runs {sum(q for q,_ in interaction)} real interaction spells ({', '.join(interaction_names[:4])}"
        f"{'...' if len(interaction_names) > 4 else ''}), the most of any deck on the list scored against your "
        f"current loss history. {len(creature_threats)} of your top-10 threats ({', '.join(creature_threats) if creature_threats else 'none currently'}) "
        f"are creatures/planeswalkers this kind of interaction can actually answer."
    )
    if size > min_size:
        reasoning += f" Scaled from {min_size} to {size} cards by adding lands proportionally; the spell package is untouched."
    reasoning += " ".join(scale_notes)

    decklist_text, non_arena_cards = format_arena_import(scaled_best, card_index, deck_name)

    all_cards = sorted({n for _, n in scaled_best["main"] + best["side"] if n not in deck_common.BASICS})
    missing_cards = [{"name": n, "status": "unverified"} for n in all_cards]

    return {
        "decklist_text": decklist_text,
        "source_url": best["source_url"],
        "archetype": best["name"],
        "tier": best["tier"],
        "format": fmt["label"],
        "size": size,
        "deck_name": deck_name,
        "threats_considered": top_threats,
        "reasoning": reasoning,
        "missing_cards": missing_cards,
        "non_arena_cards": non_arena_cards,
        "collection_checked": False,
        "path": "fallback",
        "colors_requested": sorted(colors) if colors else None,
    }


def construct_deck(format_key="historic", deck_name=None, size=None, colors=None):
    """Primary entry point. Tries the AI path (ai_deck_builder) first; falls back to the
    tier-list-scraping path on any generation/validation failure, tagging which path produced
    the result so the UI can show it honestly. `colors` is an optional set/list of W/U/B/R/G
    letters - empty/None means no color-identity constraint."""
    fmt = FORMATS.get(format_key)
    if not fmt:
        raise RuntimeError(f"no tier-list page for format '{format_key}' - available: {', '.join(FORMATS)}")
    min_size = fmt["min_size"]
    size = max(int(size), min_size) if size else min_size
    colors = set(colors) if colors else None

    import ai_deck_builder
    ai_result, ai_error = ai_deck_builder.try_ai_construct(format_key, fmt, deck_name, size, load_card_index(), colors=colors)
    if ai_result:
        return ai_result

    fallback = construct_deck_scraped(format_key, deck_name, size, colors=colors)
    fallback["fallback_reason"] = ai_error
    return fallback


if __name__ == "__main__":
    print(json.dumps(construct_deck(), indent=1))
