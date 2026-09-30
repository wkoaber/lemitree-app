# LemiTree — Sleep MVP

Answer a 25-question sleep assessment (by clicking, or by chatting) and get
personalised, evidence-labelled recommendations. Anonymous: nothing is stored
per person.

## Run it on your own machine (2 minutes)

    cd lemitree-app
    python -m venv .venv
    .\.venv\Scripts\Activate.ps1          # Windows PowerShell
    pip install -r requirements.txt
    uvicorn app:app --reload

Open http://127.0.0.1:8000 — the questionnaire should load. The chat mode
needs `ANTHROPIC_API_KEY` set in the environment; the click mode does not.

## Put it on lemitree.com with Railway (about 30 minutes)

1. **GitHub.** Create a new PRIVATE repository, e.g. `lemitree-app`. Push this
   folder to it (GitHub Desktop is the easiest way on Windows: "Add local
   repository" → "Publish repository", untick "Keep this code private" only if
   you intend it public — for now, keep it private).

2. **Railway.** Sign up at https://railway.com with your GitHub account.
   "New Project" → "Deploy from GitHub repo" → pick `lemitree-app`.
   Railway detects Python from `requirements.txt`, and `railway.json` tells it
   how to start the app. First deploy takes 2–3 minutes.

3. **The API key** (chat mode only). In the Railway service → "Variables" →
   add `ANTHROPIC_API_KEY` with your key. Redeploy.

4. **Check it works.** Railway gives you a URL like
   `lemitree-app-production.up.railway.app`. Open it. Answer a question.

5. **Your domain.** Railway service → "Settings" → "Networking" → "Custom
   Domain" → enter `www.lemitree.com`. Railway shows you a CNAME record.
   At your domain registrar, add that CNAME for `www`. For the bare
   `lemitree.com`, add a redirect to `www` (most registrars offer this), or
   an ALIAS/ANAME record if yours supports it. DNS takes minutes to hours.

## Updating the evidence

The app reads `lemitree.db` read-only. When the pipeline produces a new
database (after auditing, re-pooling, re-scoring), copy it over this file,
commit, push — Railway redeploys automatically.

    Copy-Item C:\LemiTree-evidence-pipeline\audit-kit\lemitree.db .\lemitree.db

## What's in here

    app.py              the server: 5 API routes, no framework magic
    recommend.py        the recommendation engine (deterministic; the spec)
    questionnaire.py    the 25 questions with anchors and variable mappings
    static/index.html   the whole interface, one file
    lemitree.db         the evidence database (read-only in the app)
    requirements.txt    fastapi, uvicorn, anthropic
    railway.json        how Railway starts it

## Non-negotiables baked into the code

- An effect whose confidence interval includes zero is never labelled evidenced.
- The contraindication safety gate runs before anything is shown.
- Red flags (witnessed apnoeas, habitual snoring, severe daily impact) trigger
  a clinician referral and withhold self-help actions.
- Every recommendation shows its evidence tier; unstudied never looks proven.
