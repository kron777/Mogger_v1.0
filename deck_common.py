# Shared pure helpers used by watcher.py (report.txt) and server.py (/api/construct-deck).
import collections

BASICS = {"Plains", "Island", "Swamp", "Mountain", "Forest"}


def threat_index(hist):
    """Cards in opponent decks, counted by (losses, seen) across match history.
    Returns (lc, ac) Counters: lc = loss counts, ac = total-seen counts, keyed by card name."""
    lc, ac = collections.Counter(), collections.Counter()
    for r in hist:
        for c in r.get("opp_cards", []):
            ac[c] += 1
            if r["result"] == "LOSS":
                lc[c] += 1
    return lc, ac


def is_basic_land(card_entry):
    tl = (card_entry or {}).get("type_line", "")
    return "Basic" in tl and "Land" in tl  # also matches snow basics ("Basic Snow Land - Plains")


def is_land(card_entry):
    return "Land" in (card_entry or {}).get("type_line", "")


def color_fits(card_entry, colors):
    """True if the card's color identity fits within `colors` (a set/list of W/U/B/R/G letters).
    Empty/falsy `colors` means no constraint selected - everything fits."""
    if not colors:
        return True
    ci = set((card_entry or {}).get("color_identity", []))
    return ci <= set(colors)


def is_legal(card_entry, format_key):
    """Scryfall has no 'explorer' legality key (verified against a live card object) - Explorer is
    documented community-wide as the Pioneer-legal, Arena-available subset, so it's approximated
    as pioneer-legal here. Every other format key maps directly to a real Scryfall legalities key."""
    if not card_entry:
        return False
    leg = card_entry.get("legalities", {})
    if format_key == "explorer":
        return leg.get("pioneer") == "legal"
    return leg.get(format_key) == "legal"


def validate_decklist(main_cards, target_size, format_key, card_index, colors=None):
    """main_cards: [(qty, name), ...]. Returns a list of plain-English violation strings
    (empty = valid). Shared by the land-scaling fallback path and the AI-generated path so both
    get checked against the exact same real rules instead of two drifting implementations.
    `colors` (optional set of W/U/B/R/G letters): nonland cards must fit within it - lands are
    exempt (fixing lands routinely produce colors outside the deck's own identity)."""
    violations = []
    total = sum(q for q, _ in main_cards)
    if total != target_size:
        violations.append(f"total count is {total}, must be exactly {target_size}")
    seen = set()
    for q, name in main_cards:
        if name in seen:
            violations.append(f"'{name}' appears on more than one line (merge duplicate lines)")
        seen.add(name)
        c = card_index.get(name)
        if not c:
            violations.append(f"'{name}' was not found in the card database (not a real/importable card)")
            continue
        if not is_basic_land(c) and q > 4:
            violations.append(f"'{name}' has {q} copies, the non-basic-land cap is 4")
        if not is_legal(c, format_key):
            violations.append(f"'{name}' is not legal in {format_key}")
        if colors and not is_land(c) and not color_fits(c, colors):
            violations.append(f"'{name}' has color identity {c.get('color_identity')} outside the selected {sorted(colors)} identity")
    return violations
