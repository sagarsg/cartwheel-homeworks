# HW4 review interface

A local, dependency-free interface for the Homework 4 workflow: open coding →
axial coding → structured binary labeling, over the HW3 Cartwheel traces.

## Run

```bash
# from the repo root
python -m analysis.review_app.server                 # http://127.0.0.1:8030
python -m analysis.review_app.server --port 8031
python -m analysis.review_app.server --source langfuse   # live slice (needs LANGFUSE_*)
```

Default trace source is `traces/support_traces.json`. State is read from and
written to `analysis/state/` (the same files the rubric grades). When
`LANGFUSE_*` is configured, accepted binary labels are also written back as
Langfuse scores.

## The five tabs

- **Review** — pick a session, read the full conversation (tool calls and tool
  results inline), and write one open code per trace. "No failure observed"
  records a reviewed-but-clean trace. Describe the **first** failure, then stop.
- **Taxonomy** — define 5–8 binary failure modes: name, binary definition,
  requirement source (`SPEC.md` id or a recorded revision), evaluator type,
  boundary vs. the nearest mode, positives and close negatives.
- **Labeling** — a trace × mode grid. Click a cell to cycle unset → Fail
  (present) → Pass (absent). Apply every mode to every reviewed trace.
- **Suggestions** — agent depth-search suggestions, kept separate from human
  annotations, each explicitly accepted or rejected (never a label until
  accepted). At least one rejected suggestion is required.
- **Progress** — review coverage, modes defined, and per-mode Fail/Pass/
  unlabeled counts reported as sample fractions (not prevalence).

## State files

`annotations.json` (open codes, history preserved), `patterns.json` (taxonomy),
`suggestions.json`, `samples.json` + `sample_manifest.json` (Part B batches),
and append-only `labels/<mode>.jsonl` (one line per label edit).
