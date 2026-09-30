r"""
recommend.py  --  MVP recommendation logic (Pipeline E reference implementation).

NOT a script to run directly. It is imported (see run_experiments.py) and is the
specification the web/Supabase build should follow.

Chain:
  answers (1-5 per question)
    -> reverse-score where flagged, so 1 = worst and 5 = best
    -> WEAK variables (score <= threshold), primary + secondary
    -> RELEVANCE FILTER: a candidate action must claim an effect on at least one
       WEAK variable, so recommendations respond to what the user complained about
    -> each candidate carries an EVIDENCE STATUS:
         evidenced               a pooled effect size exists
         supported_unquantified  verified papers exist, numbers not extracted yet
         searched_unsupported    audited and only rejections found (evidence of
                                 ABSENCE -- off by default)
         unstudied               never audited; genuinely unknown
    -> include_status lets the caller toggle each subset on/off, so a user can
       see how recommendations change as they include or exclude each category
    -> beneficial effects only (oriented by improvement_direction); target_range
       variables are withheld until a user baseline exists
    -> SAFETY GATE excludes actions contraindicated for this user's conditions
    -> rank: evidenced first by composite quality score, then the remaining
       statuses by ease score -- the two meanings are never mixed in one number
"""
import sqlite3, math

K_EFFICACY = 0.8

def efficacy(g):
    a = abs(g); return a / (a + K_EFFICACY)

def weakness_weight(score):
    """1 = worst answer -> weight 1.0 ; 2 -> 0.5 ; better scores are not weak."""
    return max(0.0, (3.0 - score) / 2.0)

ALL_STATUS = ("evidenced", "supported_unquantified", "unstudied",
              "no_reliable_effect", "searched_unsupported")

def relevance_band(rel):
    if not rel: return 0
    return 2 if rel >= 4 else 1

# Ranking tiers: evidence quality dominates, then the within-tier score.
# An action with verified papers must outrank one nobody has examined, even if
# the latter is cheaper -- ease can never buy its way past evidence.
STATUS_RANK = {"evidenced": 3, "supported_unquantified": 2, "unstudied": 1,
               "no_reliable_effect": 0, "searched_unsupported": 0}

# A pooled estimate whose confidence interval INCLUDES ZERO is not evidence that
# the action works -- the data are compatible with benefit, with harm, and with
# nothing. Such an effect is never labelled 'evidenced'; it becomes
# 'no_reliable_effect' ("so far, no reliable effect detected") and is OFF by
# default, alongside searched_unsupported. Both mean: we looked, and we did not
# find support. Showing either as science-backed would be the single most
# damaging thing this product could do.
def ci_includes_zero(lo, hi):
    if lo is None or hi is None:
        return False          # no interval stored -> cannot assess here
    return lo <= 0 <= hi

# Sort order is (relevance_band, status_rank, score).
# relevance_band: 2 = strongly addresses the complaint (relevance 4-5)
#                 1 = partially addresses it (relevance 2-3)
#                 0 = does not address it at all
# So a STRONGLY relevant supported action outranks a WEAKLY relevant evidenced
# one, while within the same relevance band evidence still decides. This keeps
# the list mostly evidence-backed without letting evidence drown out fit.
# RELEVANCE COMES FIRST, deliberately: an action that does not address what the
# user is actually failing at is not a better recommendation just because it is
# well evidenced. Within the relevant set, evidence quality still dominates --
# so the user gets the best-supported action THAT ADDRESSES THEIR PROBLEM.
# Without this, the evidenced tier fills every slot and all users see one list.
DEFAULT_STATUS = ("evidenced", "supported_unquantified", "unstudied")
# note: no_reliable_effect and searched_unsupported are deliberately excluded

def load_questionnaire(qpath=None):
    """Return the 25-question spec. Uses the embedded questionnaire.py by default;
    pass an xlsx path only to override it."""
    if qpath is None:
        from questionnaire import QUESTIONS
        return QUESTIONS
    try:
        import openpyxl
    except ImportError:
        raise SystemExit("openpyxl is not installed. Run:  pip install openpyxl")
    wb = openpyxl.load_workbook(qpath, read_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    hdr = {h: i for i, h in enumerate(rows[0])}
    out = []
    for r in rows[1:]:
        if not r[hdr["question_id"]]: continue
        sec = r[hdr["secondary_variable_ids"]] or ""
        out.append({"qid": r[hdr["question_id"]], "primary": r[hdr["primary_variable_id"]],
                    "secondary": [s.strip() for s in str(sec).split(",") if s.strip() and s.strip()!="None"],
                    "reverse": str(r[hdr["is_reverse_scored"]]).lower()=="true",
                    "text": r[hdr["question_text"]], "category": r[hdr["question_category"]]})
    return out

# ---------------------------------------------------------------------------
# DOMAIN SEVERITY AND CLINICAL REFERRAL
#
# LemiTree is for moving someone from mediocre to good to great. It is NOT for
# someone who is genuinely suffering: on a -10..+10 scale we aim to help people
# between roughly -4 and +8. Below -4 the right recommendation is a clinician,
# not an earplug -- and offering self-help there risks delaying real care.
#
# Two independent triggers:
#   1. AGGREGATE severity  -- the mean of the symptom answers falls below -4
#   2. RED FLAG items      -- a worst-end answer on a variable that signals a
#                             diagnosable medical condition, regardless of the
#                             aggregate (someone can sleep well on average and
#                             still have witnessed apnoeas)
# Red flags are keyed by VARIABLE, not question id, so they survive changes to
# the questionnaire.
# ---------------------------------------------------------------------------
RED_FLAG_VARIABLES = {
    "VAR_024": "witnessed pauses in breathing during sleep (possible sleep apnoea)",
    "VAR_126": "loud habitual snoring (possible obstructive sleep apnoea)",
    "VAR_123": "sleep problems severely interfering with daily life (possible chronic insomnia)",
}

def to_scale(score_1_5):
    """1-5 answer (1 = worst) -> -10..+10."""
    return (score_1_5 - 3) * 5.0

def domain_severity(questions, answers, referral_threshold=-4.0):
    """Return a dict describing how the person is doing in this domain.

    severity : mean of the symptom answers on -10..+10
    band     : suffering | struggling | okay | thriving
    red_flags: list of clinical signals found
    referral : None, 'recommended', or 'urgent'
    """
    symptom = [q for q in questions if (q.get("category") or "").lower().startswith("symptom")]
    scored = symptom or questions            # fall back if categories are absent
    vals, flags = [], []
    for q in scored:
        raw = answers.get(q["qid"])
        if raw is None: continue
        s = (6 - raw) if q["reverse"] else raw
        vals.append(to_scale(s))
        if s <= 1 and q["primary"] in RED_FLAG_VARIABLES:
            flags.append(RED_FLAG_VARIABLES[q["primary"]])
    if not vals:
        return {"severity": None, "band": None, "red_flags": [], "referral": None,
                "answered": 0}
    sev = sum(vals) / len(vals)
    band = ("suffering" if sev < referral_threshold else
            "struggling" if sev < 0 else
            "okay" if sev < 4 else "thriving")
    referral = "urgent" if flags else ("recommended" if sev < referral_threshold else None)
    return {"severity": round(sev, 2), "band": band, "red_flags": flags,
            "referral": referral, "answered": len(vals)}

REFERRAL_MESSAGE = {
 "urgent": ("Some of your answers point to a possible medical sleep disorder. Please speak to a "
            "doctor or a sleep clinic -- these conditions are diagnosable and treatable, and no "
            "self-help action substitutes for that assessment."),
 "recommended": ("Your answers suggest sleep is affecting you severely rather than mildly. We would "
                 "encourage you to speak to a doctor. The suggestions below may still help, but they "
                 "are not a substitute for proper care."),
}

def weak_variables(questions, answers, threshold=2):
    """-> (weak_all, weak_primary).

    weak_all       primary + secondary: used to FIND evidenced effects (the
                   evidence lives on outcome variables like Sleep Quality).
    weak_primary   the BEHAVIOURAL complaint only: used for RELEVANCE matching.

    Keeping them apart is essential. Sleep Onset Latency is a secondary variable
    on 14 of 25 questions and Sleep Quality on 12, so if secondaries were allowed
    to drive relevance, every user would match the same actions -- which is
    exactly the bug this split fixes.
    """
    weak_all, weak_primary = {}, {}
    for q in questions:
        raw = answers.get(q["qid"])
        if raw is None: continue
        score = (6 - raw) if q["reverse"] else raw
        if score <= threshold:
            weak_primary[q["primary"]] = min(weak_primary.get(q["primary"], 9), score)
            for vid in [q["primary"]] + q["secondary"]:
                weak_all[vid] = min(weak_all.get(vid, 9), score)
    return weak_all, weak_primary

def _evidence_status_map(q):
    ev  = {r[0] for r in q("SELECT DISTINCT action_id FROM effect_of_action WHERE summary_effect_size_value IS NOT NULL")}
    ver = {r[0] for r in q("""SELECT DISTINCT eoa.action_id FROM paper_effect_link pel
             JOIN effect_of_action eoa ON eoa.id=pel.effect_id WHERE pel.audit_status='verified'""")}
    aud = {r[0] for r in q("""SELECT DISTINCT eoa.action_id FROM paper_effect_link pel
             JOIN effect_of_action eoa ON eoa.id=pel.effect_id WHERE pel.audit_status IS NOT NULL""")}
    def st(aid):
        if aid in ev:  return "evidenced"
        if aid in ver: return "supported_unquantified"
        if aid in aud: return "searched_unsupported"
        return "unstudied"
    return st

def recommend(db, questions, answers, user_conditions=(), top_n=5, threshold=2,
              include_status=DEFAULT_STATUS):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True); q = con.execute
    weak, weak_primary = weak_variables(questions, answers, threshold)
    sev = domain_severity(questions, answers)
    report = {"severity": sev,
              "referral_message": REFERRAL_MESSAGE.get(sev["referral"]),
              # When a red flag fires, a clinician is THE recommendation. Actions are
              # still computed, but the caller must lead with the referral and should
              # not present them as a solution.
              "lead_with_referral": sev["referral"] is not None,
              "suppress_actions": sev["referral"] == "urgent",
              "weak_variables": weak, "weak_primary": weak_primary, "include_status": list(include_status),
              "excluded_gate": [], "skipped_target_range": [],
              "available_by_status": {}, "recommendations": []}
    if not weak:
        con.close(); return report

    status_of = _evidence_status_map(q)

    gated = set()
    if user_conditions:
        for aid, cond, sev in q("""SELECT c.action_id, pc.condition_name, c.severity_1_5
                FROM contraindication c JOIN population_condition pc ON pc.id=c.condition_id
                WHERE (c.severity_1_5>=5 OR (c.severity_1_5=4 AND c.requires_supervision=1))"""):
            for uc in user_conditions:
                if uc.lower() in (cond or "").lower() or (cond or "").lower() in uc.lower():
                    gated.add(aid); report["excluded_gate"].append((cond, sev))

    # RELEVANCE: which behavioural deficits does each action address? (table 16)
    # relevance[action_id][variable_business_id] = 1..5
    relevance = {}
    try:
        for aid_, vbiz_, rel_ in q("""SELECT avr.action_id, v.business_id, avr.relevance_1_5
                FROM action_variable_relevance avr JOIN variable v ON v.id = avr.variable_id
                WHERE avr.deleted_at IS NULL"""):
            relevance.setdefault(aid_, {})[vbiz_] = rel_
    except sqlite3.OperationalError:
        pass   # table not migrated yet -> behave as before
    report["relevance_rows"] = sum(len(v) for v in relevance.values())

    ease = {r[0]: r[1] for r in q("SELECT action_id, composite_quality_score FROM derived_measure WHERE measure_name='ease_score'")}
    fric = {r[0]: r[1] for r in q("SELECT action_id, friction_score FROM derived_measure WHERE measure_name='ease_score'")}
    comp = {r[0]: r[1] for r in q("""SELECT action_id, composite_quality_score FROM derived_measure
                                     WHERE measure_name='composite_quality' AND composite_quality_score IS NOT NULL""")}

    marks = ",".join("?"*len(weak))
    best, seen_actions = {}, set()
    for (aid, abiz, aname, vbiz, vname, direction, g, lo, hi, k) in q(f"""
            SELECT a.id, a.business_id, a.action_name, v.business_id, v.variable_name,
                   v.improvement_direction, eoa.summary_effect_size_value,
                   eoa.summary_ci_low, eoa.summary_ci_high, eoa.pooled_k
            FROM effect_of_action eoa
            JOIN variable v ON v.id = eoa.variable_id
            JOIN action a   ON a.id = eoa.action_id
            WHERE v.business_id IN ({marks})""", tuple(weak)).fetchall():
        if aid in gated: continue
        st = status_of(aid)
        if aid not in seen_actions:
            report["available_by_status"][st] = report["available_by_status"].get(st, 0) + 1
            seen_actions.add(aid)
        if st not in include_status: continue
        if g is not None:
            if direction == "target_range":
                report["skipped_target_range"].append((abiz, vbiz)); continue
            if direction == "increase":   b = g; blo, bhi = lo, hi
            elif direction == "decrease": b = -g; blo, bhi = (None, None) if lo is None else (-hi, -lo)
            else: continue
            # CI RULE: an interval spanning zero means no reliable effect.
            if ci_includes_zero(blo, bhi):
                st = "no_reliable_effect"
                if st not in include_status:
                    report.setdefault("excluded_no_reliable_effect", []).append((abiz, vbiz))
                    continue
            if b <= 0 and st != "no_reliable_effect": continue
            # USER-SPECIFIC RANKING: weight this effect by how weak the user is
            # on THIS variable, then divide by the action's friction. This is what
            # makes two different users get different orderings.
            w = weakness_weight(weak.get(vbiz, 3))
            f = fric.get(aid) or 1.0
            # Relevance factor: how strongly does this action address ANY of the
            # user's weak behavioural factors? Without table 16 this is neutral (1.0),
            # so behaviour is unchanged until relevance data exists.
            rel = relevance.get(aid, {})
            rel_hit = max((rel[v] for v in rel if v in weak_primary), default=None)
            rel_factor = (rel_hit / 3.0) if rel_hit else (0.4 if relevance else 1.0)
            contrib = w * efficacy(b) * rel_factor / f
            # "g" and "ci" are BOTH benefit-oriented (positive = good for the user),
            # so an interface can display them side by side without contradiction.
            # For a 'decrease' variable the raw interval is flipped to match.
            # The raw values stay available as g_raw / ci_raw for auditing.
            cand = {"action": aname, "abiz": abiz, "variable": vname, "vbiz": vbiz,
                    "g": b, "ci": (blo, bhi), "g_raw": g, "ci_raw": (lo, hi),
                    "k": k, "status": st,
                    "composite": comp.get(aid), "ease": ease.get(aid),
                    "friction": fric.get(aid), "match_score": contrib,
                    "relevance": rel_hit,
                    "key": (relevance_band(rel_hit), STATUS_RANK[st], contrib)}
        else:
            e = ease.get(aid)
            if e is None: continue
            # No quantified effect on THIS user's weak variable. Even if the
            # action is 'evidenced' elsewhere, for this user it is not -- so it
            # ranks in the supported tier and is labelled honestly.
            st_here = "supported_unquantified" if st == "evidenced" else st
            # Filter on the EFFECTIVE status. An action evidenced on some OTHER
            # variable is not evidenced for this user, so selecting "evidenced
            # only" must not return it labelled 'supported'.
            if st_here not in include_status: continue
            rel = relevance.get(aid, {})
            rel_hit = max((rel[v] for v in rel if v in weak_primary), default=None)
            rel_factor = (rel_hit / 3.0) if rel_hit else (0.4 if relevance else 1.0)
            cand = {"action": aname, "abiz": abiz, "variable": vname, "vbiz": vbiz,
                    "g": None, "ci": (None, None), "k": 0, "status": st_here,
                    "evidence_elsewhere": (st == "evidenced"), "relevance": rel_hit,
                    "composite": None, "ease": e, "friction": fric.get(aid),
                    "key": (relevance_band(rel_hit), STATUS_RANK[st_here], e * rel_factor)}
        prior = best.get(aid)
        if prior is None:
            best[aid] = cand
        elif cand["key"][1] == prior["key"][1] == STATUS_RANK["evidenced"]:
            # same action, another weak variable -> accumulate the benefit,
            # and report the strongest single effect as the headline reason
            total = prior.get("match_score", 0) + cand.get("match_score", 0)
            head = cand if cand.get("match_score", 0) > prior.get("match_score", 0) else prior
            head = dict(head); head["match_score"] = total; head["key"] = (max(cand["key"][0], prior["key"][0]), STATUS_RANK["evidenced"], total)
            best[aid] = head
        elif cand["key"] > prior["key"]:
            best[aid] = cand

    report["recommendations"] = sorted(best.values(), key=lambda r: r["key"], reverse=True)[:top_n]
    con.close(); return report
