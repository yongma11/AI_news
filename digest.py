"""
agentnews -> Telegram daily macro digest

Two modes (env DIGEST_MODE, or ?mode= on the request):
  relay       : compress the board and forward as-is (English, no LLM)
  synthesize  : translate to Korean + add a semiconductor/SOXL lens via the
                Anthropic API  (default)

Flask is only needed for the (legacy) web-server mode; the GitHub Actions run
never serves HTTP, so Flask is imported lazily inside run_web_server().
"""

import os
import re
import json
import html
import requests
from datetime import datetime, timezone


BOARD_URL = "https://agentnews.md/finance.md"
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
HTTP_TIMEOUT = 25
LLM_TIMEOUT = 60

DESK_FIELDS = ["Held", "Falsifier", "Contested", "Changed since last"]
DESK_LABELS = {
    "Held": "Held",
    "Falsifier": "Falsifier",
    "Contested": "Contested",
    "Changed since last": "Changed",
}
MAX_FIELD_LEN = 360
TELEGRAM_LIMIT = 4096
DISCLAIMER = "\u203b \uc2dc\uc7a5 \uc815\ubcf4 \uc694\uc57d\u00b7\ud574\uc11d\uc774\uba70 \ub9e4\ub9e4 \uad8c\uc720\uac00 \uc544\ub2d9\ub2c8\ub2e4."


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def cfg(name, default=None, required=False):
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


# --------------------------------------------------------------------------- #
# fetch + parse
# --------------------------------------------------------------------------- #
def fetch_board():
    r = requests.get(
        BOARD_URL, timeout=HTTP_TIMEOUT,
        headers={"User-Agent": "agentnews-digest/1.0"},
    )
    r.raise_for_status()
    return r.text


def parse_frontmatter(text):
    meta = {}
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if m:
        for line in m.group(1).splitlines():
            mm = re.match(r'\s*([\w_]+)\s*:\s*"?(.*?)"?\s*$', line)
            if mm:
                meta[mm.group(1)] = mm.group(2)
    return meta


def clean_md(s):
    if not s:
        return ""
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)   # [text](url) -> text
    s = re.sub(r"(\*\*|__|\*)", "", s)               # drop emphasis markers
    s = s.replace(chr(96), "")                       # mobile copy bug: strip backticks via chr(96)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def truncate(s, n):
    s = s.strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "\u2026"


def slice_board(text):
    start = text.find("## Current now board")
    if start == -1:
        start = 0
    end = len(text)
    for marker in ("## Go deeper", "## For AI agents", "\n---\n## For AI"):
        idx = text.find(marker, start)
        if idx != -1:
            end = min(end, idx)
    return text[start:end]


def extract_frame(text):
    m = re.search(r"## The frame right now\s*\n+\*\*(.+?)\*\*", text, re.DOTALL)
    return clean_md(m.group(1)) if m else ""


def extract_desk_fields(board):
    fields = {}
    for line in board.splitlines():
        mm = re.match(r"-\s*\*\*([\w \-]+?):\*\*\s*(.+)", line)
        if mm:
            fields[mm.group(1).strip()] = clean_md(mm.group(2))
    return fields


def extract_items(board):
    items = []
    pat = re.compile(
        r"^-\s+([\U0001F300-\U0001FAFF\u25A0-\u27BF])\s+\*\*(.+?)\*\*",
        re.MULTILINE,
    )
    for m in pat.finditer(board):
        items.append((m.group(1), clean_md(m.group(2))))
    return items


def extract_watch(board):
    m = re.search(r"\*\*Watch\*\*\s*[\u2014-]\s*(.+)", board, re.DOTALL)
    if not m:
        return ""
    chunk = m.group(1)
    for stop in ("\u00b7 keywords:", "keywords:"):
        i = chunk.find(stop)
        if i != -1:
            chunk = chunk[:i]
            break
    return clean_md(chunk)


def extract_parts(text):
    meta = parse_frontmatter(text)
    board = slice_board(text)
    return {
        "updated": meta.get("updated", ""),
        "next_update": meta.get("next_update", ""),
        "frame": extract_frame(text),
        "desk": extract_desk_fields(board),
        "items": extract_items(board),
        "watch": extract_watch(board),
    }


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def esc(s):
    return html.escape(s or "", quote=False)


def as_of_date(p):
    """Use the board's updated time as 'today'; fall back to real UTC today."""
    updated = (p.get("updated") or "").strip()
    # "2026-07-17T18:15Z" -> "2026-07-17"
    if len(updated) >= 10 and updated[4] == "-" and updated[7] == "-":
        return updated[:10]
    return datetime.now(timezone.utc).date().isoformat()


# --------------------------------------------------------------------------- #
# relay mode (English, no LLM)
# --------------------------------------------------------------------------- #
def build_message_relay(parts):
    lines = [f"\U0001F4C8 <b>\uc624\ub298\uc758 \ubbf8\uad6d \uc2dc\uc7a5 \ube0c\ub9ac\ud551</b>"
             + (f" \u00b7 {esc(parts['updated'])}" if parts["updated"] else "")]
    if parts["next_update"]:
        lines.append(f"\ub2e4\uc74c \uac31\uc2e0: {esc(parts['next_update'])}")

    if parts["frame"]:
        lines += ["", "\U0001F9ED <b>\ud55c \uc904 \uc694\uc57d</b>", esc(parts["frame"])]

    shown = [(DESK_LABELS.get(k, k), parts["desk"][k])
             for k in DESK_FIELDS if k in parts["desk"]]
    if shown:
        lines += ["", "\U0001F50E <b>\uc880 \ub354 \uc790\uc138\ud788</b>"]
        for label, val in shown:
            lines.append(f"\u2022 <b>{esc(label)}:</b> {esc(truncate(val, MAX_FIELD_LEN))}")

    if parts["items"]:
        lines += ["", "\U0001F4CC <b>\uc624\ub298\uc758 \uc8fc\uc694 \ud3ec\uc778\ud2b8</b>"]
        for emoji, h in parts["items"]:
            lines.append(f"{emoji} {esc(truncate(h, 200))}")

    if parts["watch"]:
        lines += ["", "\U0001F440 <b>\uc55e\uc73c\ub85c \uc9c0\ucf1c\ubcfc \uac83</b>", esc(truncate(parts["watch"], 500))]

    lines += ["", '\U0001F517 \uc804\uccb4 \ubcf4\ub4dc: https://agentnews.md/finance']
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------- #
# synthesize mode (Korean + semi/SOXL lens, via Anthropic API)
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = (
    "You turn a compressed macro market board into an EASY Korean briefing for "
    "GENERAL READERS who are NOT finance experts. The reader follows a leveraged "
    "semiconductor ETF (SOXL) but does not know trading-desk jargon.\n\n"
    "Return ONLY a JSON object, no markdown fences, with keys:\n"
    '  "frame_ko": string - one plain-Korean sentence giving the big picture, '
    "based on the FRESH desk frame / items / watch, NOT on STANDING BACKGROUND.\n"
    '  "desk_ko": array of {"label","text"} - rewrite the four desk-frame fields '
    "in plain Korean using THESE friendly labels, in this order:\n"
    '       Held            -> "\uc9c0\uae08 \ud575\uc2ec \ud750\ub984"\n'
    '       Falsifier       -> "\uc774\ub807\uac8c \ub418\uba74 \ud750\ub984\uc774 \ubc14\ub00c\uc5b4\uc694"\n'
    '       Contested       -> "\uc544\uc9c1 \uc758\uacac\uc774 \uac08\ub824\uc694"\n'
    '       Changed since last -> "\uc5b4\uc81c\uc640 \ub2ec\ub77c\uc9c4 \uc810"\n'
    '  "items_ko": array of strings - each main point as ONE easy sentence. '
    "PRESERVE the leading emoji.\n"
    '  "watch_ko": string - what to keep an eye on, in plain Korean.\n'
    '  "semi_soxl_ko": string - 2 to 3 easy sentences on what this setup means '
    "for semiconductors and a leveraged semi ETF like SOXL.\n\n"
    "Writing rules (IMPORTANT):\n"
    "- Write for a smart non-expert. Short, clear, friendly sentences.\n"
    "- The FIRST time a technical term appears, add a tiny parenthetical "
    "explanation, e.g. PCE(\ubbf8\uad6d \ubb3c\uac00\uc9c0\ud45c), 2\ub144\ubb3c \uae08\ub9ac(\uc2dc\uc7a5\uc774 \ubcf4\ub294 \ub2e8\uae30 \uae08\ub9ac \uc804\ub9dd), "
    "\ub2ec\ub7ec\uc778\ub371\uc2a4(\ub2ec\ub7ec\uac00 \uc5bc\ub9c8\ub098 \uac15\ud55c\uc9c0), \ub808\ubc84\ub9ac\uc9c0 ETF(\uc8fc\uac00 \ub4f1\ub77d\uc744 \uba87 \ubc30\ub85c \ud0a4\uc6b4 \uc0c1\ud488). "
    "Keep each explanation very short.\n"
    "- Avoid desk jargon (front-end, repricing, bid, tug-of-war, hawkish). Use "
    "everyday Korean instead (\uc608: \ub2e8\uae30 \uae08\ub9ac, \ub2e4\uc2dc \uac00\uaca9\uc5d0 \ubc18\uc601, \ub9e4\uc218\uc138, \uc904\ub2e4\ub9ac\uae30, "
    "\uae08\ub9ac \uc778\uc0c1\uc5d0 \ubb34\uac8c).\n"
    "- Ground everything ONLY in the provided board. Do NOT invent numbers, "
    "levels, or facts not present.\n"
    "- PRIORITY: the FRESH sections (desk frame / items / watch) outrank "
    "STANDING BACKGROUND. If they conflict, the fresh sections win, and nothing "
    "from STANDING BACKGROUND may be presented as current or upcoming.\n"
    "- DATE AWARENESS: the user message starts with an AS_OF date - treat it as "
    "today. If the board frames a scheduled event (CPI, FOMC, PCE, jobs print, "
    "etc.) whose date is BEFORE AS_OF, it has ALREADY been released: do NOT call "
    "it upcoming and do NOT say the market is 'waiting for' it. Describe it as "
    "already out, and note its result may not be reflected in this board. Only "
    "treat events dated ON or AFTER AS_OF as upcoming.\n"
    "- semi_soxl_ko explains what to watch, it is NOT a trade recommendation. "
    "Never say buy/sell/enter/exit.\n"
    "- Keep total output compact (under ~1400 Korean characters)."
)


def parts_to_plaintext(p):
    lines = [f"AS_OF: {as_of_date(p)} (treat this as today's date)", ""]
    if p["frame"]:
        # The top frame is a 'sticky' narrative not refreshed each window -> may be stale.
        # Use only as long-run context, never for what is current/upcoming.
        lines.append(
            "STANDING BACKGROUND (lower priority, may be stale - do NOT use for "
            "what is current or upcoming; long-run context only): " + p["frame"]
        )
    if p["desk"]:
        lines.append("DESK FRAME (FRESH, this window - PRIMARY SOURCE):")
        for k in DESK_FIELDS:
            if k in p["desk"]:
                lines.append(f"- {k}: {p['desk'][k]}")
    if p["items"]:
        lines.append("ITEMS (FRESH, this window):")
        for emoji, h in p["items"]:
            lines.append(f"{emoji} {h}")
    if p["watch"]:
        lines.append("WATCH (FRESH - use for what to watch next): " + p["watch"])
    return "\n".join(lines)


def call_anthropic(plain_text):
    api_key = cfg("ANTHROPIC_API_KEY", required=True)
    model = cfg("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")
    resp = requests.post(
        ANTHROPIC_API,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 2000,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": plain_text}],
        },
        timeout=LLM_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    return "".join(
        b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
    )


def parse_llm_json(txt):
    s = txt.strip()

    # Strip markdown fences without typing the char directly (mobile copy bug).
    tick3 = chr(96) * 3
    if s.startswith(tick3 + "json"):
        s = s[7:]
    elif s.startswith(tick3):
        s = s[3:]
    if s.endswith(tick3):
        s = s[:-3]

    s = s.strip()
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j != -1:
        s = s[i : j + 1]
    return json.loads(s)


def build_message_ko(obj, parts):
    lines = [f"\U0001F4C8 <b>\uc624\ub298\uc758 \ubbf8\uad6d \uc2dc\uc7a5 \ube0c\ub9ac\ud551</b>"
             + (f" \u00b7 {esc(parts['updated'])}" if parts["updated"] else "")]
    if parts["next_update"]:
        lines.append(f"\ub2e4\uc74c \uac31\uc2e0: {esc(parts['next_update'])}")

    if obj.get("frame_ko"):
        lines += ["", "\U0001F9ED <b>\ud55c \uc904 \uc694\uc57d</b>", esc(obj["frame_ko"])]

    desk = obj.get("desk_ko") or []
    if desk:
        lines += ["", "\U0001F50E <b>\uc880 \ub354 \uc790\uc138\ud788</b>"]
        for d in desk:
            label = esc(str(d.get("label", "")))
            text = esc(truncate(str(d.get("text", "")), MAX_FIELD_LEN))
            lines.append(f"\u2022 <b>{label}:</b> {text}")

    items = obj.get("items_ko") or []
    if items:
        lines += ["", "\U0001F4CC <b>\uc624\ub298\uc758 \uc8fc\uc694 \ud3ec\uc778\ud2b8</b>"]
        for it in items:
            lines.append(esc(truncate(str(it), 220)))

    if obj.get("semi_soxl_ko"):
        lines += ["", "\U0001F9EE <b>\ubc18\ub3c4\uccb4 \u00b7 SOXL \uad00\uc810</b>",
                  esc(truncate(str(obj["semi_soxl_ko"]), 900))]

    if obj.get("watch_ko"):
        lines += ["", "\U0001F440 <b>\uc55e\uc73c\ub85c \uc9c0\ucf1c\ubcfc \uac83</b>",
                  esc(truncate(str(obj["watch_ko"]), 500))]

    lines += ["", f"<i>{esc(DISCLAIMER)}</i>",
              '\U0001F517 \uc804\uccb4 \ubcf4\ub4dc: https://agentnews.md/finance']
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------- #
# telegram
# --------------------------------------------------------------------------- #
def split_message(text, limit=TELEGRAM_LIMIT):
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for para in text.split("\n"):
        if len(cur) + len(para) + 1 > limit:
            chunks.append(cur)
            cur = para
        else:
            cur = f"{cur}\n{para}" if cur else para
    if cur:
        chunks.append(cur)
    return chunks


def send_telegram(text):
    token = cfg("TELEGRAM_BOT_TOKEN", required=True)
    chat_id = cfg("TELEGRAM_CHAT_ID", required=True)
    url = TELEGRAM_API.format(token=token)
    for chunk in split_message(text):
        resp = requests.post(
            url,
            json={
                "chat_id": chat_id, "text": chunk,
                "parse_mode": "HTML", "disable_web_page_preview": True,
            },
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()


# --------------------------------------------------------------------------- #
# core
# --------------------------------------------------------------------------- #
def build_digest(mode):
    text = fetch_board()
    parts = extract_parts(text)
    if mode == "relay":
        return build_message_relay(parts), {"mode": "relay"}
    # synthesize
    raw = call_anthropic(parts_to_plaintext(parts))
    try:
        obj = parse_llm_json(raw)
        return build_message_ko(obj, parts), {"mode": "synthesize"}
    except Exception:
        # fall back to relay if the model returned something unparseable
        return build_message_relay(parts), {"mode": "relay_fallback"}


# --------------------------------------------------------------------------- #
# web server mode (legacy) - Flask imported lazily so the Actions run,
# which never serves HTTP, does not require Flask to be installed.
# --------------------------------------------------------------------------- #
def run_web_server():
    from flask import Flask, request, jsonify

    app = Flask(__name__)

    @app.route("/health")
    def health():
        return jsonify({"ok": True})

    @app.route("/digest", methods=["GET", "POST"])
    def digest():
        key = request.args.get("key") or request.headers.get("X-Run-Key")
        expected = cfg("RUN_KEY")
        if expected and key != expected:
            return jsonify({"ok": False, "error": "unauthorized"}), 401

        mode = (request.args.get("mode") or cfg("DIGEST_MODE", "synthesize")).lower()
        if mode not in ("relay", "synthesize"):
            mode = "synthesize"

        try:
            msg, info = build_digest(mode)
        except Exception as e:
            error_msg = str(e)[:200] + "..." if len(str(e)) > 200 else str(e)
            return jsonify({"ok": False, "stage": "build", "error": error_msg}), 500

        if request.args.get("dry") in ("1", "true", "yes"):
            return jsonify({"ok": True, "dry_run": True, "info": info, "message": msg})

        try:
            send_telegram(msg)
        except Exception as e:
            error_msg = str(e)[:200] + "..." if len(str(e)) > 200 else str(e)
            return jsonify({"ok": False, "stage": "send", "error": error_msg}), 500

        return jsonify({"ok": True, "sent": True, "info": info})

    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)


# --------------------------------------------------------------------------- #
# Execution Entry Point
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("GitHub Actions \ubaa8\ub4dc\ub85c \uc790\ub3d9 \uc2e4\ud589\uc744 \uc2dc\uc791\ud569\ub2c8\ub2e4...")
        try:
            mode = os.environ.get("DIGEST_MODE", "synthesize").lower()
            if mode not in ("relay", "synthesize"):
                mode = "synthesize"

            print(f"\ub370\uc774\ud130 \uc218\uc9d1 \ubc0f \ubd84\uc11d \uc911... (\ubaa8\ub4dc: {mode})")
            msg, info = build_digest(mode)

            print("\ud154\ub808\uadf8\ub7a8\uc73c\ub85c \uba54\uc2dc\uc9c0 \ubc1c\uc1a1 \uc911...")
            send_telegram(msg)

            print(f"\uc791\uc5c5 \uc644\ub8cc! \ud154\ub808\uadf8\ub7a8\uc744 \ud655\uc778\ud574 \uc8fc\uc138\uc694. (\uacb0\uacfc \uc0c1\ud0dc: {info.get('mode')})")

        except Exception as e:
            print(f"\uc2e4\ud589 \uc911 \uc5d0\ub7ec \ubc1c\uc0dd: {e}")
            raise e

    else:
        print("\uc6f9 \uc11c\ubc84 \ubaa8\ub4dc\ub85c \uc2e4\ud589\ud569\ub2c8\ub2e4...")
        run_web_server()
