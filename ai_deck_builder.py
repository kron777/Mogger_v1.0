# Primary "Construct Deck" path: ask Claude to build a decklist grounded in real account data
# (threat index, win/loss record, the real legal card pool for the format), then validate the
# result with deck_common.validate_decklist - the exact same checker the land-scaling fallback
# uses. One retry with the specific violations fed back; if still invalid, the caller
# (deck_builder.construct_deck) falls back to the tier-list-scraping path.
import json, os, re, urllib.request, urllib.error
import deck_common
import deck_builder

D = os.path.expanduser("~/mtga")
HIST = D + "/history.json"
ENV_FILE = D + "/.env"
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2026-10-21"
MODEL = "claude-opus-5-5"
MAX_POOL = 3000


def load_api_key():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    if os.path.exists(ENV_FILE):
        for line in open(ENV_FILE):
            line = line.strip()
            if line.startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip()
    return None


def build_legal_pool(format_key, card_index, threat_names, colors=None):
    """Real legal-card-pool for the format, trimmed to MAX_POOL entries for prompt size/cost -
    prioritizing the user's own threats and known interaction spells so truncation doesn't just
    chop off a random alphabetical tail of the format's card pool. If `colors` is set, nonland
    cards outside that color identity are excluded entirely (lands are never excluded by color)."""
    legal = {name: c for name, c in card_index.items() if deck_common.is_legal(c, format_key)
              and (not colors or deck_common.is_land(c) or deck_common.color_fits(c, colors))}
    priority, rest = [], []
    for name, c in legal.items():
        if name in threat_names or deck_builder.INTERACTION_RE.search(c.get("oracle_text", "")):
            priority.append(name)
        else:
            rest.append(name)
    priority.sort()
    rest.sort()
    chosen = (priority + rest)[:MAX_POOL]
    truncated = len(legal) - len(chosen)
    lines = [f"{n} ({legal[n]['set'].upper()}) {legal[n]['collector_number']} [{legal[n]['type_line']}]" for n in chosen]
    return lines, len(legal), truncated


def build_prompt(format_key, fmt, deck_name, size, card_index, colors=None):
    hist = json.load(open(HIST)) if os.path.exists(HIST) else []
    lc, ac = deck_common.threat_index(hist)
    threats = lc.most_common(30)
    wins = sum(1 for r in hist if r["result"] == "WIN")
    losses = sum(1 for r in hist if r["result"] == "LOSS")
    sample_note = ""
    if len(hist) < 20:
        sample_note = (f" This is a SMALL sample ({len(hist)} matches total) - treat single-occurrence "
                        f"threats as noise, not a real pattern, and don't overfit the build to them.")

    pool_lines, legal_count, truncated = build_legal_pool(format_key, card_index, {n for n, _ in threats}, colors=colors)
    pool_note = f" (truncated from {legal_count} total legal cards to {len(pool_lines)}; prioritized your threats and known interaction spells)" if truncated > 0 else ""
    color_line = (f"\nCOLOR IDENTITY CONSTRAINT: every nonland card must be within the {''.join(sorted(colors))} "
                  f"color identity. Lands may produce other colors for fixing, but no spell, creature, "
                  f"planeswalker, etc. outside that identity is allowed.\n") if colors else ""

    threat_lines = "\n".join(f"  {n}: {c} losses / {ac[n]} seen" for n, c in threats) or "  (no losses recorded yet)"

    prompt = f"""You are building a real, importable Magic: The Gathering Arena {fmt['label']} constructed deck.

ACCOUNT DATA (real, from this player's actual match history):
- Record: {wins}W-{losses}L across {len(hist)} tracked matches.{sample_note}
- Threat index (cards most common in opponent decks in matches this player LOST, losses/seen):
{threat_lines}

BUILD REQUEST:
- Format: {fmt['label']} ({format_key})
- Deck name: {deck_name or "(none given)"}
- Target size: exactly {size} mainboard cards
{color_line}
LEGAL CARD POOL for {fmt['label']}{pool_note} - you may ONLY use cards from this list, with the
EXACT name/set/collector_number shown (one real Arena printing per line, [type line] for reference):
{chr(10).join(pool_lines)}

HARD CONSTRAINTS (follow these exactly - a deck violating any of them will not import cleanly into Arena):
1. No more than 4 copies of any card that is not a true basic land (Plains/Island/Swamp/Mountain/Forest or their snow versions have no copy limit; everything else, max 4).
2. The mainboard ("deck") must total EXACTLY {size} cards, no more, no less.
3. Every card must be legal in {fmt['label']} and must come from the LEGAL CARD POOL list above.
4. A sideboard of up to 15 cards, same card-legality and copy rules.
{"5. Every nonland card's color identity must fit within " + "".join(sorted(colors)) + " - no exceptions." if colors else ""}

Build a deck that is well-positioned against this player's real threat list above - pick answers
to the specific recurring threats where they exist in the legal pool, not just a generic good deck.

Respond with ONLY a single JSON object, no markdown fences, no commentary before or after it:
{{"deck": [{{"qty": <int>, "name": "<exact name from pool>", "set": "<SET>", "collector_number": "<NUM>"}}, ...],
 "sideboard": [{{"qty": <int>, "name": "...", "set": "...", "collector_number": "..."}}, ...],
 "reasoning": "<3-5 sentences: the overall gameplan, and specifically which threats from the threat index this build answers and how>"}}
"""
    return prompt


def _call_claude(messages, max_tokens, api_key):
    # effort=low keeps this model's (adaptive-thinking-only) reasoning bounded - a grounded
    # pick-from-the-given-pool task doesn't need deep extended thinking, and thinking tokens are
    # billed as output tokens, competing with max_tokens against the actual JSON decklist.
    body = json.dumps({"model": MODEL, "max_tokens": max_tokens,
                        "thinking": {"type": "adaptive"}, "output_config": {"effort": "low"},
                        "messages": messages}).encode("utf-8")
    req = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "x-api-key": api_key, "anthropic-version": API_VERSION, "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Claude API HTTP {e.code}: {err_body[:300]}")


def _parse_response(data):
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    parsed = json.loads(text)
    main = [(int(c["qty"]), c["name"]) for c in parsed.get("deck", [])]
    side = [(int(c["qty"]), c["name"]) for c in parsed.get("sideboard", [])]
    return main, side, parsed.get("reasoning", ""), text


def try_ai_construct(format_key, fmt, deck_name, size, card_index, colors=None):
    """Returns (result_dict, None) on success, or (None, error_string) on any failure - API key
    missing, API error, bad JSON, or validation failure surviving one retry."""
    api_key = load_api_key()
    if not api_key:
        return None, "no ANTHROPIC_API_KEY available (not set in environment or ~/mtga/.env)"

    max_tokens = max(8000, size * 60)  # headroom for both adaptive-thinking tokens and the full JSON
    prompt = build_prompt(format_key, fmt, deck_name, size, card_index, colors=colors)
    messages = [{"role": "user", "content": prompt}]

    for attempt in range(2):  # 1 initial try + 1 corrective retry
        try:
            data = _call_claude(messages, max_tokens, api_key)
        except Exception as e:
            return None, f"Claude API call failed: {e}"

        try:
            main, side, reasoning, raw_text = _parse_response(data)
        except Exception as e:
            if attempt == 0:
                # fresh single-turn retry (not multi-turn: the previous response was unparseable
                # garbage/truncated, nothing coherent to continue from as an assistant turn)
                cutoff_note = " It was cut off at the token limit last time - keep reasoning to 2-3 short sentences." \
                    if data.get("stop_reason") == "max_tokens" else ""
                messages = [{"role": "user", "content": prompt +
                             f"\n\nIMPORTANT: respond with ONLY the JSON object, nothing else.{cutoff_note}"}]
                continue
            return None, f"Claude's response wasn't valid JSON (after retry): {e}"

        violations = deck_common.validate_decklist(main, size, format_key, card_index, colors=colors)
        if not violations:
            deck = {"main": main, "side": side}
            decklist_text, non_arena_cards = deck_builder.format_arena_import(deck, card_index, deck_name)
            all_cards = sorted({n for _, n in main + side if n not in deck_common.BASICS})
            return {
                "decklist_text": decklist_text,
                "source_url": None,
                "archetype": deck_name or f"AI-built {fmt['label']} deck",
                "tier": None,
                "format": fmt["label"],
                "size": size,
                "deck_name": deck_name,
                "reasoning": reasoning,
                "missing_cards": [{"name": n, "status": "unverified"} for n in all_cards],
                "non_arena_cards": non_arena_cards,
                "collection_checked": False,
                "path": "ai",
                "model": MODEL,
                "colors_requested": sorted(colors) if colors else None,
            }, None

        if attempt == 0:
            messages.append({"role": "assistant", "content": raw_text})
            messages.append({"role": "user", "content":
                "That response violates the hard constraints. Fix ONLY these problems and resend the "
                "full corrected JSON object (same schema, no markdown fences):\n" +
                "\n".join(f"- {v}" for v in violations)})
        else:
            return None, "AI-generated deck failed validation twice: " + "; ".join(violations)

    return None, "unreachable"
