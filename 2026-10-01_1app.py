"""
LemiTree MVP -- FastAPI application.

Routes
  GET  /                    the app (static/index.html)
  GET  /api/questions       the questionnaire: numbered questions, answer options with scores
  GET  /api/conditions      conditions that can trigger the safety gate (for the picker)
  POST /api/recommend       {answers, conditions, statuses, only_tags, exclude} -> recommendations
  GET  /api/action/{abiz}   one action in depth: what it is, pros and cons, how to do it
  POST /api/chat            {messages, current_qid} -> assistant reply, optional chosen option
  GET  /health              what is live: git commit, questionnaire version, database build

The ranking is deterministic code (recommend.py). The only model call is in
/api/chat, which maps a free-text answer onto one of the question's options and
always shows that option back so the person can correct it.

Answers arrive as SCORES (1 = bad end, 5 = good end). The page shows option
labels only; display order and score are separate (see questionnaire.py, Q12).
"""
import os, sqlite3
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from recommend import recommend, load_questionnaire, ALL_STATUS, DEFAULT_STATUS
from questionnaire import SURVEY_VERSION

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "lemitree.db")
QUESTIONS = load_questionnaire()
BY_QID = {q["qid"]: q for q in QUESTIONS}
# The chat model can be changed in Railway Variables without touching code.
CHAT_MODEL = os.environ.get("LEMITREE_CHAT_MODEL", "claude-sonnet-5-5")

app = FastAPI(title="LemiTree", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")


def _ro():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


@app.get("/")
def home():
    return FileResponse(os.path.join(HERE, "static", "index.html"))


@app.get("/health")
def health():
    """One screenshot of this URL proves what is live after a deploy.

    commit       Railway sets RAILWAY_GIT_COMMIT_SHA for GitHub deploys; 'local' otherwise
    questionnaire  the SURVEY_VERSION embedded in questionnaire.py
    db_built     written into the slim database by make_app_db.py
    """
    built = None
    try:
        con = _ro()
        built = dict(con.execute("SELECT key, value FROM app_meta").fetchall()).get("built_at")
        con.close()
    except sqlite3.OperationalError:
        pass          # database built before app_meta existed
    return {"ok": True, "commit": (os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "local")[:7],
            "questionnaire": SURVEY_VERSION, "questions": len(QUESTIONS), "db_built": built,
            "chat_model": CHAT_MODEL}


@app.get("/api/questions")
def questions():
    return [{"qid": q["qid"], "num": i + 1, "text": q["text"], "topic": q.get("topic"),
             "options": q["options"]} for i, q in enumerate(QUESTIONS)]


@app.get("/api/conditions")
def conditions():
    con = _ro()
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
    only_tags: list[str] = []          # e.g. ["one_timer"] or ["quick_win"]
    exclude: list[str] = []            # ACT_ ids the person marked "Already doing" or "Not interested"
    top_n: int = 5


FREQ_PER_DAY = {"per_day": 1.0, "per_night": 1.0, "per_week": 1/7,
                "per_month": 1/30, "per_year": 1/365}
QUICK_WIN_MIN_PER_DAY = 2.0   # a quick win takes more than zero but under two minutes a day
TAGS = ("science_backed", "one_timer", "zero_time_habit", "quick_win")


def _mid(lo, hi):
    v = [x for x in (lo, hi) if x is not None]
    return sum(v) / len(v) if v else None


def _enrich(recs):
    """Attach what a person needs to decide: cost, time, and the quality tags.

    Tags (definitions set by LemiTree; user-facing words in brackets):
      science_backed   'evidenced' for this person's variables  [Science-backed]
      one_timer        done ONCE and it keeps working: a one-off class (not
                       habit_formation / recurring) and no recurring time  [One-timer]
      zero_time_habit  a daily rule that costs no time but must be kept up,
                       e.g. a fixed bedtime  [Zero-time habit]
      quick_win        recurring, more than zero but under two minutes of
                       dedicated time a day  [Quick win]
    Zero time is not the same as done once, and not the same as a quick win:
    the three are mutually exclusive. An action with dedicated time but no
    recorded frequency gets NO time tag -- guessing would mislabel it.
    """
    if not recs:
        return recs
    con = _ro()
    for r in recs:
        row = con.execute("""SELECT id, description_short,
                                    setup_time_min_low, setup_time_min_high,
                                    setup_cost_eur_low, setup_cost_eur_high,
                                    maintenance_cost_eur_low, maintenance_cost_eur_high,
                                    attention_mode, dedicated_time_min, one_timer_class
                             FROM action WHERE business_id = ?""", (r["abiz"],)).fetchone()
        r.pop("key", None)
        if not row:
            continue
        aid = row[0]
        r["description"] = row[1]
        r["setup_time_min"] = _mid(row[2], row[3])
        r["setup_cost_eur"] = _mid(row[4], row[5])
        maint_cost = _mid(row[6], row[7])
        r["attention_mode"], ded, r["one_timer_class"] = row[8], row[9], row[10]
        freq = 0.0
        for fv, fu in con.execute("SELECT frequency_value, frequency_unit FROM action_parameter WHERE action_id=?", (aid,)):
            if fv is not None and fu in FREQ_PER_DAY:
                freq = max(freq, fv * FREQ_PER_DAY[fu])
        r["dedicated_min_per_day"] = (0.0 if (ded is not None and ded < 0.01)
                                      else (ded * freq if (ded is not None and freq) else None))
        r["cost_eur_per_month"] = (maint_cost * freq * 30) if (maint_cost is not None and freq) else (0.0 if maint_cost == 0 else None)
        mpd = r["dedicated_min_per_day"]
        is_habit = r["one_timer_class"] in (None, "", "habit_formation", "recurring")
        no_time = ded is not None and ded < 0.01
        one_timer = no_time and not is_habit
        zero_time = no_time and is_habit
        r["recurring"] = not one_timer
        r["tags"] = {
            "science_backed": r["status"] == "evidenced",
            "one_timer": one_timer,
            "zero_time_habit": zero_time,
            "quick_win": (not no_time) and mpd is not None and 0 < mpd < QUICK_WIN_MIN_PER_DAY,
        }
    con.close()
    return recs


@app.post("/api/recommend")
def api_recommend(body: RecommendIn):
    answers = {}
    for k, v in body.answers.items():
        try:
            iv = int(v)
            if 1 <= iv <= 5 and k in BY_QID:
                answers[k] = iv
        except (TypeError, ValueError):
            pass
    statuses = tuple(s for s in (body.statuses or DEFAULT_STATUS) if s in ALL_STATUS) or DEFAULT_STATUS
    top_n = min(max(body.top_n, 1), 10)
    # Ask the engine for a deep pool so that hidden actions and tag filters
    # are refilled from the next-ranked ones; the engine's order is untouched.
    rep = recommend(DB, QUESTIONS, answers, user_conditions=body.conditions,
                    top_n=60, include_status=statuses)
    skip = set(body.exclude)
    recs = [r for r in rep["recommendations"] if r["abiz"] not in skip]
    recs = _enrich(recs)
    wanted = [t for t in body.only_tags if t in TAGS]
    if wanted:
        recs = [r for r in recs if any(r.get("tags", {}).get(t) for t in wanted)]
    rep["recommendations"] = recs[:top_n]
    rep["answered"], rep["total"] = len(answers), len(QUESTIONS)
    # "Where you have most to gain": the weak complaints, worst first, in plain words.
    by_var = {q["primary"]: q for q in QUESTIONS}
    weak_p = rep.get("weak_primary", {})
    order = {q["qid"]: i for i, q in enumerate(QUESTIONS)}
    rep["gains"] = [by_var[v].get("topic") or by_var[v]["text"]
                    for v in sorted((v for v in weak_p if v in by_var),
                                    key=lambda v: (weak_p[v], order[by_var[v]["qid"]]))][:3]
    # "because you said..." -- the weak behavioural factors this action addresses
    if rep["recommendations"] and weak_p:
        con = _ro()
        for r in rep["recommendations"]:
            hits = [v for (v,) in con.execute("""SELECT v.business_id FROM action_variable_relevance avr
                       JOIN variable v ON v.id = avr.variable_id JOIN action a ON a.id = avr.action_id
                       WHERE a.business_id = ? AND avr.deleted_at IS NULL
                       ORDER BY avr.relevance_1_5 DESC""", (r["abiz"],)) if v in weak_p]
            r["because"] = [by_var[v]["text"] for v in hits if v in by_var][:2]
        con.close()
    rep.pop("excluded_gate", None)
    return rep


EFFORT = {1: "almost no effort", 2: "a little effort", 3: "some effort", 4: "real effort", 5: "a lot of effort"}
DEPENDS = {3: "depends partly on things outside your control", 4: "depends on things outside your control",
           5: "depends heavily on things outside your control"}


@app.get("/api/action/{abiz}")
def action_detail(abiz: str):
    """Everything about one action, in three parts the page shows as toggles.

    what   the description
    pros/cons  assembled from STRUCTURED fields only (effort, dependency,
           reversibility, contraindications) -- no generated prose. Cost, time
           and evidence are already on the card and are combined there.
    how    the protocol steps with their pitfall and variation, plus settings
    Mechanisms are deliberately NOT returned: principle 5 requires them to be
    stated as scientific consensus plus the main competing hypothesis, which
    the current mechanism rows are not (backlog C10).
    'draft' is true while no person has reviewed the row (human_validated = 0).
    """
    con = _ro()
    a = con.execute("""SELECT id, action_name, description_short, cognitive_load_1_5,
                              dependency_risk_1_5, reversibility_class, human_validated
                       FROM action WHERE business_id = ? AND deleted_at IS NULL""", (abiz,)).fetchone()
    if not a:
        con.close()
        raise HTTPException(404, "unknown action")
    aid = a[0]
    steps = [{"phase": p, "text": t, "pitfall": pf, "variation": vn, "minutes": m, "draft": not hv}
             for p, t, pf, vn, m, hv in con.execute("""SELECT phase, step_text, common_pitfall, variation_note,
                     time_cost_min, human_validated FROM action_protocol
                     WHERE action_id = ? AND deleted_at IS NULL ORDER BY step_order""", (aid,))]
    params = []
    for name, unit, lo, hi, txt, fv, fu, notes, hv in con.execute("""SELECT parameter_name, unit,
            recommended_min, recommended_max, recommended_value_text, frequency_value, frequency_unit,
            notes, human_validated FROM action_parameter WHERE action_id = ? AND deleted_at IS NULL""", (aid,)):
        if txt:
            value = txt
        elif lo is not None and hi is not None:
            value = f"{lo:g}–{hi:g} {unit or ''}".strip()
        elif lo is not None or hi is not None:
            value = f"{(lo if lo is not None else hi):g} {unit or ''}".strip()
        else:
            value = None
        params.append({"name": name, "value": value, "note": notes, "draft": not hv})
    # Contraindications of severity 3 and up are worth reading before you start.
    # Severity 4-5 also act as the safety gate when the person declares the condition.
    checks = [{"condition": c, "severity": s, "why": w}
              for c, s, w in con.execute("""SELECT pc.condition_name, c.severity_1_5, c.rationale_short
                     FROM contraindication c JOIN population_condition pc ON pc.id = c.condition_id
                     WHERE c.action_id = ? AND c.deleted_at IS NULL AND c.severity_1_5 >= 3
                     ORDER BY c.severity_1_5 DESC, pc.condition_name""", (aid,))]
    con.close()
    pros, cons = [], []
    if (a[5] or "").lower().startswith("fully reversible"):
        pros.append("Easy to stop: nothing lasting changes if it doesn't suit you")
    if a[3] is not None:
        (pros if a[3] <= 2 else cons).append(f"Takes {EFFORT.get(int(a[3]), 'some effort')} to keep up")
    if a[4] is not None and a[4] >= 3:
        cons.append(DEPENDS.get(int(a[4])).capitalize())
    return {"abiz": abiz, "name": a[1], "what": a[2], "draft": not a[6],
            "pros": pros, "cons": cons, "check_first": checks,
            "settings": params, "steps": steps}


class ChatIn(BaseModel):
    messages: list[dict]
    current_qid: Optional[str] = None


CHAT_SYSTEM = ("You are guiding someone through a short sleep questionnaire, one question at a time, "
               "in a warm and plain voice. Ask the current question naturally, in your own words, and "
               "give a feel for the range of possible answers. When they answer, call record_answer with "
               "the NUMBER of the listed option that best matches what they said. If the answer is "
               "ambiguous, ask one brief clarifying question instead of guessing. Never give sleep "
               "advice; only ask and record.")


@app.post("/api/chat")
def api_chat(body: ChatIn):
    try:
        import anthropic
    except ImportError:
        raise HTTPException(500, "the anthropic package is not installed")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(500, "ANTHROPIC_API_KEY is not set on the server")
    q = BY_QID.get(body.current_qid)
    if not q:
        return {"reply": "That's every question answered. Your recommendations are on the right.",
                "score": None, "done": True}
    opts = q["options"]
    ctx = (f"CURRENT QUESTION ({q['qid']}): {q['text']}\nOPTIONS:\n"
           + "\n".join(f"{i}. {o['label']}" for i, o in enumerate(opts, 1)))
    tool = {"name": "record_answer",
            "description": "Record which listed option matches the person's answer.",
            "input_schema": {"type": "object", "properties": {
                "option": {"type": "integer", "minimum": 1, "maximum": len(opts)},
                "paraphrase": {"type": "string", "description": "one short line restating what they said"}},
                "required": ["option", "paraphrase"]}}
    client = anthropic.Anthropic()
    msgs = [{"role": m["role"], "content": m["content"]}
            for m in body.messages if m.get("role") in ("user", "assistant") and m.get("content")]
    if not msgs or msgs[-1]["role"] != "user":
        msgs.append({"role": "user", "content": "(please ask me the current question)"})
    resp = client.messages.create(model=CHAT_MODEL, max_tokens=400,
                                  system=CHAT_SYSTEM + "\n\n" + ctx, tools=[tool], messages=msgs)
    reply, idx, para = "", None, None
    for b in resp.content:
        if b.type == "text":
            reply += b.text
        elif b.type == "tool_use":
            try:
                idx = int(b.input.get("option"))
            except (TypeError, ValueError):
                idx = None
            para = b.input.get("paraphrase")
    if idx is not None and not 1 <= idx <= len(opts):
        idx = None                     # never trust a number from a model blindly
    if idx is None:
        return {"reply": reply.strip() or "Sorry, I didn't catch that. Could you say it another way?",
                "score": None, "option": None, "paraphrase": para, "done": False}
    label = opts[idx - 1]["label"]
    if not reply:
        reply = f"Got it — I've noted “{label}”. Tap another answer below if that's not right."
    return {"reply": reply.strip(), "option": idx - 1, "score": opts[idx - 1]["score"],
            "label": label, "paraphrase": para, "done": False}
