"""
================================================================================
AI-PerfBench REST API Server + Web Dashboard  —  perf_bench_server.py
================================================================================
Serves the benchmark engine over HTTP with a real-time web dashboard.

Designed for:
  · Headless deployment on Raspberry Pi 5 / Linux servers
  · Local web dashboard access from any browser on the same network
  · CI/CD integration via the REST API

Deps:  pip install flask psutil
       (perf_bench.py must be in the same directory)

Quick-start:
    python perf_bench_server.py                         # binds 0.0.0.0:5000
    python perf_bench_server.py --port 8080
    python perf_bench_server.py --host 127.0.0.1

REST API:
    GET  /                          — web dashboard (HTML)
    GET  /api/health                — liveness probe
    GET  /api/info                  — live system info snapshot
    POST /api/runs                  — start benchmark (async) → {run_id}
    GET  /api/runs                  — list all runs + status summary
    GET  /api/runs/<run_id>         — poll status, progress, and full result
    DELETE /api/runs/<run_id>       — cancel a queued/running benchmark
    GET  /api/results/<filename>    — download a saved JSON result file

POST /api/runs  body (JSON):
    {
        "cmd_list":   ["python", "model.py", "--arg", "val"],  // preferred
        "cmd":        "python model.py --arg val",             // alternative
        "trials":      7,
        "sample_ms":   1.0,
        "warmup_drop": 1,
        "spike_mb":    2.0,
        "spike_boost": 10.0,
        "outlier_z":   2.5,
        "output_file": "run1.json"
    }

GET /api/runs/<run_id>  response while running:
    {
        "run_id":   "a3f9c1e2d4b8",
        "status":   "running",
        "trials":   7,
        "progress": {
            "completed_trials": 3,
            "latest_trial": { "trial_index": 2, "wall_time_s": 2.43, ... }
        },
        "result":   null
    }
================================================================================
"""

from __future__ import annotations

import argparse
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from flask import Flask, abort, jsonify, render_template, request, send_file
except ImportError:
    import sys
    sys.exit(
        "[FATAL] Flask is required:\n"
        "    pip install flask\n\n"
        "The core profiler (perf_bench.py) does NOT require Flask."
    )

from perf_bench import (
    IS_WINDOWS,
    ProfilerConfig,
    PerfBench,
    TrialResult,
    collect_system_info,
    parse_cmd_string,
    report_to_dict,
    write_json_report,
)

# ============================================================================
# Application setup
# ============================================================================

app = Flask(__name__, template_folder="templates")
app.json.sort_keys = False

_results_dir: Path = Path("results")

# Thread-safe in-memory run registry.
# Each entry: { status, started_at, finished_at, cmd_list, trials,
#               progress, result, error }
_registry: dict[str, dict] = {}
_registry_lock = threading.Lock()


# ============================================================================
# Internal helpers
# ============================================================================

def _new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _update_registry(run_id: str, **kwargs) -> None:
    with _registry_lock:
        if run_id in _registry:
            _registry[run_id].update(kwargs)


def _is_cancelled(run_id: str) -> bool:
    with _registry_lock:
        return _registry.get(run_id, {}).get("status") == "cancelled"


def _trial_summary(trial: TrialResult) -> dict:
    """Lightweight per-trial dict for the progress field."""
    return {
        "trial_index":          trial.trial_index,
        "is_cold_start":        trial.is_cold_start,
        "wall_time_s":          trial.wall_time_s,
        "rss_peak_net_bytes":   trial.rss_peak_net_bytes,
        "cpu_mean_percent":     trial.cpu_mean_percent,
        "adaptive_boost_count": trial.adaptive_boost_count,
        "accuracy_percent":     trial.accuracy.accuracy_percent,
    }


def _run_benchmark_thread(run_id: str, cfg: ProfilerConfig) -> None:
    """
    Background thread that executes one benchmark run, streams per-trial
    progress into the registry, and stores the final result.
    """
    _update_registry(run_id, status="running", started_at=_iso_now())

    if _is_cancelled(run_id):
        return

    def on_trial_complete(trial_num: int, total: int, trial: TrialResult) -> None:
        if _is_cancelled(run_id):
            return
        with _registry_lock:
            if run_id not in _registry:
                return
            prog = _registry[run_id].setdefault("progress", {})
            prog["completed_trials"] = trial_num
            prog["latest_trial"] = _trial_summary(trial)

    try:
        bench = PerfBench(cfg, on_trial_complete=on_trial_complete)
        report = bench.run()

        if cfg.output_path:
            write_json_report(report, cfg.output_path)

        _update_registry(
            run_id,
            status="completed",
            finished_at=_iso_now(),
            result=report_to_dict(report),
            error=None,
        )
    except BaseException as exc:
        # BaseException, not Exception, on purpose. perf_bench.py is also a CLI
        # tool: on a fatal trial error (e.g. the target command does not exist)
        # it calls sys.exit(), which raises SystemExit. SystemExit derives from
        # BaseException, so an `except Exception` clause would NOT catch it --
        # the worker thread would die silently and the run would stay pinned at
        # status="running" forever, with no error ever surfaced to the client.
        # Catching BaseException guarantees every terminated run is reported.
        _update_registry(
            run_id,
            status="error",
            finished_at=_iso_now(),
            result=None,
            error=f"{type(exc).__name__}: {exc}",
        )
        traceback.print_exc()


# ============================================================================
# Dashboard route
# ============================================================================

@app.route("/", methods=["GET"])
def dashboard():
    """Serve the web dashboard HTML page."""
    return render_template("dashboard.html")


# ============================================================================
# REST API routes
# ============================================================================

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": _iso_now()})


@app.route("/api/info", methods=["GET"])
def system_info():
    return jsonify(collect_system_info())


@app.route("/api/runs", methods=["GET"])
def list_runs():
    with _registry_lock:
        runs = [
            {
                "run_id":      rid,
                "status":      v["status"],
                "started_at":  v.get("started_at"),
                "finished_at": v.get("finished_at"),
                "cmd_list":    v.get("cmd_list"),
                "trials":      v.get("trials"),
            }
            for rid, v in _registry.items()
        ]
    return jsonify({"count": len(runs), "runs": runs})


@app.route("/api/runs", methods=["POST"])
def start_run():
    """
    Submit a new benchmark run asynchronously.

    Accepts either:
        { "cmd_list": ["python", "model.py", "--arg", "5"] }   (preferred)
        { "cmd": "python model.py --arg 5" }                    (parsed server-side)
    """
    body = request.get_json(silent=True) or {}

    # Resolve command ──────────────────────────────────────────────────────
    cmd_list = body.get("cmd_list")
    if not cmd_list:
        raw_cmd = body.get("cmd", "").strip()
        if raw_cmd:
            cmd_list = parse_cmd_string(raw_cmd)

    if not cmd_list or not isinstance(cmd_list, list) or not all(
        isinstance(t, str) and t for t in cmd_list
    ):
        return jsonify({
            "error": "'cmd_list' or 'cmd' is required and must resolve to a non-empty list of strings",
            "example": {"cmd_list": ["python", "heavy_math.py"]},
        }), 400

    # Parameters ───────────────────────────────────────────────────────────
    trials      = max(1, int(body.get("trials",      7)))
    sample_ms   = max(0.01, float(body.get("sample_ms",  1.0)))
    warmup_drop = max(0, int(body.get("warmup_drop", 1)))
    spike_mb    = max(0.1, float(body.get("spike_mb",  2.0)))
    spike_boost = max(1.0, float(body.get("spike_boost", 10.0)))
    outlier_z   = max(1.0, float(body.get("outlier_z",  2.5)))
    output_file = body.get("output_file")

    output_path: Optional[Path] = (
        _results_dir / output_file if output_file else None
    )

    cfg = ProfilerConfig(
        cmd_list=cmd_list,
        trials=trials,
        sample_interval_s=sample_ms / 1000.0,
        spike_rss_delta_bytes=int(spike_mb * 1024 * 1024),
        spike_boost_factor=spike_boost,
        warmup_drop_trials=warmup_drop,
        outlier_z_threshold=outlier_z,
        output_path=output_path,
    )

    run_id = _new_run_id()
    with _registry_lock:
        _registry[run_id] = {
            "status":      "queued",
            "started_at":  None,
            "finished_at": None,
            "cmd_list":    cmd_list,
            "trials":      trials,
            "output_file": output_file,
            "progress":    {"completed_trials": 0, "latest_trial": None},
            "result":      None,
            "error":       None,
        }

    thread = threading.Thread(
        target=_run_benchmark_thread,
        args=(run_id, cfg),
        daemon=True,
        name=f"bench-{run_id}",
    )
    thread.start()

    return jsonify({"run_id": run_id, "status": "queued", "trials": trials}), 202


@app.route("/api/runs/<run_id>", methods=["GET"])
def get_run(run_id: str):
    with _registry_lock:
        entry = _registry.get(run_id)
    if entry is None:
        abort(404, description=f"No run with id '{run_id}'")
    return jsonify({"run_id": run_id, **entry})


@app.route("/api/runs/<run_id>", methods=["DELETE"])
def cancel_run(run_id: str):
    """
    Mark a run as cancelled.
    Queued runs are cancelled before they start.
    Running runs complete their current trial then stop (data integrity preserved).
    """
    with _registry_lock:
        if run_id not in _registry:
            abort(404, description=f"No run with id '{run_id}'")
        current = _registry[run_id]["status"]
        if current in ("completed", "error", "cancelled"):
            return jsonify({
                "run_id":   run_id,
                "status":   current,
                "message":  f"Run already in terminal state: {current}",
            }), 409
        _registry[run_id]["status"]      = "cancelled"
        _registry[run_id]["finished_at"] = _iso_now()

    return jsonify({"run_id": run_id, "status": "cancelled"})


@app.route("/api/results/<path:filename>", methods=["GET"])
def download_result(filename: str):
    """Download a saved JSON result file. Path traversal is blocked."""
    safe_path = (_results_dir / filename).resolve()
    try:
        safe_path.relative_to(_results_dir.resolve())
    except ValueError:
        abort(403, description="Path traversal denied")

    if not safe_path.exists():
        abort(404, description=f"Result file '{filename}' not found")

    return send_file(str(safe_path), mimetype="application/json", as_attachment=True)


# ── Error handlers ─────────────────────────────────────────────────────────

@app.errorhandler(400)
def bad_request(err):
    return jsonify({"error": str(err.description)}), 400

@app.errorhandler(403)
def forbidden(err):
    return jsonify({"error": str(err.description)}), 403

@app.errorhandler(404)
def not_found(err):
    return jsonify({"error": str(err.description)}), 404

@app.errorhandler(409)
def conflict(err):
    return jsonify({"error": str(err.description)}), 409


# ============================================================================
# CLI & entry point
# ============================================================================

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="perf_bench_server",
        description=(
            "AI-PerfBench Web Dashboard Server\n\n"
            "Open http://localhost:5000 in your browser after starting.\n\n"
            "Quick-start:\n"
            "    python perf_bench_server.py\n"
            "    python perf_bench_server.py --port 8080\n"
            "    python perf_bench_server.py --host 127.0.0.1"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--host", default="0.0.0.0",
                   help="Bind address (default: 0.0.0.0 — all LAN interfaces)")
    p.add_argument("--port", type=int, default=5000,
                   help="TCP port (default: 5000)")
    p.add_argument("--results-dir", default="results",
                   help="Directory for saved JSON result files (default: results/)")
    p.add_argument("--debug", action="store_true",
                   help="Enable Flask debug mode (development only)")
    return p


def main() -> None:
    global _results_dir

    parser = _build_parser()
    args = parser.parse_args()

    _results_dir = Path(args.results_dir)
    _results_dir.mkdir(parents=True, exist_ok=True)

    display_host = "localhost" if args.host in ("0.0.0.0", "::") else args.host

    print(f"\n[AI-PerfBench Dashboard]")
    print(f"  Open in browser  : http://{display_host}:{args.port}")
    if args.host in ("0.0.0.0", "::"):
        print(f"  LAN access       : http://<your-ip>:{args.port}")
    print(f"  Results dir      : {_results_dir.resolve()}")
    print(f"  Press Ctrl+C to stop\n")

    app.run(
        host=args.host,
        port=args.port,
        debug=args.debug,
        threaded=True,
        use_reloader=False,
    )


if __name__ == "__main__":
    main()
