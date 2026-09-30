"""
LemiTree MVP -- FastAPI application.

Routes
  GET  /                 the app (static/index.html)
  GET  /api/questions    the questionnaire, with scale anchors
  GET  /api/conditions   conditions that can trigger the safety gate (for the picker)
  POST /api/recommend    {answers, conditions, statuses} -> recommendations + severity
  POST /api/chat         {messages, current_qid} -> assistant reply, optional parsed score
  GET  /health

The ranking is deterministic code (recommend.py). The only model call is in
/api/chat, which turns a free-text answer into a 1-5 score and always shows
the interpreted score back to the user so they can correct it.
"""
import os, sqlite3
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from recommend import recommend, load_questionnaire, ALL_STATUS, DEFAULT_STATUS

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "lemitree.db")
QUESTIONS = load_questionnaire()

app = FastAPI(title="LemiTree", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")


@app.get("/")
def home():
    return FileResponse(os.path.join(HERE, "static", "index.html"))


@app.get("/health")
def health():
    return {"ok": True, "questions": len(QUESTIONS)}


@app.get("/api/questions")
def questions():
    return [{"qid": q["qid"], "text": q["text"], "category": q["category"],
             "reverse": q["reverse"], "primary": q["primary"],
             "low": q.get("low"), "high": q.get("high")} for q in QUESTIONS]


@app.get("/api/conditions")
def conditions():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = con.execute("""SELECT DISTINCT pc.condition_name FROM contraindication c
        JOIN population_condition pc ON pc.id = c.condition_id
        WHERE c.severity_1_5 >= 5 OR (c.severity_1_5 = 4 AND c.requires_supervision = 1)
        ORDER BY pc.condition_name""").fetchall()
    con.close()
    return [r[0] for r in rows]


class RecommendIn(BaseModel):
    answers: dict
    conditions: list[str] = []
    statuses: Optional[list[str]] = None
    top_n: int = 5


def _enrich(recs):
    """Attach what the person needs to decide: what it is, what it costs."""
    if not recs:
        return recs
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cols = {x[1] for x in con.execute('PRAGMA table_info("action")').fetchall()}
    extra = (", attention_mode, dedicated_time_min, one_timer_class"
             if "attention_mode" in cols else ", NULL, NULL, NULL")
    for r in recs:
        row = con.execute(f"""SELECT description_short, setup_time_min_low, setup_time_min_high,
                                     setup_cost_eur_low, setup_cost_eur_high {extra}
                              FROM action WHERE business_id = ?""", (r["abiz"],)).fetchone()
        if row:
            r["description"] = row[0]
            r["setup_time_min"] = row[2] if row[2] is not None else row[1]
            r["setup_cost_eur"] = row[4] if row[4] is not None else row[3]
            r["attention_mode"], r["dedicated_time_min"], r["one_timer_class"] = row[5], row[6], row[7]
        r.pop("key", None)
    con.close()
    return recs


@app.post("/api/recommend")
def api_recommend(body: RecommendIn):
    answers = {}
    for k, v in body.answers.items():
        try:
            iv = int(v)
            if 1 <= iv <= 5:
                answers[k] = iv
        except (TypeError, ValueError):
            pass
    statuses = tuple(s for s in (body.statuses or DEFAULT_STATUS) if s in ALL_STATUS) or DEFAULT_STATUS
    rep = recommend(DB, QUESTIONS, answers, user_conditions=body.conditions,
                    top_n=min(max(body.top_n, 1), 10), include_status=statuses)
    rep["recommendations"] = _enrich(rep["recommendations"])
    rep["answered"], rep["total"] = len(answers), len(QUESTIONS)
    # "because you said..." -- the weak behavioural factors this action addresses
    by_var = {q["primary"]: q for q in QUESTIONS}
    weak_p = rep.get("weak_primary", {})
    if rep["recommendations"] and weak_p:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        for r in rep["recommendations"]:
            hits = [v for (v,) in con.execute("""SELECT v.business_id FROM action_variable_relevance avr
                       JOIN variable v ON v.id = avr.variable_id JOIN action a ON a.id = avr.action_id
                       WHERE a.business_id = ? AND avr.deleted_at IS NULL
                       ORDER BY avr.relevance_1_5 DESC""", (r["abiz"],)) if v in weak_p]
            r["because"] = [by_var[v]["text"] for v in hits if v in by_var][:2]
        con.close()
    rep.pop("excluded_gate", None)
    return rep


class ChatIn(BaseModel):
    messages: list[dict]
    current_qid: Optional[str] = None


CHAT_SYSTEM = ("You are guiding someone through a short sleep questionnaire, one question at a time, "
               "in a warm and plain voice. Ask the current question naturally, in your own words, and "
               "mention both ends of the scale so the person knows the range. When they answer, call "
               "record_answer with the 1-5 score that best matches what they said, where 1 is the LOW "
               "anchor and 5 is the HIGH anchor. If the answer is ambiguous, ask one brief clarifying "
               "question instead of guessing. Never give sleep advice; only ask and record.")


@app.post("/api/chat")
def api_chat(body: ChatIn):
    try:
        import anthropic
    except ImportError:
        raise HTTPException(500, "the anthropic package is not installed")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(500, "ANTHROPIC_API_KEY is not set on the server")
    q = next((x for x in QUESTIONS if x["qid"] == body.current_qid), None)
    if not q:
        return {"reply": "That's every question answered. Your recommendations are on the right.",
                "score": None, "done": True}
    ctx = (f"CURRENT QUESTION ({q['qid']}): {q['text']}\n"
           f"Low end of scale (1): {q.get('low', '')}\nHigh end of scale (5): {q.get('high', '')}")
    tool = {"name": "record_answer",
            "description": "Record the person's answer to the current question.",
            "input_schema": {"type": "object", "properties": {
                "score": {"type": "integer", "minimum": 1, "maximum": 5},
                "paraphrase": {"type": "string", "description": "one short line restating what they said"}},
                "required": ["score", "paraphrase"]}}
    client = anthropic.Anthropic()
    msgs = [{"role": m["role"], "content": m["content"]}
            for m in body.messages if m.get("role") in ("user", "assistant") and m.get("content")]
    if not msgs or msgs[-1]["role"] != "user":
        msgs.append({"role": "user", "content": "(please ask me the current question)"})
    resp = client.messages.create(model="claude-sonnet-4-6", max_tokens=400,
                                  system=CHAT_SYSTEM + "\n\n" + ctx, tools=[tool], messages=msgs)
    reply, score, para = "", None, None
    for b in resp.content:
        if b.type == "text":
            reply += b.text
        elif b.type == "tool_use":
            score, para = int(b.input.get("score")), b.input.get("paraphrase")
    if score and not reply:
        reply = f"Got it — I've noted that as {score} out of 5 ({para}). Tap below to correct it if that's not right."
    return {"reply": reply.strip(), "score": score, "paraphrase": para, "done": False}
