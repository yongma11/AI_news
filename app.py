"""
agentnews -> Telegram daily macro digest

Two modes (env DIGEST_MODE, or ?mode= on the request):
  relay       : compress the board and forward as-is (English, no LLM)
  synthesize  : translate to Korean + add a semiconductor/SOXL lens via the
                Anthropic API  (default)

Flow:
  cron-job.org --(GET /digest?key=RUN_KEY)--> this server
      -> fetch https://agentnews.md/finance.md
      -> compress (frame / desk frame / item headlines / watch)
      -> [synthesize] Claude API: Korean + 반도체/SOXL 관점
      -> send to your Telegram chat

Secrets are read from environment variables only. Never hard-code them.
  TELEGRAM_BOT_TOKEN   from @BotFather
  TELEGRAM_CHAT_ID     your chat id (see README)
  RUN_KEY              a random string you choose, to protect the endpoint
  ANTHROPIC_API_KEY    only needed for synthesize mode
  ANTHROPIC_MODEL      optional, default claude-sonnet-4-6
  DIGEST_MODE          'synthesize' (default) or 'relay'
"""

import os
import re
import json
import html
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

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
DISCLAIMER = "※ 시장 정보 요약·해석이며 매매 권유가 아닙니다."


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
    s = re.sub(r"(\*\*|__|\*|`)", "", s)             # drop emphasis markers
    s = re.sub(r"\s+", " ", s).strip()
    return s


def truncate(s, n):
    s = s.strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "\u2026"


def slice_board(text):
    """Live 'Current now board' region only.

    Stops before '## Go deeper' / '## For AI agents' so the agent-directed
    footer (e.g. '/install') is never relayed or fed to the LLM.
    """
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
    m = re.search(r"\*\*Watch\*\*\s*[—-]\s*(.+)", board, re.DOTALL)
    if not m:
        return ""
    chunk = m.group(1)
    for stop in ("· keywords:", "keywords:"):
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


# --------------------------------------------------------------------------- #
# relay mode (English, no LLM)
# --------------------------------------------------------------------------- #
def build_message_relay(parts):
    lines = [f"\U0001F5D3 <b>거시 브리핑</b>"
             + (f" · {esc(parts['updated'])}" if parts["updated"] else "")]
    if parts["next_update"]:
        lines.append(f"다음 갱신: {esc(parts['next_update'])}")

    if parts["frame"]:
        lines += ["", "\U0001F9ED <b>프레임</b>", esc(parts["frame"])]

    shown = [(DESK_LABELS.get(k, k), parts["desk"][k])
             for k in DESK_FIELDS if k in parts["desk"]]
    if shown:
        lines += ["", "\U0001F6E0 <b>Desk frame</b>"]
        for label, val in shown:
            lines.append(f"• <b>{esc(label)}:</b> {esc(truncate(val, MAX_FIELD_LEN))}")

    if parts["items"]:
        lines += ["", "\U0001F4CC <b>핵심 항목</b>"]
        for emoji, h in parts["items"]:
            lines.append(f"{emoji} {esc(truncate(h, 200))}")

    if parts["watch"]:
        lines += ["", "\U0001F440 <b>Watch</b>", esc(truncate(parts["watch"], 500))]

    lines += ["", '\U0001F517 전체 보드: https://agentnews.md/finance']
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------- #
# synthesize mode (Korean + 반도체/SOXL 관점, via Anthropic API)
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = (
    "You translate and lightly interpret a compressed macro market board for a "
    "Korean reader who trades a leveraged semiconductor ETF (SOXL) on a "
    "rule-based system.\n\n"
    "Return ONLY a JSON object, no markdown fences, with keys:\n"
    '  "frame_ko": string  - the top-line frame in natural Korean (1 sentence).\n'
    '  "desk_ko": array of {"label","text"}  - translate each desk-frame field '
    "to Korean. Keep labels as given (Held/Falsifier/Contested/Changed).\n"
    '  "items_ko": array of strings  - translate each item headline to Korean. '
    "PRESERVE the leading emoji.\n"
    '  "watch_ko": string  - the watch threads in Korean.\n'
    '  "semi_soxl_ko": string  - 2 to 4 sentences. Interpret what THIS macro '
    "setup (front-end/rates, dollar, AI bid, oil) implies for the semiconductor "
    "complex and a leveraged semi ETF like SOXL. Focus on the volatility regime "
    "and the rates/dollar vs AI-bid tug-of-war.\n\n"
    "Rules:\n"
    "- Natural, concise Korean. Common financial terms may stay as-is "
    "(2년물, 달러인덱스, PCE).\n"
    "- Ground everything ONLY in the provided board. Do NOT invent numbers, "
    "levels, or facts not present.\n"
    "- semi_soxl_ko is interpretation of implications, NOT a trade "
    "recommendation. Never say buy/sell/enter/exit. Frame as what to watch.\n"
    "- Keep total output compact (under ~1500 Korean characters)."
)


def parts_to_plaintext(p):
    lines = []
    if p["frame"]:
        lines.append("FRAME: " + p["frame"])
    if p["desk"]:
        lines.append("DESK FRAME:")
        for k in DESK_FIELDS:
            if k in p["desk"]:
                lines.append(f"- {k}: {p['desk'][k]}")
    if p["items"]:
        lines.append("ITEMS:")
        for emoji, h in p["items"]:
            lines.append(f"{emoji} {h}")
    if p["watch"]:
        lines.append("WATCH: " + p["watch"])
    return "\n".join(lines)


def call_anthropic(plain_text):
    api_key = cfg("ANTHROPIC_API_KEY", required=True)
    model = cfg("ANTHROPIC_MODEL", "claude-sonnet-4-6")
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
    s = re.sub(r"^```(?:json)?", "", s).strip()
    s = re.sub(r"```$", "", s).strip()
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j != -1:
        s = s[i : j + 1]
    return json.loads(s)


def build_message_ko(obj, parts):
    lines = [f"\U0001F5D3 <b>거시 브리핑</b>"
             + (f" · {esc(parts['updated'])}" if parts["updated"] else "")]
    if parts["next_update"]:
        lines.append(f"다음 갱신: {esc(parts['next_update'])}")

    if obj.get("frame_ko"):
        lines += ["", "\U0001F9ED <b>프레임</b>", esc(obj["frame_ko"])]

    desk = obj.get("desk_ko") or []
    if desk:
        lines += ["", "\U0001F6E0 <b>Desk frame</b>"]
        for d in desk:
            label = esc(str(d.get("label", "")))
            text = esc(truncate(str(d.get("text", "")), MAX_FIELD_LEN))
            lines.append(f"• <b>{label}:</b> {text}")

    items = obj.get("items_ko") or []
    if items:
        lines += ["", "\U0001F4CC <b>핵심 항목</b>"]
        for it in items:
            lines.append(esc(truncate(str(it), 220)))

    if obj.get("semi_soxl_ko"):
        lines += ["", "\U0001F9EE <b>반도체 · SOXL 관점</b>",
                  esc(truncate(str(obj["semi_soxl_ko"]), 900))]

    if obj.get("watch_ko"):
        lines += ["", "\U0001F440 <b>Watch</b>",
                  esc(truncate(str(obj["watch_ko"]), 500))]

    lines += ["", f"<i>{esc(DISCLAIMER)}</i>",
              '\U0001F517 전체 보드: https://agentnews.md/finance']
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
# routes
# --------------------------------------------------------------------------- #
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
        return jsonify({"ok": False, "stage": "build", "error": str(e)}), 500

    if request.args.get("dry") in ("1", "true", "yes"):
        return jsonify({"ok": True, "dry_run": True, "info": info, "message": msg})

    try:
        send_telegram(msg)
    except Exception as e:
        return jsonify({"ok": False, "stage": "send", "error": str(e)}), 500

    return jsonify({"ok": True, "sent": True, "info": info})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
