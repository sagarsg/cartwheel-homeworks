"""HW4 review-interface backend.

A dependency-free standard-library HTTP server. It serves the single-file app
in ``ui/`` and a small JSON API over the plain files in
``analysis/state/``, reusing the reference server's read/write/Langfuse-sync
helpers so the committed state layout stays compatible.

What this backend adds over the reference (``analysis/server.py``):

  - ``GET  /api/sessions``   the reviewable traces, normalized and grouped by
                             session (``cartwheel.session_id`` when present,
                             otherwise the scenario id), each conversation in
                             chronological order with tool calls and tool
                             results inline. This is the data the reviewer
                             reads; the reference server had no trace endpoint
                             because the reference UI loaded traces from a
                             pasted sample file.
  - ``POST /api/sample``     build one Part B review batch with a named
                             selection method and append it to the sample set
                             and ``state/sample_manifest.json``.
  - ``GET/POST /api/labels`` the structured binary labels: one present/absent
                             decision per (trace, final mode), mirrored to
                             ``state/labels/<mode>.jsonl`` (append-only) and,
                             when Langfuse is configured, to a trace score.

The file-backed endpoints (``annotations``, ``patterns``, ``suggestions``,
``samples``) are delegated unchanged to the reference handler, so open codes,
the taxonomy, agent suggestions, and the live sample set live in the same
``analysis/state/`` files the grading rubric asks for.

Run it:

    python analysis/review_app/server.py                  # serve on :8030
    python analysis/review_app/server.py --port 8031
    python analysis/review_app/server.py --source traces/support_traces.json

``--source`` is the Module 1 / HW3 trace export to review (default
``traces/support_traces.json`` at the repo root, or ``langfuse`` to pull the
live slice when ``LANGFUSE_*`` is configured).
"""

from __future__ import annotations

import argparse
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

# Reuse the reference server's primitives so the two interfaces share one
# state contract (same files, same atomic writes, same Langfuse score sync).
from analysis import server as ref
from analysis.helpers import _state, selection
from analysis.helpers.normalization import normalize_traces

HERE = Path(__file__).resolve().parent
UI_DIR = HERE / "ui"
REPO_ROOT = HERE.parent.parent

# Set by main() before the server starts; the handler reads them.
SESSIONS: list[dict[str, Any]] = []
TRACES_BY_ID: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# loading and grouping
# ---------------------------------------------------------------------------


def _load_source(source: str) -> list[dict[str, Any]]:
    """Return normalized trace records from an export path or ``langfuse``."""
    if source.lower() == "langfuse":
        from analysis.helpers import langfuse_io

        if not langfuse_io.is_configured():
            raise SystemExit(
                "source 'langfuse' requires the LANGFUSE_* environment; "
                "pass a trace export path instead."
            )
        # fetch_traces returns records already in the normalized shape.
        return langfuse_io.fetch_traces()
    path = Path(source)
    if not path.is_absolute():
        path = REPO_ROOT / path
    if not path.exists():
        raise SystemExit(f"trace source not found: {path}")
    doc = json.loads(path.read_text())
    raw = doc.get("traces", doc) if isinstance(doc, dict) else doc
    return normalize_traces(raw)


def _group_by_session(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group normalized traces into conversations.

    Cartwheel writes one Langfuse trace per user turn, so a multi-turn
    conversation is several traces that share a ``cartwheel.session_id``. We
    group on that id so a follow-up turn is shown with the earlier turn's tool
    calls rather than in isolation. When the export carries no session id
    (the HW3 single-turn export), each trace is its own session keyed by its
    scenario id, which keeps every trace individually reviewable.
    """
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for rec in records:
        meta = rec.get("meta", {})
        sid = meta.get("session_id") or meta.get("scenario_id") or rec["id"]
        if sid not in groups:
            groups[sid] = {"session_id": sid, "meta": meta, "traces": []}
            order.append(sid)
        groups[sid]["traces"].append(rec)
    sessions = []
    for sid in order:
        g = groups[sid]
        g["traces"].sort(key=lambda r: r.get("timestamp") or "")
        sessions.append(g)
    return sessions


# ---------------------------------------------------------------------------
# labels
# ---------------------------------------------------------------------------


def _labels_dir() -> Path:
    return _state.state_root() / "labels"


def _read_all_labels() -> list[dict[str, Any]]:
    """Return the current (non-superseded) label per (trace, mode).

    Label files are append-only: a changed label writes a new line and the
    latest line for a (trace_id, mode) pair wins. This mirrors the helper
    package's append-only convention so the open-code-to-label history stays
    inspectable.
    """
    out: dict[tuple[str, str], dict[str, Any]] = {}
    ldir = _labels_dir()
    if not ldir.exists():
        return []
    for f in sorted(ldir.glob("*.jsonl")):
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = (str(row.get("trace_id")), str(row.get("mode")))
            out[key] = row
    return list(out.values())


def _append_label(row: dict[str, Any]) -> None:
    mode = str(row.get("mode"))
    path = _labels_dir() / f"{mode}.jsonl"
    _state.append_jsonl(path, row)


def _sync_label_score(row: dict[str, Any]) -> bool:
    """Write one label to Langfuse as a score; no-op when unconfigured."""
    try:
        from analysis.helpers import langfuse_io
    except Exception:
        return False
    if not langfuse_io.is_configured():
        return False
    label = row.get("label")
    if label not in (0, 1, "0", "1"):
        return False
    langfuse_io.write_label_score(
        trace_id=str(row.get("trace_id")),
        mode=str(row.get("mode")),
        label=int(label),
        comment=row.get("note"),
    )
    return True


# ---------------------------------------------------------------------------
# request handler
# ---------------------------------------------------------------------------


class ReviewHandler(ref.ReviewHandler):
    """Adds the trace, sampling, and label routes to the reference handler."""

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send_file(UI_DIR / "index.html", "text/html; charset=utf-8")
            return
        if path.startswith("/ui/"):
            asset = UI_DIR / path[len("/ui/"):]
            if asset.is_file() and UI_DIR in asset.resolve().parents:
                self._send_file(asset, ref._guess_type(asset))
                return
        if path == "/api/sessions":
            self._send_json({"sessions": SESSIONS, "count": len(TRACES_BY_ID)})
            return
        if path == "/api/labels":
            self._send_json(_read_all_labels())
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/sample":
            self._handle_sample()
            return
        if path == "/api/labels":
            self._handle_label()
            return
        super().do_POST()

    # -- new routes -------------------------------------------------------

    def _handle_sample(self) -> None:
        body = self._read_body() or {}
        strategy = str(body.get("strategy", "random"))
        try:
            k = int(body.get("k", 15))
        except (TypeError, ValueError):
            self._send_json({"error": "k must be an integer"}, status=400)
            return
        batch = str(body.get("batch", strategy))
        exclude = set(str(x) for x in body.get("exclude_ids", []))

        traces = list(TRACES_BY_ID.values())
        picks = selection.select(traces, k=k, strategy=strategy, exclude_ids=exclude)
        for p in picks:
            p["batch"] = batch

        # Append to the live sample set and record the manifest entry.
        samples = ref._read_json(ref.API_FILES["/api/samples"], [])
        if not isinstance(samples, list):
            samples = []
        seen = {s.get("trace_id") for s in samples if isinstance(s, dict)}
        for p in picks:
            tid = p["trace_id"]
            if tid in seen:
                continue
            t = TRACES_BY_ID[tid]
            samples.append(
                {
                    "trace_id": tid,
                    "batch": batch,
                    "reason": p["reason"],
                    "meta": t.get("meta"),
                    "features": t.get("features"),
                }
            )
            seen.add(tid)
        ref._write_json(ref.API_FILES["/api/samples"], samples)
        self._record_manifest(batch, strategy, k, picks)
        self._send_json({"ok": True, "picks": picks, "total": len(samples)})

    def _record_manifest(
        self, batch: str, strategy: str, k: int, picks: list[dict[str, Any]]
    ) -> None:
        path = _state.state_path("sample_manifest.json")
        manifest = ref._read_json(path, {})
        if not isinstance(manifest, dict) or "batches" not in manifest:
            manifest = {"batches": []}
        manifest["batches"].append(
            {
                "batch": batch,
                "strategy": strategy,
                "k": k,
                "picks": picks,
            }
        )
        ref._write_json(path, manifest)

    def _handle_label(self) -> None:
        body = self._read_body()
        rows = body if isinstance(body, list) else [body]
        written = 0
        synced = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            if not row.get("trace_id") or not row.get("mode"):
                continue
            if row.get("label") not in (0, 1, "0", "1"):
                continue
            row = {**row, "label": int(row["label"])}
            _append_label(row)
            written += 1
            try:
                if _sync_label_score(row):
                    synced += 1
            except Exception as exc:  # pragma: no cover - network-only path
                self._send_json(
                    {"error": f"Langfuse score write failed: {exc}",
                     "cached_locally": True, "written": written},
                    status=502,
                )
                return
        result = {"ok": True, "written": written}
        if synced:
            result["langfuse_scores_written"] = synced
        self._send_json(result)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8030)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--source",
        default="traces/support_traces.json",
        help="trace export to review, or 'langfuse' for the live slice",
    )
    args = parser.parse_args()

    global SESSIONS, TRACES_BY_ID
    records = _load_source(args.source)
    SESSIONS = _group_by_session(records)
    TRACES_BY_ID = {r["id"]: r for r in records}

    ref.STATE_DIR.mkdir(parents=True, exist_ok=True)
    _labels_dir().mkdir(parents=True, exist_ok=True)

    server = ThreadingHTTPServer((args.host, args.port), ReviewHandler)
    url = f"http://{args.host}:{args.port}/"
    print(f"HW4 review interface on {url}")
    print(f"loaded {len(TRACES_BY_ID)} traces in {len(SESSIONS)} sessions "
          f"from {args.source}")
    print(f"serving state from {ref.STATE_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
