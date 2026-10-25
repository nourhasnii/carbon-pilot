# Carbon Anomaly Detection

CarbonPilot uses scikit-learn `IsolationForest` to identify unusual carbon emission
patterns from historical approved activities.

This is a real, unsupervised machine-learning model fitted on each tenant's own
approved history. It is **not** a simulated or random score, and it does not call
any external AI service.

> This model only flags observations that are *statistically* unusual relative to
> the tenant's own history. It does **not** identify the business cause and makes
> **no legal, regulatory or scientific claim**. A human reviewer must confirm every
> finding.

## Where the code lives

| Concern | Location |
| --- | --- |
| Log model + enums | [apps/carbon/models.py](../apps/carbon/models.py) (`AnomalyLog`, `AnomalyType`, `AnomalySeverity`, `AnomalyStatus`) |
| ML service | [apps/carbon/services/anomaly_detection.py](../apps/carbon/services/anomaly_detection.py) |
| Views / URLs | [apps/carbon/views.py](../apps/carbon/views.py), [apps/carbon/urls.py](../apps/carbon/urls.py) |
| UI | `templates/carbon/anomaly_list.html`, `anomaly_detail.html`, dashboard card |

## Input data

Only activities that are **all** of the following are read:

- `status == APPROVED`
- `is_archived == False`
- belonging to a **single** company (`company` is never mixed)
- carrying a non-null `co2e_kg`

Draft, submitted, rejected and archived activities are excluded. Because the
queryset always starts from one company, data from different tenants is never
combined.

## Preprocessing and grouping

Activities are grouped by **department + category + reporting year** — a
"meaningful group". This keeps electricity of one department and period compared
with electricity of the same department and period, and never with unrelated waste
or transport rows.

Within a group, each observation is described by two numeric features:

- `co2e_kg`
- `quantity`

The feature matrix is standardised (zero mean, unit variance) with
`StandardScaler` so no single feature dominates purely by scale.

## Minimum data requirement

A group needs at least `MIN_GROUP_OBSERVATIONS = 8` approved observations before
the model runs. Smaller groups are skipped so the service never invents an anomaly
from thin data. When no group is eligible, the service returns:

> Insufficient historical data for anomaly detection.

and creates no log entries.

## Model

`IsolationForest` (an ensemble of random isolation trees) is fitted **per group**
with:

- `n_estimators = 200`
- `random_state = 42` (fixed seed)

The fixed seed makes the whole pipeline **deterministic**: the same data always
produces the same verdicts.

## Anomaly score

The forest's `decision_function` output is min-max normalised to `[0, 1]`, where
`1` is the most unusual observation in the group. That value is stored in
`AnomalyLog.score` (5 decimal places). Rows the forest labels `+1` (normal) are
ignored; only rows labelled `-1` become candidate findings.

## Severity and type

Each flagged observation is classified deterministically from the normalised score
and the ratio of the value to the group's historical **median**:

| Condition | Type |
| --- | --- |
| ratio ≥ `SPIKE_RATIO` (2.0) | `SPIKE` |
| otherwise, rising trend supported by the data and this is the latest value | `TREND_INCREASE` |
| otherwise | `OUTLIER` |

A "rising trend" is only considered supported when a linear fit has a positive
slope **and** the step/value correlation is ≥ `TREND_CORRELATION` (0.5). If the
data does not support a spike or a trend, the finding is reported as `OUTLIER`.

| Condition | Severity |
| --- | --- |
| score ≥ `HIGH_SCORE` (0.75) **and** ratio ≥ `SPIKE_RATIO` (2.0) | `HIGH` |
| score ≥ `MEDIUM_SCORE` (0.5) **or** ratio ≥ `MEDIUM_RATIO` (1.5) | `MEDIUM` |
| otherwise | `LOW` |

The message explains *what the model observed* in plain language (for example,
"CO2e is 10.0× the historical median for Operations · Electricity — significantly
higher than the usual pattern") and explicitly states that the model did not
determine the underlying business cause.

## Anomaly logging and duplicate prevention

Each finding is written to an `AnomalyLog` that belongs to exactly one company and
records the department, the related activity, the type, the score, the severity,
the explanation, the reporting period and the detection timestamp.

Re-running detection does not spam the log. Every entry has a deterministic
`fingerprint` (`company:activity:period`) backed by a database unique constraint,
so a repeated run **updates** the existing row instead of inserting a duplicate.
A reviewer's `status` (REVIEWED / DISMISSED) is preserved across re-runs.

## Security / tenant isolation

- Detection is always scoped to the company resolved from the authenticated user,
  never from a request parameter.
- Visibility follows the same role rules as activities: company-wide roles see the
  whole tenant, a Department Manager sees only their department, other members only
  their own findings.
- Cross-tenant probes return **404** (never 403) so ids cannot be enumerated.
- Running detection and triaging findings require the Company Owner / ESG
  Reporting Manager role (or platform Super Admin).

## Limitations

- Findings are *statistical* signals relative to the tenant's own past data, not
  proof of an error or of a regulatory breach.
- The model needs a reasonable amount of approved history per group; new tenants
  will often see "insufficient data".
- IsolationForest assumes anomalies are rare and different; a group made up
  entirely of unusual values may yield few or no flags.
- Changing the feature set, groups or thresholds changes the results. All
  thresholds are documented constants at the top of
  `services/anomaly_detection.py` and can be tuned deliberately.
- The model does not, and must not be presented to, provide legal, regulatory or
  scientific certainty.
