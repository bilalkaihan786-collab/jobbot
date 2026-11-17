# jobbot

Finds jobs, scores how well you fit each one, writes a tailored resume (and cover letter when the
application accepts one), and fills out or submits the application.

```
find  ──►  filter  ──►  Claude scores fit  ──►  prepare: tailored resume PDF/DOCX  ──►  apply: fill form ──► you (or auto) submit
```

## Setup

You need Python 3.10+ and an Anthropic API key (https://console.anthropic.com).

```bash
pip install -r requirements.txt
playwright install chromium
export ANTHROPIC_API_KEY=sk-ant-...
cp config.example.yaml config.yaml
cp profile.example.yaml profile.yaml
```

Then edit both files:

- **profile.yaml** is your master resume. Put in every role, every strong bullet, every skill and
  your standard application answers (work authorization, salary, etc.). jobbot picks from and
  rephrases this for each job. It never adds anything that isn't there.
- **config.yaml** holds your search keywords and locations, plus the companies to watch. Each company
  is the slug from its careers URL (`job-boards.greenhouse.io/<slug>`, `jobs.lever.co/<slug>`,
  `jobs.ashbyhq.com/<slug>`).

## Use

```bash
python -m jobbot find        # fetch + filter + score; prints ✓ for good matches
python -m jobbot status -v   # see matches with reasons / missing requirements
python -m jobbot prepare     # tailored resume for each match -> output/<company>__<role>/
python -m jobbot apply       # opens each application, fills it, waits for you to submit
python -m jobbot run         # all of the above
```

Applied somewhere by hand? `python -m jobbot mark <key> applied`.

### Apply modes

| mode | what happens |
|---|---|
| `review` (default) | A browser window opens and every field gets filled in. You check it, change anything that's wrong, click **Submit**, then answer `y` in the terminal. |
| `auto` | The form is submitted automatically, but only when every required field was answered with confidence and there's no CAPTCHA. Any other form goes back to you. |

Run in review mode for your first batch so you can see what it fills in, then switch with `--mode auto`
or by changing `apply.mode`.

## How it decides things

- **Fit score:** Claude compares the posting with your profile and preferences and gives a score
  from 0 to 100, plus reasons, missing requirements and red flags. Only jobs at or above
  `matching.min_score` go forward.
- **Truthfulness:** employers, titles, dates, education and contact details are copied straight
  from your profile by code. Claude only picks and rephrases your bullets and writes the summary.
  Skills or roles that aren't in your profile are removed.
- **Cover letters:** with `when_accepted` (the default), a cover letter is written only when the
  form has a field for one, so you don't pay for letters nobody reads.
- **Form questions:** name, email, phone, links, location and current company come from your
  profile. Everything else is answered by Claude using your profile and `application_answers`. If
  answering would mean guessing a fact about you (clearance, criminal history, an unknown
  date), it leaves the field blank for you. EEO questions use your `eeo` answers, or "decline to
  self-identify" when you haven't given one.
- **Records:** everything is stored in `jobbot.db`. Each job folder holds the resume, the cover
  letter and a screenshot of the filled-in form.

## Sources and limits

- **Greenhouse, Lever and Ashby:** jobs are found through each employer's public job-board API,
  and jobbot can fill in the application. Most tech companies use one of these three.
- **Remotive** (optional): it finds remote jobs and prepares your documents. You apply yourself
  on the company's own site.
- **LinkedIn, Indeed and Workday are not supported on purpose.** Their terms ban bots, and
  accounts used for automation get banned. Workday also requires a separate account for every
  company.
- CAPTCHAs, unusual custom widgets and multi-page forms go back to you instead of being guessed.

## Cost

With Claude Opus 5.5, scoring a job costs about $0.01–0.02. A tailored resume plus cover letter costs
about $0.05–0.10. `matching.max_jobs_to_score` caps how many jobs each run scores. To spend less,
set `model: claude-sonnet-5-5`.

## Tests

```bash
pip install pytest && pytest -q
```

The tests run offline. They fill in local copies of Greenhouse- and Lever-style forms, with Claude
replaced by a stub.
