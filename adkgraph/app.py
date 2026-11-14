"""ISTTS Insurance Customer Care: Gradio UI for the ADK 2.0 claims graph.

    python app.py   # Gemini (needs GOOGLE_API_KEY)

Left: the customer chat with Nara. Right: the claims back office, with the graph
lighting up node by node, the adjuster panel (shown when the graph pauses for
approval), and session state. On a phone the two panels stack.
"""

import json
import logging
import os
import re
import uuid

import gradio as gr
from dotenv import load_dotenv

load_dotenv(".env")            # project root
load_dotenv("claimflow/.env")  # the settings file that adk web reads

from google.adk import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.genai import types  # noqa: E402

from claimflow.agent import root_agent  # noqa: E402
from claimflow.agents import MODEL  # noqa: E402
from claimflow.nodes import FraudServiceUnavailable  # noqa: E402
from claimflow.schemas import FIELD_LABELS, rupiah  # noqa: E402



class QuietDemoFailure(logging.Filter):
    """fraud_check fails once on purpose (CLAIMFLOW_FLAKY) to show retries; ADK logs
    every node failure with a full traceback, so shorten that one to a single line."""

    def filter(self, record):
        exc = record.exc_info[1] if record.exc_info else None
        if isinstance(exc, FraudServiceUnavailable):
            record.msg, record.args, record.exc_info = f"[demo] fraud_check: {exc}", (), None
        return True


logging.getLogger("google_adk.google.adk.workflow._node_runner").addFilter(QuietDemoFailure())

APP, USER = "claimflow", "customer"
SESSIONS = InMemorySessionService()
RUNNER = Runner(app_name=APP, agent=root_agent, session_service=SESSIONS)

# Nodes whose messages go to the customer chat; everything else goes to the trace.
CHAT_NODES = {"status_lookup", "manual_queue", "send_letter", "escalate_letter"}

# ── Graph drawing ────────────────────────────────────────────────────────────
# name: (x, y, kind, label)
NODES = {
    "START":            (300, 22, "end", "START"),
    "triage_agent":     (300, 64, "llm", "triage_agent"),
    "route_request":    (300, 106, "fn", "route_request"),
    "status_lookup":    (500, 148, "fn", "status_lookup"),
    "intake_agent":     (300, 148, "llm", "intake_agent"),
    "validate_claim":   (300, 190, "fn", "validate_claim"),
    "ask_customer":     (95, 190, "human", "ask_customer"),
    "merge_reply":      (95, 232, "fn", "merge_reply"),
    "manual_queue":     (500, 190, "fn", "manual_queue"),
    "start_assessment": (300, 240, "fn", "start_assessment"),
    "fraud_check":      (125, 290, "fn", "fraud_check ↻"),
    "coverage_check":   (300, 290, "fn", "coverage_check"),
    "damage_estimator": (475, 290, "llm", "damage_estimator"),
    "gather":           (300, 340, "join", "gather (JoinNode)"),
    "decide":           (300, 384, "fn", "decide"),
    "adjuster_review":  (300, 428, "human", "adjuster_review"),
    "after_review":     (300, 470, "fn", "after_review"),
    "pay_claim":        (110, 470, "fn", "pay_claim"),
    "deny_claim":       (490, 470, "fn", "deny_claim"),
    "letter_writer":    (300, 520, "llm", "letter_writer"),
    "letter_critic":    (300, 562, "llm", "letter_critic"),
    "after_critic":     (300, 604, "fn", "after_critic"),
    "send_letter":      (175, 650, "fn", "send_letter"),
    "escalate_letter":  (425, 650, "fn", "escalate_letter"),
}
# (from, to, route label or None)
EDGES = [
    ("START", "triage_agent", None), ("triage_agent", "route_request", None),
    ("route_request", "intake_agent", "NEW_CLAIM"), ("route_request", "status_lookup", "STATUS"),
    ("intake_agent", "validate_claim", None),
    ("validate_claim", "ask_customer", "ASK"), ("ask_customer", "merge_reply", None),
    ("merge_reply", "validate_claim", None), ("validate_claim", "manual_queue", "MANUAL"),
    ("validate_claim", "start_assessment", "OK"),
    ("start_assessment", "fraud_check", None), ("start_assessment", "coverage_check", None),
    ("start_assessment", "damage_estimator", None),
    ("fraud_check", "gather", None), ("coverage_check", "gather", None),
    ("damage_estimator", "gather", None), ("gather", "decide", None),
    ("decide", "pay_claim", "AUTO"), ("decide", "adjuster_review", "REVIEW"),
    ("decide", "deny_claim", "DENY"), ("adjuster_review", "after_review", None),
    ("after_review", "pay_claim", "APPROVED"), ("after_review", "deny_claim", "REJECTED"),
    ("pay_claim", "letter_writer", None), ("deny_claim", "letter_writer", None),
    ("letter_writer", "letter_critic", None), ("letter_critic", "after_critic", None),
    ("after_critic", "letter_writer", "REVISE"), ("after_critic", "send_letter", "PASS"),
    ("after_critic", "escalate_letter", "ESCALATE"),
]
COLORS = {"llm": ("#C3ECF6", "#4285F4"), "human": ("#F8D8D8", "#EA4335"),
          "fn": ("#EEEEEE", "#5F6368"), "join": ("#FFE7A5", "#F9AB00"),
          "end": ("#1E1E1E", "#1E1E1E")}
W, H = 132, 28


def graph_svg(st: dict) -> str:
    visited, routes = set(st["visited"]), st["routes"]
    waiting = st["pending"]["node"] if st["pending"] else None
    current = st["order"][-1] if st["order"] else None
    parts = ['<svg viewBox="0 0 600 672" width="100%" style="max-width:600px;font-family:'
             'Google Sans,Roboto,Arial,sans-serif">',
             '<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" '
             'markerHeight="6" orient="auto"><path d="M0,0L10,5L0,10z" fill="#9AA0A6"/></marker>'
             '<marker id="ahOn" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" '
             'markerHeight="6" orient="auto"><path d="M0,0L10,5L0,10z" fill="#1A73E8"/></marker>'
             '</defs><style>@keyframes p{50%{stroke-opacity:.25}}.wait{animation:p 1s infinite}</style>']
    for a, b, label in EDGES:
        (x1, y1, *_), (x2, y2, *_) = NODES[a], NODES[b]
        on = a in visited and b in visited and (label is None or label in routes.get(a, []))
        color, width = ("#1A73E8", 2.2) if on else ("#C8CBD0", 1.2)
        marker = "ahOn" if on else "ah"
        if (a, b) == ("after_critic", "letter_writer"):          # back-edge on the right
            d = f"M{x1 + W / 2},{y1} C{x1 + 140},{y1} {x2 + 140},{y2} {x2 + W / 2},{y2}"
            lx, ly, anchor = x1 + 112, (y1 + y2) / 2, "start"
        elif (a, b) == ("merge_reply", "validate_claim"):
            d = f"M{x1 + W / 2},{y1} L{x2 - W / 2},{y2 + 6}"
            lx, ly, anchor = 0, 0, "start"
        else:
            if y2 > y1:      # downward edge: bottom of source to top of target
                sx, sy, ex, ey = x1, y1 + H / 2, x2, y2 - H / 2
            else:            # sideways edge: side of source to side of target
                side = W / 2 if x2 > x1 else -W / 2
                sx, sy, ex, ey = x1 + side, y1, x2 - side, y2
            d = f"M{sx},{sy} L{ex},{ey}"
            if y2 > y1:
                t = 0.3 if x1 == x2 else 0.38
                lx, ly = sx + (ex - sx) * t, sy + (ey - sy) * t + 4
                lx, anchor = (lx - 6, "end") if ex < sx else (lx + 5, "start")
            else:
                lx, ly, anchor = (sx + ex) / 2, sy - 5, "middle"
        parts.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}" '
                     f'marker-end="url(#{marker})"/>')
        if label:
            parts.append(f'<text x="{lx}" y="{ly}" font-size="9" text-anchor="{anchor}" '
                         f'fill="{"#1A73E8" if on else "#9AA0A6"}" '
                         f'font-family="Roboto Mono,monospace">{label}</text>')
    for name, (x, y, kind, label) in NODES.items():
        fill, stroke = COLORS[kind]
        seen = name in visited or name == "START"
        if not seen:
            fill, stroke = "#FFFFFF", "#DADCE0"
        text = "#FFFFFF" if kind == "end" else ("#1E1E1E" if seen else "#9AA0A6")
        sw = 3 if name in (current, waiting) else 1.2
        cls = ' class="wait"' if name == waiting else ""
        parts.append(f'<rect{cls} x="{x - W / 2}" y="{y - H / 2}" width="{W}" height="{H}" rx="8" '
                     f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')
        parts.append(f'<text x="{x}" y="{y + 4}" font-size="11" text-anchor="middle" '
                     f'fill="{text}">{label}</text>')
    parts.append("</svg>")
    legend = ('<div style="font-size:12px;color:#5F6368;margin-top:4px">'
              '<span style="color:#4285F4">■</span> LLM agent &nbsp; '
              '<span style="color:#EA4335">■</span> human input &nbsp; '
              '<span style="color:#5F6368">■</span> Python function &nbsp; '
              '<span style="color:#F9AB00">■</span> JoinNode &nbsp; '
              '<span style="color:#1A73E8">━</span> path taken</div>')
    return "".join(parts) + legend


def trace_html(st: dict) -> str:
    if not st["trace"]:
        return "<div style='color:#9AA0A6'>No nodes have run yet.</div>"
    rows = []
    for icon, node, detail in st["trace"][-40:]:
        rows.append(f"<div style='font-size:13px;padding:2px 0'>{icon} <code>{node}</code>"
                    f"{(' — ' + detail) if detail else ''}</div>")
    return "<div style='max-height:260px;overflow-y:auto'>" + "".join(reversed(rows)) + "</div>"


# ── Free-text replies to ask_customer ────────────────────────────────────────
MONTHS = {m: i for i, names in enumerate([
    ("januari", "january", "jan"), ("februari", "february", "feb"), ("maret", "march", "mar"),
    ("april", "apr"), ("mei", "may"), ("juni", "june", "jun"), ("juli", "july", "jul"),
    ("agustus", "august", "agu", "aug"), ("september", "sep"), ("oktober", "october", "okt", "oct"),
    ("november", "nov"), ("desember", "december", "des", "dec")], 1) for m in names}
KEYWORDS = {
    "incident_type": {"vehicle": ("mobil", "motor", "kendaraan", "car", "vehicle"),
                      "property": ("rumah", "toko", "properti", "house", "property"),
                      "travel": ("perjalanan", "travel")},
    "incident_cause": {"collision": ("tabrak", "serempet", "collision"),
                       "theft": ("curi", "theft", "stolen"),
                       "flood": ("banjir", "flood"), "fire": ("bakar", "fire")},
}


def parse_free_text(text: str) -> dict:
    """Pulls policy number, date, type, cause and amount out of a short reply."""
    low, out = text.lower(), {}
    if m := re.search(r"POL-\d+", text, re.I):
        out["policy_id"] = m.group(0).upper()
    if m := re.search(r"\d{4}-\d{2}-\d{2}", text):
        out["incident_date"] = m.group(0)
    elif m := re.search(r"(\d{1,2})\s+([a-z]+)\s+(\d{4})", low):
        if m.group(2) in MONTHS:
            out["incident_date"] = f"{m.group(3)}-{MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}"
    for field, options in KEYWORDS.items():
        for value, words in options.items():
            if any(w in low for w in words):
                out[field] = value
                break
    if m := re.search(r"rp\.?\s*([\d.,]+)\s*(juta|jt|million|miliar|m)?\b", low):
        num, unit = m.group(1), m.group(2)
        if unit:   # "8,5 juta": comma or dot is the decimal mark
            value = float(num.replace(".", "").replace(",", ".") if "," in num else num)
            value *= 1_000_000_000 if unit == "miliar" else 1_000_000
        else:      # "8.500.000": dots or commas group thousands
            value = float(re.sub(r"[.,]", "", num))
        out["amount_claimed"] = value
    return out


# ── Session state and graph driver ───────────────────────────────────────────
def new_state() -> dict:
    return {"session_id": None, "pending": None, "visited": ["START"], "order": [],
            "routes": {}, "trace": []}


def node_of(event) -> tuple[str, str]:
    path = (event.node_info.path if event.node_info else None) or event.author or ""
    leaf = path.split("/")[-1]
    name, _, run = leaf.partition("@")
    return name, run


def text_of(event) -> str:
    if not (event.content and event.content.parts):
        return ""
    return "".join(p.text or "" for p in event.content.parts if not p.thought).strip()


async def _drive(st: dict, chat: list, content: types.Content, invocation_id=None):
    """Runs the graph until it ends or pauses; yields after every event."""
    if not st["session_id"]:
        session = await SESSIONS.create_session(app_name=APP, user_id=USER)
        st["session_id"] = session.id
    st["pending"] = None
    async for event in RUNNER.run_async(user_id=USER, session_id=st["session_id"],
                                        invocation_id=invocation_id, new_message=content):
        name, run = node_of(event)
        if name not in NODES:
            continue
        tag = f"{name} (round {run})" if run not in ("", "1") else name
        if name not in st["visited"]:
            st["visited"].append(name)
        st["order"].append(name)
        if event.long_running_tool_ids:
            for part in event.content.parts:
                fc = part.function_call
                if fc and fc.id in event.long_running_tool_ids:
                    args = fc.args or {}
                    st["pending"] = {"id": fc.id, "name": fc.name, "node": name,
                                     "invocation_id": event.invocation_id,
                                     "payload": args.get("payload"),
                                     "message": args.get("message", "")}
            st["trace"].append(("⏸", tag, "waiting for human input"))
            if name == "ask_customer":
                chat.append({"role": "assistant",
                             "content": st["pending"]["message"].split(". Reply")[0] + "."})
            else:
                chat.append({"role": "assistant", "content":
                             "Your claim is waiting for adjuster approval. We will"
                             " update you once a decision is made."})
        elif event.error_code:
            st["trace"].append(("↻", tag, f"{event.error_code}: retrying"))
        elif event.actions and event.actions.route is not None:
            route = str(event.actions.route)
            st["routes"].setdefault(name, [])
            if route not in st["routes"][name]:
                st["routes"][name].append(route)
            st["trace"].append(("↳", tag, f"route → <b>{route}</b>"))
        elif (text := text_of(event)) and event.author != "user" and not text.startswith("{"):
            if name in CHAT_NODES:
                chat.append({"role": "assistant", "content": text})
                st["trace"].append(("💬", tag, "message to customer"))
            elif name == "letter_writer":
                st["trace"].append(("📝", tag, "letter draft written"))
            else:
                st["trace"].append(("💬", tag, text[:90]))
        elif event.output is not None:
            st["trace"].append(("✓", tag, ""))
        yield


async def drive(st: dict, chat: list, content: types.Content, invocation_id=None):
    """Like _drive, but a failure becomes a polite chat message, not a broken UI."""
    try:
        async for _ in _drive(st, chat, content, invocation_id):
            yield
    except Exception as exc:  # noqa: BLE001 - shown to the presenter in the trace
        st["pending"] = None
        st["trace"].append(("⚠️", "error", f"{type(exc).__name__}: {str(exc)[:120]}"))
        chat.append({"role": "assistant", "content": "Sorry, our system is having trouble right"
                     " now. Please try again in a moment, or call our 24/7 hotline +62315027920."})
        yield


async def session_state(st: dict) -> dict:
    if not st["session_id"]:
        return {}
    s = await SESSIONS.get_session(app_name=APP, user_id=USER, session_id=st["session_id"])
    keys = ["claim", "assessment", "adjuster", "outcome", "attempts", "letter_rounds"]
    return {k: s.state[k] for k in keys if k in s.state}


def function_response(st: dict, response: dict) -> tuple[types.Content, str]:
    p = st["pending"]
    content = types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
        id=p["id"], name=p["name"], response=response))])
    return content, p["invocation_id"]


# ── UI updates ───────────────────────────────────────────────────────────────
GREETING = ("Hello, welcome to **ISTTS Insurance Customer Care**! 👋\n\n"
            "I'm Nara, your claims assistant. Tell me what happened and I'll open a claim"
            " for you, or give me your claim number (e.g. CLM-0042) to check its status.")


def greeting() -> list:
    return [{"role": "assistant", "content": GREETING}]


def status_html(kind: str) -> str:
    label = {"adjuster": "Waiting for adjuster approval", "customer": "Waiting for customer details",
             "ready": "Ready"}[kind]
    return f"<div class='pill pill-{kind}'><span class='dot'></span>{label}</div>"


def adjuster_html(payload: dict | None) -> str:
    if not payload:
        return ""
    f, c, d = payload["fraud"], payload["coverage"], payload["damage"]
    flags = "".join(f"<li>{x}</li>" for x in f["flags"]) or "<li>none</li>"
    risk = "high" if f["score"] >= 0.6 else "low"
    return (f"<div class='adj-amount'>{rupiah(payload['payable'])}<span>proposed payment</span></div>"
            "<div class='adj-grid'>"
            f"<div><span>Damage estimate</span><b>{rupiah(d['estimate'])}</b></div>"
            f"<div><span>Own risk</span><b>{rupiah(payload['own_risk'])}</b></div>"
            f"<div><span>Sum insured</span><b>{rupiah(c['limit'])}</b></div>"
            f"<div><span>Fraud score</span><b class='risk-{risk}'>{f['score']}</b></div></div>"
            f"<p class='adj-note'>“{d['rationale']}” <i>(confidence {d['confidence']})</i></p>"
            f"<div class='adj-flags'>Fraud flags<ul>{flags}</ul></div>")


async def view(st, chat, clear_text=False):
    p = st["pending"]
    waiting_adj = bool(p and p["node"] == "adjuster_review")
    waiting_cust = bool(p and p["node"] == "ask_customer")
    missing = (p["payload"] or {}).get("missing", []) if waiting_cust else []
    out = {
        chatbot: chat, graph: graph_svg(st), trace: trace_html(st), state_view: await session_state(st),
        status: status_html("adjuster" if waiting_adj else "customer" if waiting_cust else "ready"),
        adj_box: gr.update(visible=waiting_adj),
        adj_info: adjuster_html(p["payload"]) if waiting_adj else "",
        form_box: gr.update(visible=waiting_cust),
        f_policy: gr.update(visible="policy_id" in missing),
        f_date: gr.update(visible="incident_date" in missing),
        f_type: gr.update(visible="incident_type" in missing),
        f_cause: gr.update(visible="incident_cause" in missing),
        f_amount: gr.update(visible="amount_claimed" in missing),
        state: st,
    }
    if waiting_adj:
        out[adj_amount] = (p["payload"] or {}).get("payable")
    if clear_text:
        out[textbox] = ""
    return out


async def on_send(text, chat, st):
    text = (text or "").strip()
    if not text:
        yield await view(st, chat)
        return
    chat = chat + [{"role": "user", "content": text}]
    p = st["pending"]
    if p and p["node"] == "adjuster_review":
        chat.append({"role": "assistant", "content": "Your claim is with our adjuster right now."
                     " I'll update you here as soon as there is a decision."})
        yield await view(st, chat, clear_text=True)
        return
    if p and p["node"] == "ask_customer":
        try:
            reply = json.loads(text)
        except ValueError:      # free text: pull out policy number, date, amount, etc.
            reply = parse_free_text(text)
        content, inv = function_response(st, reply)
    else:
        content, inv = types.Content(role="user", parts=[types.Part(text=text)]), None
    yield await view(st, chat, clear_text=True)
    async for _ in drive(st, chat, content, inv):
        yield await view(st, chat)
    yield await view(st, chat)


async def on_form(policy, date, kind, cause, amount, chat, st):
    reply = {k: v for k, v in {"policy_id": policy, "incident_date": date, "incident_type": kind,
                               "incident_cause": cause, "amount_claimed": amount}.items() if v}
    shown = "\n".join(f"- **{FIELD_LABELS[k].capitalize()}:** {rupiah(v) if k == 'amount_claimed' else v}"
                      for k, v in reply.items())
    chat = chat + [{"role": "user", "content": shown or "(no details)"}]
    content, inv = function_response(st, reply)
    async for _ in drive(st, chat, content, inv):
        yield await view(st, chat)
    yield await view(st, chat)


async def on_decision(approved, amount, note, chat, st):
    reply = {"approved": approved, "approved_amount": amount if approved else None,
             "note": note or ("Approved by adjuster." if approved else "Rejected by adjuster.")}
    st["trace"].append(("👤", "adjuster", ("APPROVE " + rupiah(amount or 0)) if approved else "REJECT"))
    content, inv = function_response(st, reply)
    async for _ in drive(st, chat, content, inv):
        yield await view(st, chat)
    yield await view(st, chat)


async def on_reset():
    return await view(new_state(), greeting(), clear_text=True)


async def on_approve(amount, note, chat, st):
    async for out in on_decision(True, amount, note, chat, st):
        yield out


async def on_reject(amount, note, chat, st):
    async for out in on_decision(False, amount, note, chat, st):
        yield out


# ── Layout ───────────────────────────────────────────────────────────────────
# Quick replies double as the demo scenarios A–E.
QUICK_REPLIES = [
    ("🚗 Car accident", "Hi, I'm Budi Santoso, policy number POL-1001. On 2 November 2026 my car was"
     " sideswiped by a truck on the Jagorawi toll road; the bumper and left door are dented. The"
     " authorised workshop quoted Rp 8.500.000."),
    ("🌊 House flooded", "Good morning, I'm Siti Rahmawati. My house in Bekasi was flooded 1 metre"
     " deep; the kitchen and furniture are damaged. Estimated repair cost is around Rp 120 million."),
    ("🔎 Claim status", "I'd like to check the status of my claim CLM-0042, please."),
    ("🏍️ Motorbike stolen", "I'm Agus Pratama, policy POL-3003. My motorbike was stolen from a"
     " minimarket car park on 10 November 2026, a loss of Rp 18 million."),
    ("🔥 Flood on fire policy", "I'm Rina Wijaya, policy POL-4004. On 9 November 2026 my house in"
     " East Jakarta was flooded, a loss of around Rp 40 million."),
]
MODE = f"Gemini ({MODEL})"
HERE = os.path.dirname(os.path.abspath(__file__))
AVATARS = (os.path.join(HERE, "assets", "customer.svg"), os.path.join(HERE, "assets", "agent.svg"))

LOGO = ('<svg viewBox="0 0 40 40" width="38" height="38"><path d="M20 3 35 9v10c0 9.5-6.3 16.6-15 18'
        '-8.7-1.4-15-8.5-15-18V9z" fill="#F2A900"/><path d="m13 20 5 5 9-10" fill="none" '
        'stroke="#0A2A5E" stroke-width="3.6" stroke-linecap="round" stroke-linejoin="round"/></svg>')
HEADER = f"""
<div class="brand-bar">
  <div class="brand">{LOGO}<div><div class="brand-name">ISTTS Insurance</div>
    <div class="brand-tag">Protecting what matters, every day</div></div></div>
  <div class="brand-nav"><span>Car</span><span>Home</span><span>Travel</span>
    <span class="active">Claims</span><span>Help</span></div>
  <div class="brand-contact"><span class="small">24/7 claims hotline</span><b><a href="tel:+62315027920">+62315027920</a></b></div>
</div>
<div class="hero"><h1>Customer Care</h1>
  <p>File a claim or check its status with Nara, our claims assistant. Most claims are decided in minutes.</p></div>
"""
AGENT_HEAD = """
<div class="agent-head"><div class="agent-avatar">N</div>
  <div><b>Nara</b> · Claims Assistant<div class="online"><span></span>Online now</div></div></div>
"""
FOOTER = (f"<div class='foot'>ISTTS Insurance is a fictional company · demo data only · "
          f"powered by an ADK 2.0 graph workflow on {MODE}</div>")

CSS = """
body,.gradio-container{background:#FFFFFF!important}
.gradio-container{max-width:1320px!important;margin:0 auto!important;padding:12px!important}
button{transition:transform .12s ease,box-shadow .15s ease,background .15s ease!important}
button:hover{transform:translateY(-1px)}button:active{transform:translateY(0) scale(.98)}
footer{display:none!important}
.brand-bar{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;
  background:linear-gradient(110deg,#0A2A5E 0%,#1F4A8F 60%,#1A73E8 100%);color:#fff;padding:14px 22px;border-radius:14px}
.brand-bar,.brand-bar *{color:#FFFFFF!important}
.brand-bar .brand-contact b,.brand-bar .brand-contact a{color:#F2A900!important;text-decoration:none}
.brand-bar .brand-nav .active{color:#0A2A5E!important}
.brand{display:flex;align-items:center;gap:12px}
.brand-name{font-size:20px;font-weight:800;letter-spacing:.2px}
.brand-tag{font-size:12px;opacity:.75}
.brand-nav{display:flex;gap:6px;font-size:14px}
.brand-nav span{padding:6px 12px;border-radius:999px;cursor:pointer;transition:background .15s}
.brand-nav span:hover{background:rgba(255,255,255,.15)}
.brand-nav .active{background:#F2A900;color:#0A2A5E;font-weight:700}
.brand-contact{text-align:right;line-height:1.2}.brand-contact b{font-size:20px;color:#F2A900;display:block}
.brand-contact .small{font-size:11px;opacity:.75}
.hero{padding:18px 4px 4px}.hero h1{margin:0;font-size:26px;color:#0A2A5E!important}
.hero p{margin:4px 0 0;color:#5B6475}
.card{background:#fff!important;border:1px solid #E3E8F2!important;border-radius:16px!important;
  box-shadow:0 4px 18px rgba(10,42,94,.07)!important;padding:16px!important;
  transition:box-shadow .2s ease,border-color .2s ease}
.card:hover{box-shadow:0 8px 28px rgba(26,115,232,.13)!important;border-color:#C9D9F5!important}
.card textarea:focus,.card input:focus{border-color:#1A73E8!important;
  box-shadow:0 0 0 3px rgba(26,115,232,.15)!important}
.agent-head{display:flex;align-items:center;gap:12px;color:#1E2A3B;padding-bottom:6px}
.agent-avatar{width:42px;height:42px;border-radius:50%;background:#0A2A5E;color:#F2A900;
  display:flex;align-items:center;justify-content:center;font-weight:800;font-size:18px}
.online{font-size:12px;color:#1E8E3E}.online span{display:inline-block;width:8px;height:8px;
  border-radius:50%;background:#1E8E3E;margin-right:6px}
.chips{gap:8px!important;flex-wrap:wrap!important}
.chips button{flex:0 0 auto!important;min-width:0!important;border-radius:999px!important;
  border:1px solid #C9D5EA!important;background:#F2F6FD!important;color:#0A2A5E!important;
  font-weight:600!important;padding:6px 14px!important}
.chips button:nth-child(1){border-color:#A8C7FA!important;background:#EEF4FF!important}
.chips button:nth-child(2){border-color:#9ED9E0!important;background:#EAF8FA!important}
.chips button:nth-child(3){border-color:#C8B6F2!important;background:#F4EFFF!important}
.chips button:nth-child(4){border-color:#F6C38E!important;background:#FFF4E8!important}
.chips button:nth-child(5){border-color:#F4A9A6!important;background:#FFF0EF!important}
.chips button:hover{filter:saturate(1.6) brightness(.97);box-shadow:0 3px 10px rgba(10,42,94,.12)}
.section-title{font-weight:700;color:#0A2A5E;font-size:16px}
.section-sub{font-size:12px;color:#5B6475;margin-top:2px}
.pill{display:inline-flex;align-items:center;gap:8px;padding:6px 12px;border-radius:999px;
  font-size:13px;font-weight:600}
.pill .dot{width:8px;height:8px;border-radius:50%;background:currentColor}
.pill-ready{background:#E6F4EA;color:#1E8E3E}
.pill-customer{background:#E8F0FE;color:#1A73E8}
.pill-adjuster{background:#FEF3D6;color:#B06000}
.pill-adjuster .dot,.pill-customer .dot{animation:blink 1s infinite}
@keyframes blink{50%{opacity:.25}}
.adj-card{border:2px solid #F2A900!important;background:#FFFBF0!important}
.adj-amount{font-size:28px;font-weight:800;color:#0A2A5E}
.adj-amount span{display:block;font-size:12px;font-weight:500;color:#5B6475}
.adj-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:10px 0}
.adj-grid div{background:#fff;border:1px solid #F0E2B6;border-radius:10px;padding:8px 10px}
.adj-grid span{display:block;font-size:11px;color:#5B6475}.adj-grid b{font-size:15px;color:#1E2A3B}
.risk-high{color:#C5221F!important}.risk-low{color:#1E8E3E!important}
.adj-note{font-size:13px;color:#3C4655;margin:6px 0}
.adj-flags{font-size:13px;color:#3C4655}.adj-flags ul{margin:4px 0 0 18px}
.form-card{border:2px solid #1A73E8!important;background:#F5F9FF!important}
.foot{text-align:center;font-size:12px;color:#7A8394;padding:10px}
#graph svg{display:block;margin:auto}
.gradio-container,.gradio-container .main,.gradio-container .wrap,.gradio-container .contain{min-width:0!important}
.gradio-container{width:100%!important}
.card .row{min-width:0!important}
@media (max-width:820px){
  .gradio-container{padding:4px!important}
  .gradio-container .main{padding-left:6px!important;padding-right:6px!important}
  .brand-bar{padding:12px 14px;border-radius:12px}
  .brand-nav{display:none}
  .brand-contact b{font-size:16px}
  .hero h1{font-size:21px}.hero p{font-size:13px}
  .card{padding:10px!important;border-radius:12px!important}
  .chips{flex-wrap:nowrap!important;overflow-x:auto!important;padding-bottom:4px}
  .adj-amount{font-size:23px}
}
"""
THEME = gr.themes.Soft(
    primary_hue=gr.themes.Color(c50="#EEF3FB", c100="#DCE6F5", c200="#B9CCEB", c300="#8AA8DA",
                                c400="#5B7BAA", c500="#1F4A8F", c600="#0A2A5E", c700="#08224D",
                                c800="#061A3B", c900="#04122A", c950="#020A18"),
    font=[gr.themes.GoogleFont("Plus Jakarta Sans"), "Arial", "sans-serif"],
).set(button_primary_background_fill="#0A2A5E", button_primary_background_fill_hover="#1F4A8F",
      button_primary_text_color="#FFFFFF")
FORCE_LIGHT = """() => { const u = new URL(window.location);
  if (u.searchParams.get('__theme') !== 'light') { u.searchParams.set('__theme', 'light');
  window.location.replace(u); } }"""

with gr.Blocks(title="ISTTS Insurance · Customer Care") as demo:
    state = gr.State(new_state())
    gr.HTML(HEADER)
    with gr.Row(equal_height=False):
        # Customer side
        with gr.Column(scale=7, min_width=320, elem_classes="card"):
            gr.HTML(AGENT_HEAD)
            chatbot = gr.Chatbot(value=greeting(), show_label=False, height="60vh", min_height=360, max_height=520,
                                 avatar_images=AVATARS, layout="bubble")
            with gr.Row(elem_classes="chips"):
                chips = [gr.Button(label, size="sm") for label, _ in QUICK_REPLIES]
            with gr.Row():
                textbox = gr.Textbox(placeholder="Describe what happened, or ask about a claim…",
                                     show_label=False, scale=6, container=False)
                send = gr.Button("Send ➤", variant="primary", scale=1, min_width=90)
            with gr.Column(visible=False, elem_classes="card form-card") as form_box:
                gr.HTML("<div class='section-title'>📋 A few more details to open your claim</div>")
                with gr.Row():
                    f_policy = gr.Textbox(label="Policy number", placeholder="POL-2002")
                    f_date = gr.Textbox(label="Incident date (YYYY-MM-DD)", placeholder="2026-11-08")
                with gr.Row():
                    f_type = gr.Dropdown(["vehicle", "property", "travel", "other"],
                                         label="Insured object")
                    f_cause = gr.Dropdown(["collision", "theft", "flood", "fire", "other"],
                                          label="Cause of loss")
                    f_amount = gr.Number(label="Estimated loss (Rp)")
                form_send = gr.Button("Submit details", variant="primary")
            reset = gr.Button("↺ Start a new conversation", size="sm", variant="secondary")
        # Back office side
        with gr.Column(scale=5, min_width=320, elem_classes="card"):
            gr.HTML("<div class='section-title'>🏢 Claims back office</div>"
                    "<div class='section-sub'>Live view of the ADK 2.0 graph handling this"
                    " conversation</div>")
            status = gr.HTML(status_html("ready"))
            with gr.Column(visible=False, elem_classes="card adj-card") as adj_box:
                gr.HTML("<div class='section-title'>👤 Adjuster approval required</div>")
                adj_info = gr.HTML()
                with gr.Row():
                    adj_amount = gr.Number(label="Approved amount (Rp)")
                    adj_note = gr.Textbox(label="Note", placeholder="Site survey completed…")
                with gr.Row():
                    approve = gr.Button("✓ Approve", variant="primary")
                    reject = gr.Button("✕ Reject", variant="stop")
            with gr.Tab("Workflow graph"):
                graph = gr.HTML(graph_svg(new_state()), elem_id="graph")
            with gr.Tab("Node trace"):
                trace = gr.HTML(trace_html(new_state()))
            with gr.Tab("Session state"):
                state_view = gr.JSON()
    gr.HTML(FOOTER)

    outputs = [chatbot, graph, trace, state_view, status, adj_box, adj_info, adj_amount,
               form_box, f_policy, f_date, f_type, f_cause, f_amount, textbox, state]
    send.click(on_send, [textbox, chatbot, state], outputs)
    textbox.submit(on_send, [textbox, chatbot, state], outputs)
    for chip, (_, text) in zip(chips, QUICK_REPLIES):
        chip.click(lambda t=text: t, None, textbox).then(on_send, [textbox, chatbot, state], outputs)
    form_send.click(on_form, [f_policy, f_date, f_type, f_cause, f_amount, chatbot, state], outputs)
    approve.click(on_approve, [adj_amount, adj_note, chatbot, state], outputs)
    reject.click(on_reject, [adj_amount, adj_note, chatbot, state], outputs)
    reset.click(on_reset, None, outputs)


if __name__ == "__main__":
    demo.queue().launch(theme=THEME, css=CSS, js=FORCE_LIGHT,
                        server_name=os.getenv("HOST", "127.0.0.1"),
                        server_port=int(os.getenv("PORT", "7860")))
