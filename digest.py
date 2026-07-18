"""
agentnews -> Telegram daily macro digest

Two modes (env DIGEST_MODE, or ?mode= on the request):
  relay       : compress the board and forward as-is (English, no LLM)
  synthesize  : translate to Korean + add a semiconductor/SOXL lens via the
                Anthropic API  (default)
"""

import os
import re
import json
import html
import requests
from datetime import datetime, timezone
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
    s = re.sub(r"(\*\*|__|\*)", "", s)               # drop emphasis markers
    s = s.replace(chr(96), "")                       # 모바일 복사 버그 방지를 위해 chr(96)으로 백틱 제거
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


def as_of_date(p):
    """보드의 updated 시각을 기준일(오늘)로 사용. 없으면 실제 UTC 오늘로 폴백."""
    updated = (p.get("updated") or "").strip()
    # "2026-07-17T18:15Z" -> "2026-07-17"
    if len(updated) >= 10 and updated[4] == "-" and updated[7] == "-":
        return updated[:10]
    return datetime.now(timezone.utc).date().isoformat()


# --------------------------------------------------------------------------- #
# relay mode (English, no LLM)
# --------------------------------------------------------------------------- #
def build_message_relay(parts):
    lines = [f"\U0001F4C8 <b>오늘의 미국 시장 브리핑</b>"
             + (f" · {esc(parts['updated'])}" if parts["updated"] else "")]
    if parts["next_update"]:
        lines.append(f"다음 갱신: {esc(parts['next_update'])}")

    if parts["frame"]:
        lines += ["", "\U0001F9ED <b>한 줄 요약</b>", esc(parts["frame"])]

    shown = [(DESK_LABELS.get(k, k), parts["desk"][k])
             for k in DESK_FIELDS if k in parts["desk"]]
    if shown:
        lines += ["", "\U0001F50E <b>좀 더 자세히</b>"]
        for label, val in shown:
            lines.append(f"• <b>{esc(label)}:</b> {esc(truncate(val, MAX_FIELD_LEN))}")

    if parts["items"]:
        lines += ["", "\U0001F4CC <b>오늘의 주요 포인트</b>"]
        for emoji, h in parts["items"]:
            lines.append(f"{emoji} {esc(truncate(h, 200))}")

    if parts["watch"]:
        lines += ["", "\U0001F440 <b>앞으로 지켜볼 것</b>", esc(truncate(parts["watch"], 500))]

    lines += ["", '\U0001F517 전체 보드: https://agentnews.md/finance']
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------- #
# synthesize mode (Korean + 반도체/SOXL 관점, via Anthropic API)
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
    '       Held            -> "지금 핵심 흐름"\n'
    '       Falsifier       -> "이렇게 되면 흐름이 바뀌어요"\n'
    '       Contested       -> "아직 의견이 갈려요"\n'
    '       Changed since last -> "어제와 달라진 점"\n'
    '  "items_ko": array of strings - each main point as ONE easy sentence. '
    "PRESERVE the leading emoji.\n"
    '  "watch_ko": string - what to keep an eye on, in plain Korean.\n'
    '  "semi_soxl_ko": string - 2 to 3 easy sentences on what this setup means '
    "for semiconductors and a leveraged semi ETF like SOXL.\n\n"
    "Writing rules (IMPORTANT):\n"
    "- Write for a smart non-expert. Short, clear, friendly sentences.\n"
    "- The FIRST time a technical term appears, add a tiny parenthetical "
    "explanation, e.g. PCE(미국 물가지표), 2년물 금리(시장이 보는 단기 금리 전망), "
    "달러인덱스(달러가 얼마나 강한지), 레버리지 ETF(주가 등락을 몇 배로 키운 상품). "
    "Keep each explanation very short.\n"
    "- Avoid desk jargon (front-end, repricing, bid, tug-of-war, hawkish). Use "
    "everyday Korean instead (예: 단기 금리, 다시 가격에 반영, 매수세, 줄다리기, "
    "금리 인상에 무게).\n"
    "- Ground everything ONLY in the provided board. Do NOT invent numbers, "
    "levels, or facts not present.\n"
    "- PRIORITY: the FRESH sections (desk frame / items / watch) outrank "
    "STANDING BACKGROUND. If they conflict, the fresh sections win, and nothing "
    "from STANDING BACKGROUND may be presented as current or upcoming.\n"
    "- DATE AWARENESS: the user message starts with an AS_OF date — treat it as "
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
        # 상단 프레임은 매 윈도우 갱신되지 않는 'sticky' 서술이라 낡을 수 있음.
        # 최신 여부 판단·관전포인트에는 쓰지 말고 장기 맥락 배경으로만.
        lines.append(
            "STANDING BACKGROUND (lower priority, may be stale — do NOT use for "
            "what is current or upcoming; long-run context only): " + p["frame"]
        )
    if p["desk"]:
        lines.append("DESK FRAME (FRESH, this window — PRIMARY SOURCE):")
        for k in DESK_FIELDS:
            if k in p["desk"]:
                lines.append(f"- {k}: {p['desk'][k]}")
    if p["items"]:
        lines.append("ITEMS (FRESH, this window):")
        for emoji, h in p["items"]:
            lines.append(f"{emoji} {h}")
    if p["watch"]:
        lines.append("WATCH (FRESH — use for what to watch next): " + p["watch"])
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

    # 모바일 복사 버그를 피하기 위해 특수기호를 직접 쓰지 않고 우회해서 마크다운 기호를 제거합니다.
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
    lines = [f"\U0001F4C8 <b>오늘의 미국 시장 브리핑</b>"
             + (f" · {esc(parts['updated'])}" if parts["updated"] else "")]
    if parts["next_update"]:
        lines.append(f"다음 갱신: {esc(parts['next_update'])}")

    if obj.get("frame_ko"):
        lines += ["", "\U0001F9ED <b>한 줄 요약</b>", esc(obj["frame_ko"])]

    desk = obj.get("desk_ko") or []
    if desk:
        lines += ["", "\U0001F50E <b>좀 더 자세히</b>"]
        for d in desk:
            label = esc(str(d.get("label", "")))
            text = esc(truncate(str(d.get("text", "")), MAX_FIELD_LEN))
            lines.append(f"• <b>{label}:</b> {text}")

    items = obj.get("items_ko") or []
    if items:
        lines += ["", "\U0001F4CC <b>오늘의 주요 포인트</b>"]
        for it in items:
            lines.append(esc(truncate(str(it), 220)))

    if obj.get("semi_soxl_ko"):
        lines += ["", "\U0001F9EE <b>반도체 · SOXL 관점</b>",
                  esc(truncate(str(obj["semi_soxl_ko"]), 900))]

    if obj.get("watch_ko"):
        lines += ["", "\U0001F440 <b>앞으로 지켜볼 것</b>",
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


# --------------------------------------------------------------------------- #
# Execution Entry Point
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("GitHub Actions 모드로 자동 실행을 시작합니다...")
        try:
            mode = os.environ.get("DIGEST_MODE", "synthesize").lower()
            if mode not in ("relay", "synthesize"):
                mode = "synthesize"

            print(f"데이터 수집 및 분석 중... (모드: {mode})")
            msg, info = build_digest(mode)

            print("텔레그램으로 메시지 발송 중...")
            send_telegram(msg)

            print(f"작업 완료! 텔레그램을 확인해 주세요. (결과 상태: {info.get('mode')})")

        except Exception as e:
            print(f"실행 중 에러 발생: {e}")
            raise e

    else:
        print("웹 서버 모드로 실행합니다...")
        port = int(os.environ.get("PORT", "8080"))
        app.run(host="0.0.0.0", port=port)
