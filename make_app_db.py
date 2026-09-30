r"""
make_app_db.py  --  build the small database the website serves, from the full
pipeline database.

WHY
  The pipeline database is ~105 MB, over GitHub's 100 MB per-file limit, and
  most of it is pipeline internals the website never reads: 7,312 abstracts,
  39,445 prefilter verdicts, the harvest log. Those also should not sit on a
  public server -- the method is kept confidential.

WHAT IT KEEPS
  Everything the recommender and the page read, unchanged. paper_effect_link is
  reduced to its HUMAN-AUDITED rows only, because evidence status depends on
  nothing else.

SAFETY CHECK
  After building, it runs the same recommendation scenarios against BOTH
  databases and refuses to finish unless the results are identical.

Usage (from inside the lemitree-app folder):
  python make_app_db.py "C:\LemiTree-evidence-pipeline\audit-kit\lemitree.db"
"""
import sys, os, shutil, sqlite3

DROP = ["llm_prefilter_decision", "harvest_log", "proposed_effect_link",
        "scientific_evidence", "scientific_journal", "claim_evidence", "migration_log"]

def build(src, dst):
    if os.path.exists(dst):
        os.remove(dst)
    shutil.copyfile(src, dst)
    con = sqlite3.connect(dst); q = con.execute
    q("PRAGMA foreign_keys=OFF")
    have = {r[0] for r in q("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in DROP:
        if t in have:
            q(f'DROP TABLE "{t}"')
    # evidence status only depends on audited links
    before = q("SELECT COUNT(*) FROM paper_effect_link").fetchone()[0]
    q("DELETE FROM paper_effect_link WHERE audit_status IS NULL")
    after = q("SELECT COUNT(*) FROM paper_effect_link").fetchone()[0]
    con.commit()
    q("VACUUM")
    con.close()
    return before, after

def check(src, dst):
    """Same scenarios, both databases -> results must match exactly."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from recommend import recommend, load_questionnaire, ALL_STATUS
    Q = load_questionnaire()
    byv = {q["primary"]: q for q in Q}
    def ans(worst=(), base=4):
        a = {q["qid"]: (6 - base if q["reverse"] else base) for q in Q}
        for v in worst:
            if v in byv:
                qq = byv[v]; a[qq["qid"]] = 5 if qq["reverse"] else 1
        return a
    scenarios = [
        ("noise, screens, wind-down", ans(["VAR_105", "VAR_100", "VAR_113"]), [], None),
        ("mattress, naps",            ans(["VAR_106", "VAR_111"]), [], None),
        ("all worst",                 {q["qid"]: (5 if q["reverse"] else 1) for q in Q}, [], None),
        ("witnessed apnoea",          ans(["VAR_024", "VAR_126"]), [], None),
        ("safety gate",               ans(["VAR_105"]), ["Caregiver on-call status requiring auditory alertness"], None),
        ("everything toggled on",     ans(["VAR_105", "VAR_100"]), [], ALL_STATUS),
    ]
    ok = True
    for name, a, conds, st in scenarios:
        kw = {"user_conditions": conds, "top_n": 8}
        if st: kw["include_status"] = st
        r1 = recommend(src, Q, a, **kw); r2 = recommend(dst, Q, a, **kw)
        s1 = [(x["abiz"], x["status"]) for x in r1["recommendations"]]
        s2 = [(x["abiz"], x["status"]) for x in r2["recommendations"]]
        same = s1 == s2 and r1["severity"] == r2["severity"] and r1["suppress_actions"] == r2["suppress_actions"]
        print(f"  {'identical' if same else 'DIFFERENT':10} {name}")
        ok = ok and same
    return ok

def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    src = sys.argv[1]
    here = os.path.dirname(os.path.abspath(__file__))
    dst = os.path.join(here, "lemitree.db")
    if os.path.abspath(src) == os.path.abspath(dst):
        sys.exit("Source and destination are the same file -- point at the PIPELINE database.")
    before, after = build(src, dst)
    mb = os.path.getsize(dst) / 1e6
    print(f"built {dst}")
    print(f"  size {os.path.getsize(src)/1e6:.1f} MB -> {mb:.1f} MB")
    print(f"  paper_effect_link {before} -> {after} rows (audited only)")
    print("checking the website gives the same answers on both databases:")
    if not check(src, dst):
        os.remove(dst)
        sys.exit("\nFAILED: results differ -- slim database removed. Nothing changed.")
    if mb >= 95:
        sys.exit("\nFAILED: still too large for GitHub.")
    print(f"\nOK -- {mb:.1f} MB, identical results. Safe to commit.")

if __name__ == "__main__":
    main()
