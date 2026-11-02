#!/usr/bin/env python3
# Serves ~/mtga as static files (dashboard.html, history.json, ...) plus /api/construct-deck.
import http.server, json, os, sys, traceback, urllib.parse

D = os.path.expanduser("~/mtga")
sys.path.insert(0, D)
import deck_builder
import ai_deck_builder


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=D, **kw)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/construct-deck":
            self.handle_construct_deck()
            return
        if path == "/api/formats":
            self.handle_formats()
            return
        if path == "/api/construct-deck-prompt":
            self.handle_generate_prompt()
            return
        # never serve dotfiles (.env holds ANTHROPIC_API_KEY) or anything outside the mtga dir
        if any(part.startswith(".") for part in path.split("/") if part):
            self.send_error(404, "File not found")
            return
        super().do_GET()

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_formats(self):
        self._send_json({k: {"label": v["label"], "min_size": v["min_size"]} for k, v in deck_builder.FORMATS.items()})

    @staticmethod
    def _parse_colors(qs):
        raw = qs.get("colors", [""])[0].strip().upper()
        if not raw:
            return None
        colors = {c for c in raw.split(",") if c in ("W", "U", "B", "R", "G")}
        return colors or None

    def handle_construct_deck(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        fmt = qs.get("format", ["historic"])[0]
        name = qs.get("name", [""])[0].strip() or None
        size_raw = qs.get("size", [""])[0]
        size = int(size_raw) if size_raw.isdigit() else None
        colors = self._parse_colors(qs)
        try:
            result = deck_builder.construct_deck(format_key=fmt, deck_name=name, size=size, colors=colors)
            self._send_json(result)
        except Exception as e:
            traceback.print_exc()
            self._send_json({"error": str(e)}, status=502)

    def handle_generate_prompt(self):
        """Same grounding data as the AI-direct path (build_prompt), but never calls the API -
        just hands back the assembled prompt text to paste elsewhere, free."""
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        format_key = qs.get("format", ["historic"])[0]
        name = qs.get("name", [""])[0].strip() or None
        size_raw = qs.get("size", [""])[0]
        colors = self._parse_colors(qs)
        try:
            fmt = deck_builder.FORMATS.get(format_key)
            if not fmt:
                raise RuntimeError(f"no tier-list page for format '{format_key}' - available: {', '.join(deck_builder.FORMATS)}")
            size = max(int(size_raw), fmt["min_size"]) if size_raw.isdigit() else fmt["min_size"]
            card_index = deck_builder.load_card_index()
            prompt_text = ai_deck_builder.build_prompt(format_key, fmt, name, size, card_index, colors=colors)
            self._send_json({"prompt_text": prompt_text, "format": fmt["label"], "size": size, "deck_name": name,
                              "colors_requested": sorted(colors) if colors else None})
        except Exception as e:
            traceback.print_exc()
            self._send_json({"error": str(e)}, status=502)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8777
    bind = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
    httpd = http.server.ThreadingHTTPServer((bind, port), Handler)
    print(f"serving {D} on {bind}:{port}", flush=True)
    httpd.serve_forever()
