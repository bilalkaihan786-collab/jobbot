# jobbot

Finds jobs, scores how well you fit each one, writes a tailored resume (and cover letter when the
application accepts one), and fills out or submits the application.

The example settings are set up for a **Kinesiology + French student in Canada** looking for
student jobs (summer, part-time, co-op): research assistant and clinical/hospital roles, French
teaching and tutoring, and government jobs that need French. Change them on the Preferences tab.

```
find  ──►  filter  ──►  Claude scores fit  ──►  prepare: tailored resume PDF/DOCX  ──►  apply: fill form ──► you (or auto) submit
```

## Setup

You need Python 3.10+ and one of these to do the AI work:

- **Claude** (best results): an Anthropic API key from https://console.anthropic.com. Costs a few
  cents per job.
- **Ollama** (free, runs on your own computer): install https://ollama.com, then run
  `ollama pull qwen2.5:7b` once. Needs about 8 GB of RAM. It's slower than Claude, and its
  resumes and answers are weaker, so check them more carefully.

With the default `provider: auto`, jobbot uses Claude when `ANTHROPIC_API_KEY` is set and Ollama
otherwise. Set `provider: claude` or `provider: ollama` in config.yaml to choose explicitly.

```bash
pip install -r requirements.txt
playwright install chromium
export ANTHROPIC_API_KEY=sk-ant-...   # skip this line if you're using Ollama
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

## Web page (easiest)

```bash
python -m jobbot web
```

This opens jobbot in your browser at http://localhost:8000. It only runs on your own computer.
On first run it creates `config.yaml` and `profile.yaml` from the examples, and you can edit both
on the **Settings** tab. The **Jobs** tab has buttons for each step (Find jobs, Prepare resumes,
Apply), a live activity log, your matches with the reasons for each score, and links to the
generated resumes. In review mode, when a form is filled in, the page asks you to check it in the
browser window and click Submit there, then tell jobbot what happened. Keep the terminal window
open while you use it; press Ctrl+C there to stop.

The Jobs tab has a search box and filters (internships only, minimum score, remote only) with
sorting by best match, newest or company. The **Preferences** tab is a form for what you're looking
for: keywords, locations, companies, level, salary and more. It saves into `config.yaml` and
`profile.yaml` and keeps their comments.

Use `--port 8080` to pick another port, or `--no-browser` to skip opening a tab.

### Student jobs, internships and co-ops

Set **What are you looking for?** on the Preferences tab (or `search.focus` in config.yaml):

| focus | what happens |
|---|---|
| `students` | Student-friendly work of any kind: summer, part-time during school, co-ops, internships. The AI judges you as a student and marks jobs that need a finished degree, a professional licence (e.g. registered kinesiologist, nurse) or years of experience as not worth applying. Resumes list Education first. |
| `internships` | Only postings whose title says internship, co-op or student. |
| `jobs` | Regular roles after graduation. |

Tell it when you can work (`preferences.schedule`: summer, part-time, co-op), your term
(`preferences.internship_term`), and the kinds of work you want (`preferences.targets`). Put your
expected graduation in your profile's education `details`. List your languages under `languages`:
they appear on every resume, and postings that require French (e.g. bilingual government jobs)
are scored against them. After changing the focus, **Find jobs** looks again at postings it
filtered out before.

### Adzuna (hospitals, universities, school boards, governments)

Greenhouse, Lever and Ashby are mostly used by tech companies. For healthcare, research,
teaching and government jobs, turn on **Adzuna** on the Preferences tab: register for a free
app ID and key at https://developer.adzuna.com and paste them in. Each search term runs
separately in your country and city. Adzuna only provides a snippet of each description, so
open the posting before you apply; you apply on the employer's site, and jobbot prepares the
resume and cover letter. In Canada the Jobs tab also links Job Bank searches for your terms.

### Your profile

`profile.example.yaml` is an example student. Replace everything with your own details, then
delete the `example_profile: true` line. Until you do, jobbot finds jobs but won't score them,
write resumes or fill applications.

## Command line


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
