"""Replay a fixed set of prompts against a running Scotri API and record how each run went.

    services/.venv/Scripts/python.exe tools/benchmark/run_benchmark.py              # every case
    services/.venv/Scripts/python.exe tools/benchmark/run_benchmark.py --case <case id>
    services/.venv/Scripts/python.exe tools/benchmark/run_benchmark.py --report-only tools/benchmark/results/<file>.json

Talks to the API over HTTP only (POST /v1/chat, GET /v1/chat/{id}, /steps, /v1/learnings/{id}), so
it works against any deployment the token can reach. Records metrics, never the answer text: the
results file is safe to commit and compare over time (OPEN_ITEMS.md #68). Each run is compared with
the newest earlier results file, or the one --baseline names.

Plain stdlib. The token comes from SCOTRI_BENCH_TOKEN, else from
SCOTRI_DEV_AUTH_TOKEN in services/.env (local dev auth only); it is never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
CASES = HERE.parents[1] / "client" / "benchmark-cases.json"
"""Client-owned: the cases name this client's tickets and tables. Example:
examples/acme-retail/client/benchmark-cases.json."""
RESULTS = HERE / "results"
SCRATCH = ROOT / "scratch"
STASH = SCRATCH / ".benchmark-stash"

TERMINAL = {"succeeded", "failed", "cancelled", "incomplete"}
JIRA_WRITE = re.compile(r"app\.cli\s+jira-(comment|edit-comment|create-bug|attach)\b")
REFUSED = ("Denied by Scotri's tool scope", "<tool_use_error>")
RATE_LIMITED = "rate limit reached"
WHOLE_NOTEBOOK = re.compile(r"mirror-read\b(?!.*--lines).*\.ipynb", re.S)
RETRIEVE = re.compile(r'app\.cli\s+retrieve\b|"command":\s*"retrieve"')


# --- HTTP ------------------------------------------------------------------------------------------

def _token() -> str:
    token = os.environ.get("SCOTRI_BENCH_TOKEN")
    if token:
        return token
    env = ROOT / "services" / ".env"
    for line in env.read_text(encoding="utf-8").splitlines() if env.exists() else []:
        if line.startswith("SCOTRI_DEV_AUTH_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"')
    sys.exit("No token: set SCOTRI_BENCH_TOKEN, or SCOTRI_DEV_AUTH_TOKEN in services/.env.")


class Api:
    def __init__(self, base: str, token: str):
        self.base, self._token = base.rstrip("/"), token

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        req = urllib.request.Request(
            self.base + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return res.status, json.loads(res.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}


# --- isolation -------------------------------------------------------------------------------------

def _ticket_files(ticket: str) -> list[Path]:
    files = [p for p in SCRATCH.glob(f"{ticket}-*") if p.is_file()]
    attachments = SCRATCH / "jira-attachments" / ticket
    return files + ([attachments] if attachments.is_dir() else [])


@contextmanager
def set_aside(ticket: str | None, run_dir: Path):
    """While a ticket case runs, move that ticket's earlier deliverables (scratch/<ticket>-*, its
    downloaded attachments) out of the agent's sight, so the case measures a cold investigation, not a
    re-read of the last answer. Afterwards the run's own files go to `after/` and the earlier ones come
    back, so scratch/ ends exactly as it started. Yields whether isolation happened."""
    if not ticket:
        yield False
        return
    before, after = run_dir / ticket / "before", run_dir / ticket / "after"
    moved = []
    for path in _ticket_files(ticket):
        dest = before / path.relative_to(SCRATCH)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest))
        moved.append((dest, path))
    try:
        yield True
    finally:
        for path in _ticket_files(ticket):
            dest = after / path.relative_to(SCRATCH)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(dest))
        for dest, original in moved:
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dest), str(original))


# --- one case ---------------------------------------------------------------------------------------

def _ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def run_case(api: Api, case: dict, poll_seconds: float, timeout_seconds: float) -> dict:
    submitted = time.monotonic()
    code, start = api.call("POST", "/v1/chat", {"prompt": case["prompt"]})
    if code not in (200, 202) or "job_id" not in start:
        return {"id": case["id"], "status": "not_started", "error": f"HTTP {code}: {start.get('message', start)}"}
    job_id = start["job_id"]
    print(f"  {case['id']}: job {job_id}", flush=True)

    running_since = None
    poll: dict = {}
    while time.monotonic() - submitted < timeout_seconds:
        _, poll = api.call("GET", f"/v1/chat/{job_id}")
        status = poll.get("status")
        if status not in ("queued", None) and running_since is None:
            running_since = time.monotonic()
        if status in TERMINAL:
            break
        time.sleep(poll_seconds)
    else:
        api.call("POST", f"/v1/chat/{job_id}/cancel")
        poll = {**poll, "status": "benchmark_timeout"}
    finished = time.monotonic()

    _, steps_body = api.call("GET", f"/v1/chat/{job_id}/steps")
    steps = steps_body.get("steps", []) if isinstance(steps_body, dict) else []
    lcode, learning = api.call("GET", f"/v1/learnings/{job_id}")
    return _metrics(case, job_id, poll, steps, learning if lcode == 200 else None,
                    wall=finished - submitted,
                    queued=(running_since or finished) - submitted)


def _metrics(case: dict, job_id: str, poll: dict, steps: list, learning: dict | None,
             wall: float, queued: float) -> dict:
    calls = [s for s in steps if s.get("kind") == "tool_call"]
    tools: dict[str, int] = {}
    for s in calls:
        tools[s.get("tool") or "?"] = tools.get(s.get("tool") or "?", 0) + 1
    inputs = [s.get("input_summary") or "" for s in calls]
    previews = [s.get("result_preview") or "" for s in calls]
    stamps = [t for t in (_ts(s.get("timestamp")) for s in steps) if t]
    result = poll.get("result") or ""
    skills = [m.group(1) for s, i in zip(calls, inputs) if s.get("tool") == "Skill"
              for m in [re.search(r'"skill":\s*"([^"]+)"', i)] if m]

    m = {
        "id": case["id"],
        "job_id": job_id,
        "status": poll.get("status"),
        "wall_seconds": round(wall),
        "queued_seconds": round(queued),
        "run_seconds": round(wall - queued),
        "agent_seconds": round((max(stamps) - min(stamps)).total_seconds()) if len(stamps) > 1 else None,
        "tool_calls": len(calls),
        "tools": dict(sorted(tools.items(), key=lambda kv: -kv[1])),
        "skills": skills,
        "phases": (poll.get("progress") or {}).get("phases_seen", []),
        "refused_calls": sum(any(r in p for r in REFUSED) for p in previews),
        # A batch's per-item errors can sit past the preview's cut-off, so the answer saying so counts too.
        "rate_limited_calls": max(sum(RATE_LIMITED in p for p in previews),
                                  int(bool(re.search(r"rate[- ]limit", result, re.I)))),
        "failed_calls": sum((p.startswith("Exit code") or '"ok": false' in p) and RATE_LIMITED not in p
                            for p in previews),
        "whole_notebook_reads": sum(bool(WHOLE_NOTEBOOK.search(i)) for i in inputs),
        "jira_writes": sum(bool(JIRA_WRITE.search(i)) for i in inputs),
        "retrieve_seen": any(RETRIEVE.search(i) or RETRIEVE.search(p) for i, p in zip(inputs, previews)),
        "verdict": (learning or {}).get("verdict"),
        "confidence": (learning or {}).get("confidence"),
        "learning_recorded": learning is not None,
        "diagrams": len(poll.get("diagrams") or []),
        "unchecked_mentions": len(re.findall(r"\bunchecked\b", result)),
        "not_executed_mentions": len(re.findall(r"not executed", result, re.I)),
        "result_chars": len(result),
        "error": poll.get("error"),
    }
    m["checks"] = _checks(case.get("checks", {}), m, result)
    m["passed"] = all(c["passed"] for c in m["checks"])
    return m


def _checks(spec: dict, m: dict, result: str) -> list[dict]:
    out = [
        {"name": "succeeded", "passed": m["status"] == "succeeded", "detail": m["status"]},
        {"name": "no_jira_write", "passed": m["jira_writes"] == 0, "detail": m["jira_writes"]},
    ]
    if "verdict_in" in spec:
        out.append({"name": "verdict", "passed": m["verdict"] in spec["verdict_in"], "detail": m["verdict"]})
    if "min_diagrams" in spec:
        out.append({"name": "diagrams", "passed": m["diagrams"] >= spec["min_diagrams"], "detail": m["diagrams"]})
    if "max_unchecked" in spec:
        out.append({"name": "unchecked", "passed": m["unchecked_mentions"] <= spec["max_unchecked"],
                    "detail": m["unchecked_mentions"]})
    for needle in spec.get("must_mention", []):
        out.append({"name": f"mentions {needle!r}", "passed": needle in result, "detail": None})
    if spec.get("must_mention_any"):
        out.append({"name": "mentions any of " + "/".join(spec["must_mention_any"]),
                    "passed": any(n in result for n in spec["must_mention_any"]), "detail": None})
    return out


# --- report -----------------------------------------------------------------------------------------

def _previous(exclude: Path | None) -> Path | None:
    files = sorted(p for p in RESULTS.glob("*.json") if p != exclude)
    return files[-1] if files else None


def _delta(new, old) -> str:
    if new is None:
        return "–"
    if old is None:
        return f"{new}"
    d = new - old
    return f"{new} ({'+' if d > 0 else ''}{d})" if d else f"{new} (=)"


def report(run: dict, baseline: dict | None) -> str:
    prev = {c["id"]: c for c in (baseline or {}).get("cases", [])}
    lines = [
        f"# Benchmark {run['started_at']}",
        "",
        f"Commit `{run['commit']}`, API `{run['api']}`"
        + (f", compared with `{baseline['file']}` (commit `{baseline['commit']}`)" if baseline else ", no baseline"),
        "",
        "| Case | Status | Run s | Tool calls | Refused | Failed | Whole nb | retrieve | Verdict | Unchecked | Checks |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for c in run["cases"]:
        p = prev.get(c["id"], {})
        failed = [k["name"] for k in c.get("checks", []) if not k["passed"]]
        lines.append(
            f"| {c['id']} | {c.get('status')} | {_delta(c.get('run_seconds'), p.get('run_seconds'))} "
            f"| {_delta(c.get('tool_calls'), p.get('tool_calls'))} | {_delta(c.get('refused_calls'), p.get('refused_calls'))} "
            f"| {_delta(c.get('failed_calls'), p.get('failed_calls'))} | {_delta(c.get('whole_notebook_reads'), p.get('whole_notebook_reads'))} "
            f"| {'yes' if c.get('retrieve_seen') else 'no'} | {c.get('verdict') or '–'} "
            f"| {_delta(c.get('unchecked_mentions'), p.get('unchecked_mentions'))} "
            f"| {'pass' if c.get('passed') else 'FAIL: ' + ', '.join(failed or [c.get('error') or '?'])}"
            f"{' · RATE-LIMITED x' + str(c['rate_limited_calls']) + ', not comparable' if c.get('rate_limited_calls') else ''} |")
    total = [c.get("run_seconds") or 0 for c in run["cases"]]
    lines += ["", f"Total run time {sum(total)} s; {sum(c.get('passed', False) for c in run['cases'])}"
              f"/{len(run['cases'])} cases passed.",
              "", "`retrieve` is detected from command text and previews, so a call hidden inside a batch "
              "file can read as `no`. A case marked RATE-LIMITED lost governed reads to the per-user "
              "rate limit (every case runs as the same user), so its numbers aren't comparable: re-run it "
              "once the window resets. Run seconds are from leaving the queue to the final answer. Ticket cases run "
              "with that ticket's earlier scratch/ deliverables set aside (`isolated` in the JSON)."]
    return "\n".join(lines) + "\n"


# --- main -------------------------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default=os.environ.get("SCOTRI_BENCH_API", "http://localhost:8000"))
    ap.add_argument("--case", action="append", help="run only these case ids (repeatable)")
    ap.add_argument("--baseline", type=Path, help="results file to compare with (default: newest earlier one)")
    ap.add_argument("--poll-seconds", type=float, default=10)
    ap.add_argument("--timeout-minutes", type=float, default=30)
    ap.add_argument("--report-only", type=Path, help="print the report for an existing results file")
    args = ap.parse_args()

    if args.report_only:
        run = json.loads(args.report_only.read_text(encoding="utf-8"))
        base = args.baseline or _previous(args.report_only)
        baseline = json.loads(base.read_text(encoding="utf-8")) | {"file": base.name} if base else None
        print(report(run, baseline))
        return

    if not CASES.is_file():
        sys.exit(f"{CASES} does not exist. Copy examples/acme-retail/client/benchmark-cases.json there and "
                 f"replace the prompts with this client's own tickets and tables.")
    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    if args.case:
        unknown = set(args.case) - {c["id"] for c in cases}
        if unknown:
            sys.exit(f"Unknown case id(s): {', '.join(sorted(unknown))}")
        cases = [c for c in cases if c["id"] in args.case]

    api = Api(args.api, _token())
    code, health = api.call("GET", "/health")
    if code != 200:
        sys.exit(f"API at {args.api} is not healthy: HTTP {code} {health}")

    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True).stdout.strip() or "unknown"
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    run = {"started_at": started, "commit": commit, "api": args.api, "cases": []}
    print(f"Benchmark {started} at commit {commit}: {len(cases)} case(s), one at a time", flush=True)
    # One at a time on purpose: parallel runs share the governor and the engagement queue, which
    # would make the timings depend on each other.
    local = urlparse(args.api).hostname in ("localhost", "127.0.0.1", "::1")
    if not local:
        print("  (remote API: ticket cases run without isolation; earlier deliverables stay visible)")
    for case in cases:
        with set_aside(case.get("ticket") if local else None, STASH / started) as isolated:
            result = run_case(api, case, args.poll_seconds, args.timeout_minutes * 60)
        run["cases"].append(result | {"isolated": isolated})

    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{started}.json"
    out.write_text(json.dumps(run, indent=1) + "\n", encoding="utf-8")
    base = args.baseline or _previous(out)
    baseline = json.loads(base.read_text(encoding="utf-8")) | {"file": base.name} if base else None
    text = report(run, baseline)
    out.with_suffix(".md").write_text(text, encoding="utf-8")
    print("\n" + text + f"\nSaved {out.relative_to(ROOT)} and the .md report next to it.")
    sys.exit(0 if all(c.get("passed") for c in run["cases"]) else 1)


if __name__ == "__main__":
    main()
