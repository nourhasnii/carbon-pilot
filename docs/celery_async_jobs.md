# Asynchronous AI jobs (Celery + Redis)

One CarbonPilot AI feature runs in the background: **AI recommendation
generation**. It calls the local Qwen model through Ollama, and a full
generation can take minutes on a CPU. Running it inside the HTTP request held
the browser open and made a long job look like a hung page.

That single job is now queued on **Celery** and executed by a **worker**
process, with **Redis** acting as the local broker and result backend. The
button returns immediately and the page polls the job's status until it reaches
a terminal state.

> Only this job is asynchronous. Every other AI feature — the ESG completeness
> coach, internal-audit analysis, the assistant, and the scikit-learn models
> (objective prediction, carbon anomaly detection) — stays synchronous, because
> those calls are fast and gain nothing from a worker.

## Where the code lives

| Concern | Location |
| --- | --- |
| Celery app | [config/celery.py](../config/celery.py), [config/__init__.py](../config/__init__.py) |
| Broker / timeout settings | [config/settings.py](../config/settings.py) (`CELERY_*`, `AI_JOB_*`) |
| Job model + statuses | [apps/analytics/models.py](../apps/analytics/models.py) (`AiJob`, `JobType`, `JobStatus`) |
| Celery task | [apps/recommendations/tasks.py](../apps/recommendations/tasks.py) |
| Enqueue seam | [apps/recommendations/services/jobs.py](../apps/recommendations/services/jobs.py) |
| Views / URLs | [apps/recommendations/views.py](../apps/recommendations/views.py) (`recommendation_generate`, `ai_job_status`) |
| UI | `templates/recommendations/list.html`, `static/js/ai-job-poll.js` |

## Starting it locally (Windows PowerShell)

Three processes. Redis and the worker are new; Django is unchanged.

```powershell
# 1. Redis (the broker + result backend)
#    Any local Redis service works. For example, a Redis server listening on
#    the default port:
redis-server

# 2. Celery worker — run from the project root, in the virtualenv.
#    --pool=solo is REQUIRED on Windows: the default prefork pool is not
#    supported there.
.\venv\Scripts\python.exe -m celery -A config worker --pool=solo -l info

# 3. Django
.\venv\Scripts\python.exe manage.py runserver
```

Redis defaults to `redis://127.0.0.1:6379/0`. Override it (and the worker's
behaviour) through the environment if your setup differs:

| Variable | Default | Purpose |
| --- | --- | --- |
| `CELERY_REDIS_URL` | `redis://127.0.0.1:6379/0` | Redis connection |
| `CELERY_BROKER_URL` | falls back to `CELERY_REDIS_URL` | Broker |
| `CELERY_RESULT_BACKEND` | falls back to `CELERY_REDIS_URL` | Result backend |
| `AI_JOB_TIMEOUT_SECONDS` | `OLLAMA_TIMEOUT + 300` | A job still active after this is reported as timed out |
| `AI_JOB_QUEUE_GRACE_SECONDS` | `90` | A job still Pending after this is reported as "no worker" |
| `CELERY_TASK_ALWAYS_EAGER` | `false` | Run tasks inline (no Redis/worker needed) |

## What the user sees

The job row is the single source of truth. Its status moves:

```
PENDING ──▶ RUNNING ──▶ DONE
   │            │
   └────────────┴──────▶ FAILED
```

* **PENDING** — the job is queued; no worker has picked it up yet.
* **RUNNING** — a worker has started the Qwen generation.
* **DONE** — finished; `message` reports how many recommendations were created
  (and, when the model was unavailable, that rule-based output was produced
  instead).
* **FAILED** — the job did not produce a result; `message` is a short,
  non-technical explanation.

`static/js/ai-job-poll.js` polls `recommendations:ai_job_status?job=<id>` every
three seconds and updates the banner. On a terminal state it shows the outcome
and reloads the page once so the new recommendations appear. Without JavaScript
the form still queues the job and a manual refresh shows the result.

## Handling worker / broker problems

Every failure path ends in a clear `FAILED` state with a plain-language message;
technical detail stays in the worker log and in the job's `error_code` column.

| Situation | Detected by | `error_code` | User message |
| --- | --- | --- | --- |
| Redis/broker unreachable at enqueue | `.delay()` raises a broker error | `broker_unavailable` | Background job service unavailable — ask an administrator to check Redis and the worker |
| Any other enqueue error | `.delay()` raises | `enqueue_failed` | The AI job could not be started — nothing was changed |
| Queued but no worker consuming | still `PENDING` past `AI_JOB_QUEUE_GRACE_SECONDS` | `worker_unavailable` | Queued but no worker picked it up — ask an administrator to check the worker |
| Worker died mid-run | still active past `AI_JOB_TIMEOUT_SECONDS` | `job_timeout` | Did not finish in time and was stopped — nothing was changed |
| Task raised, or refused the run | task exception / `GenerationResult.ok` is `False` | `task_error` / `run_refused` | The AI job stopped unexpectedly — please try again |

The staleness checks run on the job's own row, so they work even when the worker
is dead and writes nothing. They are also why the timeout is enforced here and
not by Celery: on Windows (`--pool=solo`) Celery cannot enforce time limits, so
the UI relies on these checks instead.

Queuing a run while another is already Pending/Running does **not** create a
duplicate — the existing job is returned and the user is told a job is already
running.

## Tenant isolation and permissions

Unchanged from the rest of the platform:

* An `AiJob` row carries `company` and `created_by`, so results are tied to the
  tenant and the user who started the job.
* The status endpoint resolves the company from the authenticated user, never
  from the URL, and reads only `AiJob.objects.for_company(company)`. An id
  belonging to another tenant returns **404**, so tenants cannot probe for
  existing ids.
* Enqueuing and viewing require the same roles as before:
  `MANAGER_ROLES` for generation, `VIEWER_ROLES` for the status endpoint.
  A Department Manager still cannot generate without a department.
* The worker re-derives the company from the stored job row — the browser never
  supplies a company id.

## Tests

The suite runs with no Redis and no worker: when `manage.py test` is detected,
`CELERY_TASK_ALWAYS_EAGER` makes tasks execute inline, so behaviour is
deterministic. Set `CELERY_TASK_ALWAYS_EAGER=true` to get the same behaviour
while developing without a worker.

```powershell
.\venv\Scripts\python.exe manage.py test apps.recommendations apps.analytics
```
