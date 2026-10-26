"""ESG intelligence services.

Each sub-module owns one responsibility:

* :mod:`access` — role rules and tenant scoping (never trusts client input).
* :mod:`assessment` — the deterministic ESG data-completeness engine.
* :mod:`signals` — read-only summaries aggregated from the other modules.
* :mod:`catalog` — the default required-indicator catalog.
* :mod:`workflow` — indicator/value lifecycle (transactional, audited).
* :mod:`coach_ai` — the Qwen completeness coach (grounded, tenant-scoped).
"""
