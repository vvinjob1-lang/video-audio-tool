# apply_gemini_tts_patch.py
# Runtime-safe patcher for adding Gemini TTS support to the existing Flask app.py.
#
# Usage:
#   python apply_gemini_tts_patch.py
#
# It is idempotent. Running it multiple times will not duplicate the patch.

from pathlib import Path
import re
import sys

APP_PATH = Path("app.py")
ADDON_IMPORT = "from gemini_tts_addon import try_handle_gemini_tts, register_gemini_tts_routes"
REGISTER_CALL = "register_gemini_tts_routes(app)"
TTS_HOOK = """    # V20.4 Gemini TTS add-on: handle Gemini engines first, otherwise continue existing Edge TTS logic.
    gemini_tts_response = try_handle_gemini_tts()
    if gemini_tts_response is not None:
        return gemini_tts_response
"""


def fail(msg: str):
    print(f"[gemini-tts-patch] ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def main():
    if not APP_PATH.exists():
        fail("app.py not found in current directory.")

    text = APP_PATH.read_text(encoding="utf-8")

    # Add import.
    if ADDON_IMPORT not in text:
        lines = text.splitlines()
        insert_at = 0
        for i, line in enumerate(lines):
            if line.startswith("import ") or line.startswith("from "):
                insert_at = i + 1
        lines.insert(insert_at, ADDON_IMPORT)
        text = "\n".join(lines) + "\n"

    # Register routes after Flask app creation.
    if REGISTER_CALL not in text:
        m = re.search(r"(?m)^(app\s*=\s*Flask\([^\n]*\)\s*)$", text)
        if not m:
            fail("Could not find app = Flask(...) line. Please add register_gemini_tts_routes(app) manually after app creation.")
        insert_pos = m.end()
        text = text[:insert_pos] + "\n" + REGISTER_CALL + "\n" + text[insert_pos:]

    # Add hook at top of /tts route.
    if "gemini_tts_response = try_handle_gemini_tts()" not in text:
        # Find common Flask route decorators for /tts.
        route_match = re.search(
            r"(?ms)^@app\.route\((['\"])/tts\1[^\n]*\)\s*\n(?:@[^\n]+\n)*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\([^)]*\)\s*:\s*\n",
            text,
        )
        if not route_match:
            route_match = re.search(
                r"(?ms)^@app\.post\((['\"])/tts\1[^\n]*\)\s*\n(?:@[^\n]+\n)*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\([^)]*\)\s*:\s*\n",
                text,
            )
        if not route_match:
            fail("Could not find the /tts route. Please insert the TTS_HOOK at the top of your /tts function.")

        insert_pos = route_match.end()
        text = text[:insert_pos] + TTS_HOOK + text[insert_pos:]

    APP_PATH.write_text(text, encoding="utf-8")
    print("[gemini-tts-patch] OK: app.py patched with Gemini TTS support.")


if __name__ == "__main__":
    main()
