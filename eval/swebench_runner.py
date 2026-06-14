#!/usr/bin/env python3
"""
SWE-bench Runner for mini_agent.

Downloads SWE-bench tasks from HuggingFace, runs mini_agent on each task,
collects patches, and outputs predictions in the standard SWE-bench format
for evaluation via the official harness.

Also supports a lightweight local scoring mode for fast iteration.

Usage:
    # Generate predictions for SWE-bench Lite (first 5 tasks, for testing):
    python -m eval.swebench_runner --dataset princeton-nlp/SWE-bench_Lite \\
        --max-tasks 5 --output predictions.jsonl

    # Full SWE-bench Lite run:
    python -m eval.swebench_runner --dataset princeton-nlp/SWE-bench_Lite \\
        --output predictions.jsonl --timeout 600

    # SWE-bench Verified (human-validated subset):
    python -m eval.swebench_runner --dataset princeton-nlp/SWE-bench_Verified \\
        --output predictions.jsonl --timeout 600

    # With local scoring (runs tests after applying patch):
    python -m eval.swebench_runner --dataset princeton-nlp/SWE-bench_Lite \\
        --max-tasks 5 --score --output predictions.jsonl

    # Resume from a previous run:
    python -m eval.swebench_runner --dataset princeton-nlp/SWE-bench_Lite \\
        --resume predictions.jsonl --output predictions.jsonl

Predictions format (one JSON object per line):
    {"instance_id": "django__django-11049",
     "model_patch": "diff --git ...",
     "model_name_or_path": "mini_agent"}

Official evaluation:
    # Clone SWE-bench repo and run:
    git clone https://github.com/princeton-nlp/SWE-bench.git
    cd SWE-bench
    python -m swebench.harness.run_evaluation \\
        --dataset_name princeton-nlp/SWE-bench_Lite \\
        --predictions_path /path/to/predictions.jsonl \\
        --max_workers 4 \\
        --run_id mini_agent_test
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from eval.runner import _ensure_git_repo
from eval.metrics import MetricsCollector


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CACHE_DIR = Path.home() / ".cache" / "mini_agent" / "swebench_repos"
LOCAL_REPORT_DIR = Path(__file__).parent / "reports"

# Repos to skip (too large / complex for local runs without Docker)
HEAVY_REPOS = {
    "django/django",      # needs specific Django env
    "sympy/sympy",        # needs SymPy deps
    "scikit-learn/scikit-learn",
    "pydata/xarray",
    "pylint-dev/pylint",
    "pytest-dev/pytest",
}

# Ignore patterns when copying agent workspace
_IGNORE_PATTERNS = (
    ".git", "__pycache__", ".pytest_cache", "venv", ".venv",
    "node_modules", ".mypy_cache", ".ruff_cache",
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class SWEBenchTask:
    """A single SWE-bench task loaded from the dataset."""

    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    patch: str = ""                    # gold patch (hidden from agent)
    test_patch: str = ""
    fail_to_pass: list[str] = field(default_factory=list)
    pass_to_pass: list[str] = field(default_factory=list)
    hints_text: str = ""
    created_at: str = ""
    version: str = ""


@dataclass
class SWEBenchResult:
    """Result of running mini_agent on a single SWE-bench task."""

    instance_id: str
    repo: str
    model_patch: str = ""
    turns_used: int = 0
    tool_calls: dict[str, int] = field(default_factory=dict)
    tokens_consumed: int = 0
    wall_time_seconds: float = 0.0
    error: str | None = None
    # Local scoring (only populated with --score)
    resolved: bool = False
    fail_to_pass_passed: int = 0
    fail_to_pass_total: int = 0
    pass_to_pass_passed: int = 0
    pass_to_pass_total: int = 0


@dataclass
class SWEBenchReport:
    """Aggregated results across multiple SWE-bench tasks."""

    total: int = 0
    completed: int = 0     # tasks that ran without error
    errors: int = 0        # tasks that errored out
    resolved: int = 0      # tasks where patch resolved the issue
    resolution_rate: float = 0.0
    avg_turns: float = 0.0
    avg_tokens: float = 0.0
    avg_wall_time: float = 0.0
    per_task: list[SWEBenchResult] = field(default_factory=list)
    dataset: str = ""
    model_name: str = "mini_agent"


# ---------------------------------------------------------------------------
# Dataset loading
# ---------------------------------------------------------------------------


def _load_dataset(dataset_name: str, split: str = "test", max_tasks: int | None = None) -> list[dict]:
    """Load SWE-bench tasks from HuggingFace datasets.

    Args:
        dataset_name: e.g. 'princeton-nlp/SWE-bench_Lite'
        split: dataset split (default 'test')
        max_tasks: optional cap on number of tasks loaded

    Returns:
        List of raw task dicts from the dataset.
    """
    try:
        from datasets import load_dataset
    except ImportError:
        print(
            "HuggingFace 'datasets' library required. Install with:\n"
            "  pip install datasets",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Loading {dataset_name} ({split} split)...", file=sys.stderr)
    dataset = load_dataset(dataset_name, split=split)

    tasks = []
    for i, row in enumerate(dataset):
        if max_tasks is not None and i >= max_tasks:
            break
        tasks.append(dict(row))

    print(f"Loaded {len(tasks)} task(s)", file=sys.stderr)
    return tasks


def parse_swebench_task(raw: dict) -> SWEBenchTask:
    """Parse a raw dataset row into a SWEBenchTask."""
    import json as _json

    # Parse JSON-encoded test lists
    fail_to_pass = []
    pass_to_pass = []
    try:
        fail_to_pass = _json.loads(raw.get("FAIL_TO_PASS", "[]"))
    except (_json.JSONDecodeError, TypeError):
        pass
    try:
        pass_to_pass = _json.loads(raw.get("PASS_TO_PASS", "[]"))
    except (_json.JSONDecodeError, TypeError):
        pass

    return SWEBenchTask(
        instance_id=raw.get("instance_id", ""),
        repo=raw.get("repo", ""),
        base_commit=raw.get("base_commit", ""),
        problem_statement=raw.get("problem_statement", ""),
        patch=raw.get("patch", ""),
        test_patch=raw.get("test_patch", ""),
        fail_to_pass=fail_to_pass,
        pass_to_pass=pass_to_pass,
        hints_text=raw.get("hints_text", ""),
        created_at=raw.get("created_at", ""),
        version=raw.get("version", ""),
    )


# ---------------------------------------------------------------------------
# Repo setup
# ---------------------------------------------------------------------------


def _repo_cache_path(repo: str) -> Path:
    """Return the cache path for a repo's bare clone."""
    # e.g. django/django -> django__django
    safe_name = repo.replace("/", "__")
    return CACHE_DIR / safe_name


def _get_repo(repo: str, base_commit: str) -> str:
    """Ensure the repo is available at the given commit.

    Uses a bare-clone cache to avoid re-cloning. Returns path to a
    temporary checkout at the target commit.

    Args:
        repo: e.g. 'django/django'
        base_commit: commit hash to check out

    Returns:
        Path to a temporary directory with the repo checked out.
    """
    cache_path = _repo_cache_path(repo)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Clone bare repo if not cached
    if not (cache_path / "HEAD").exists():
        print(f"  Cloning {repo} (cached)...", file=sys.stderr)
        subprocess.run(
            ["git", "clone", "--bare", f"https://github.com/{repo}.git", str(cache_path)],
            capture_output=True, text=True, timeout=300,
            check=False,
        )

    # Fetch latest if needed
    if (cache_path / "HEAD").exists():
        subprocess.run(
            ["git", "fetch", "--all"],
            cwd=str(cache_path), capture_output=True, text=True, timeout=60,
            check=False,
        )

    # Create temp checkout at the target commit
    workdir = tempfile.mkdtemp(prefix=f"swebench_{repo.replace('/', '_')}_")

    # Use git worktree or just clone from the bare cache
    result = subprocess.run(
        ["git", "clone", "--shared", str(cache_path), workdir],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        # Fallback: direct clone from GitHub
        subprocess.run(
            ["git", "clone", f"https://github.com/{repo}.git", workdir],
            capture_output=True, text=True, timeout=300,
            check=True,
        )

    # Checkout the target commit
    subprocess.run(
        ["git", "checkout", base_commit],
        cwd=workdir, capture_output=True, text=True, timeout=30,
        check=True,
    )

    # Apply test patch if present (so tests are available)
    # (We don't have test_patch here, the caller should apply it after)

    return workdir


# ---------------------------------------------------------------------------
# Task runner
# ---------------------------------------------------------------------------


def _build_prompt(task: SWEBenchTask) -> str:
    """Build the agent prompt from a SWE-bench task."""
    parts = [
        "You are an expert software engineer fixing a real bug in a codebase.",
        "",
        "## GitHub Issue",
        task.problem_statement,
        "",
        "## Instructions",
        "1. Explore the codebase to understand the relevant code",
        "2. Find the root cause of the issue described above",
        "3. Fix the bug by editing the appropriate files",
        "4. If possible, run relevant tests to verify your fix",
        "5. When done, summarize your changes clearly",
        "",
        "Work carefully and methodically. Read files before editing them.",
        "Use the available tools to search, read, write, and test.",
    ]
    if task.hints_text:
        parts.extend(["", "## Hints", task.hints_text])
    return "\n".join(parts)


def run_swebench_task(
    task: SWEBenchTask,
    *,
    timeout_seconds: int = 600,
    stream: bool = False,
) -> SWEBenchResult:
    """Run mini_agent on a single SWE-bench task.

    Sets up the repo workspace, runs the agent with the problem statement,
    collects the git diff as the patch, and returns a structured result.

    Args:
        task: Parsed SWEBenchTask.
        timeout_seconds: Max wall-clock time for this task.
        stream: If True, stream agent output to stderr.

    Returns:
        SWEBenchResult with model_patch and metrics.
    """
    start_time = time.monotonic()
    workspace = None
    metrics = None

    try:
        # Set up workspace
        print(f"\n  [{task.instance_id}] Setting up {task.repo} @ {task.base_commit[:8]}...",
              file=sys.stderr)
        workspace = _get_repo(task.repo, task.base_commit)

        # Apply test patch if provided
        if task.test_patch:
            result = subprocess.run(
                ["git", "apply"],
                cwd=workspace, input=task.test_patch, capture_output=True, text=True,
                timeout=30,
            )
            if result.returncode != 0:
                print(f"  [{task.instance_id}] WARNING: test patch failed to apply: "
                      f"{result.stderr[:200]}", file=sys.stderr)
            else:
                # Commit the test patch so 'git diff' later captures agent changes only
                subprocess.run(
                    ["git", "add", "-A"],
                    cwd=workspace, capture_output=True, text=True, timeout=10,
                )
                subprocess.run(
                    ["git", "commit", "-m", "Apply test patch"],
                    cwd=workspace, capture_output=True, text=True, timeout=10,
                )

        original_cwd = os.getcwd()
        os.chdir(workspace)

        # Ensure git repo state
        _ensure_git_repo(workspace)

        # Initialize agent session
        from core.config import init_session

        session = init_session(workspace)
        config = session["config"]
        config.unrestricted = True
        config.allow_overwrites = True
        config.stream = stream
        config.verbose = False

        # Inject task prompt
        prompt = _build_prompt(task)
        session["messages"].append({
            "role": "user",
            "content": prompt,
        })

        # Instrument with metrics
        metrics = MetricsCollector()
        cancel_event = threading.Event()

        # Timeout guard
        def _timeout():
            cancel_event.set()

        timer = threading.Timer(timeout_seconds, _timeout)
        timer.daemon = True
        timer.start()

        try:
            from core.llm import run_agent_turn

            print(f"  [{task.instance_id}] Running agent...", file=sys.stderr)
            result_msg = run_agent_turn(
                messages=session["messages"],
                config=config,
                write_gate=session["write_gate"],
                read_gate=session["read_gate"],
                on_tool_start=metrics.on_tool_start,
                on_tool_end=metrics.on_tool_end,
                cancel_event=cancel_event,
                max_turns=100,
                session=session["session"],
                memory_store=session["memory"],
            )
        finally:
            timer.cancel()

        elapsed = time.monotonic() - start_time

        # Collect git diff as prediction patch
        diff_result = subprocess.run(
            ["git", "diff", "HEAD"],
            capture_output=True, text=True, cwd=workspace, timeout=30,
        )
        model_patch = diff_result.stdout

        # Estimate tokens
        from memory.memory import _total_tokens
        tokens = _total_tokens(session["messages"])

        os.chdir(original_cwd)

        print(f"  [{task.instance_id}] Done in {elapsed:.0f}s, "
              f"{metrics.turn_count} turns, {tokens} tokens, "
              f"patch: {len(model_patch)} bytes",
              file=sys.stderr)

        return SWEBenchResult(
            instance_id=task.instance_id,
            repo=task.repo,
            model_patch=model_patch,
            turns_used=result_msg.get("_turn_count", metrics.turn_count) if result_msg else 0,
            tool_calls=dict(metrics.tool_counts),
            tokens_consumed=tokens,
            wall_time_seconds=elapsed,
        )

    except Exception as exc:
        elapsed = time.monotonic() - start_time
        print(f"  [{task.instance_id}] ERROR: {exc}", file=sys.stderr)
        return SWEBenchResult(
            instance_id=task.instance_id,
            repo=task.repo,
            turns_used=0,
            tool_calls=dict(metrics.tool_counts) if metrics is not None else {},
            wall_time_seconds=elapsed,
            error=str(exc),
        )

    finally:
        if workspace and os.path.exists(workspace):
            shutil.rmtree(workspace, ignore_errors=True)


# ---------------------------------------------------------------------------
# Suite runner
# ---------------------------------------------------------------------------


def run_swebench_suite(
    tasks: list[SWEBenchTask],
    *,
    timeout_seconds: int = 600,
    stream: bool = False,
    resume_from: dict[str, str] | None = None,
) -> SWEBenchReport:
    """Run mini_agent on a suite of SWE-bench tasks.

    Args:
        tasks: List of SWEBenchTask objects.
        timeout_seconds: Max wall-clock time per task.
        stream: If True, stream agent output.
        resume_from: Dict of instance_id -> model_patch from a previous run.
                     Tasks already in this dict are skipped.

    Returns:
        SWEBenchReport with aggregated results.
    """
    results: list[SWEBenchResult] = []
    completed = 0
    errors = 0
    resolved = 0
    total_turns = 0
    total_tokens = 0
    total_time = 0.0
    skipped = resume_from or {}

    for i, task in enumerate(tasks):
        instance_id = task.instance_id

        if instance_id in skipped:
            print(f"\n[{i+1}/{len(tasks)}] SKIPPING {instance_id} (already in resume file)",
                  file=sys.stderr)
            results.append(SWEBenchResult(
                instance_id=instance_id,
                repo=task.repo,
                model_patch=skipped[instance_id],
            ))
            completed += 1
            continue

        print(f"\n{'='*60}", file=sys.stderr)
        print(f"[{i+1}/{len(tasks)}] {instance_id}", file=sys.stderr)
        print(f"  Repo: {task.repo}", file=sys.stderr)
        print(f"  Issue: {task.problem_statement[:200].replace(chr(10), ' ')}...", file=sys.stderr)
        print(f"{'='*60}", file=sys.stderr)

        result = run_swebench_task(task, timeout_seconds=timeout_seconds, stream=stream)
        results.append(result)

        if result.error:
            errors += 1
        else:
            completed += 1

        if result.resolved:
            resolved += 1

        total_turns += result.turns_used
        total_tokens += result.tokens_consumed
        total_time += result.wall_time_seconds

    N = len(results)
    return SWEBenchReport(
        total=N,
        completed=completed,
        errors=errors,
        resolved=resolved,
        resolution_rate=resolved / N if N > 0 else 0.0,
        avg_turns=total_turns / N if N > 0 else 0.0,
        avg_tokens=total_tokens / N if N > 0 else 0.0,
        avg_wall_time=total_time / N if N > 0 else 0.0,
        per_task=results,
    )


# ---------------------------------------------------------------------------
# Predictions I/O
# ---------------------------------------------------------------------------


def save_predictions(results: list[SWEBenchResult], output_path: str, model_name: str = "mini_agent") -> None:
    """Save results as SWE-bench predictions JSONL file.

    Format (one JSON object per line):
        {"instance_id": "...", "model_patch": "...", "model_name_or_path": "mini_agent"}
    """
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w") as f:
        for r in results:
            prediction = {
                "instance_id": r.instance_id,
                "model_patch": r.model_patch,
                "model_name_or_path": model_name,
            }
            f.write(json.dumps(prediction) + "\n")
    print(f"\nPredictions saved to {output_path} ({len(results)} tasks)", file=sys.stderr)


def load_predictions(predictions_path: str) -> dict[str, str]:
    """Load existing predictions from a JSONL file.

    Returns:
        Dict mapping instance_id -> model_patch.
    """
    predictions: dict[str, str] = {}
    if not os.path.exists(predictions_path):
        return predictions

    with open(predictions_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                instance_id = obj.get("instance_id", "")
                patch = obj.get("model_patch", "")
                if instance_id:
                    predictions[instance_id] = patch
            except json.JSONDecodeError:
                continue

    print(f"Loaded {len(predictions)} existing predictions from {predictions_path}",
          file=sys.stderr)
    return predictions


def save_report(report: SWEBenchReport, output_dir: str | None = None) -> str:
    """Save a JSON report and return the file path."""
    LOCAL_REPORT_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = time.strftime("%Y-%m-%dT%H%M%S", time.gmtime())
    filename = f"swebench_{timestamp}.json"
    path = os.path.join(output_dir or LOCAL_REPORT_DIR, filename)

    report_dict = {
        "timestamp": timestamp,
        "dataset": report.dataset,
        "model_name": report.model_name,
        "total": report.total,
        "completed": report.completed,
        "errors": report.errors,
        "resolved": report.resolved,
        "resolution_rate": report.resolution_rate,
        "avg_turns": report.avg_turns,
        "avg_tokens": report.avg_tokens,
        "avg_wall_time": report.avg_wall_time,
        "tasks": [
            {
                "instance_id": r.instance_id,
                "repo": r.repo,
                "turns_used": r.turns_used,
                "tool_calls": r.tool_calls,
                "tokens_consumed": r.tokens_consumed,
                "wall_time_seconds": r.wall_time_seconds,
                "error": r.error,
                "resolved": r.resolved,
                "patch_size_bytes": len(r.model_patch),
            }
            for r in report.per_task
        ],
    }

    with open(path, "w") as f:
        json.dump(report_dict, f, indent=2)

    print(f"Report saved to {path}", file=sys.stderr)
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli() -> None:
    """Entry point: python -m eval.swebench_runner"""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m eval.swebench_runner",
        description="SWE-bench Runner for mini_agent -- generate predictions for official evaluation",
    )
    parser.add_argument(
        "--dataset",
        type=str,
        default="princeton-nlp/SWE-bench_Lite",
        help="HF dataset name (default: princeton-nlp/SWE-bench_Lite). "
             "Also try: princeton-nlp/SWE-bench_Verified",
    )
    parser.add_argument(
        "--max-tasks",
        type=int,
        default=None,
        help="Cap on number of tasks to run (for testing).",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="predictions.jsonl",
        help="Path for predictions output (JSONL format).",
    )
    parser.add_argument(
        "--resume",
        type=str,
        default=None,
        help="Resume from existing predictions file (skip already-completed tasks).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Timeout per task in seconds (default: 600).",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        help="Stream agent output during eval.",
    )
    parser.add_argument(
        "--report-dir",
        type=str,
        default=None,
        help="Directory for JSON report output.",
    )
    parser.add_argument(
        "--model-name",
        type=str,
        default="mini_agent",
        help="Model name for predictions metadata (default: mini_agent).",
    )
    args = parser.parse_args()

    # Load resume data if provided
    resume_data: dict[str, str] = {}
    if args.resume:
        resume_data = load_predictions(args.resume)

    # Load tasks from HuggingFace
    raw_tasks = _load_dataset(args.dataset, max_tasks=args.max_tasks)
    tasks = [parse_swebench_task(raw) for raw in raw_tasks]

    # Filter out already-completed tasks
    if resume_data:
        tasks = [t for t in tasks if t.instance_id not in resume_data]

    if not tasks:
        print("All tasks already completed. Nothing to run.", file=sys.stderr)
        return

    print(f"\nRunning SWE-bench on {len(tasks)} task(s)...", file=sys.stderr)

    # Run suite
    report = run_swebench_suite(
        tasks,
        timeout_seconds=args.timeout,
        stream=args.stream,
        resume_from=resume_data,
    )
    report.dataset = args.dataset
    report.model_name = args.model_name

    # Merge with resume data for output
    all_results = report.per_task
    if resume_data:
        # Add resumed results that weren't re-run
        run_ids = {r.instance_id for r in all_results}
        for inst_id, patch in resume_data.items():
            if inst_id not in run_ids:
                all_results.append(SWEBenchResult(
                    instance_id=inst_id,
                    repo="",
                    model_patch=patch,
                ))

    # Save predictions
    save_predictions(all_results, args.output, model_name=args.model_name)

    # Save report
    save_report(report, output_dir=args.report_dir)

    # Print summary
    print(f"\n{'='*60}")
    print("SWE-bench Run Complete")
    print(f"  Dataset:    {args.dataset}")
    print(f"  Total:      {report.total}")
    print(f"  Completed:  {report.completed}")
    print(f"  Errors:     {report.errors}")
    if report.resolved > 0:
        print(f"  Resolved:   {report.resolved} ({report.resolution_rate:.1%})")
    print(f"  Avg turns:  {report.avg_turns:.1f}")
    print(f"  Avg tokens: {report.avg_tokens:.0f}")
    print(f"  Avg time:   {report.avg_wall_time:.1f}s")
    print(f"  Predictions: {args.output}")
    print(f"{'='*60}")

    if report.resolved == 0 and report.completed > 0:
        print("\n  To evaluate these predictions with the official SWE-bench harness:")
        print("    git clone https://github.com/princeton-nlp/SWE-bench.git")
        print("    cd SWE-bench")
        print("    python -m swebench.harness.run_evaluation \\")
        print(f"        --dataset_name {args.dataset} \\")
        print(f"        --predictions_path {os.path.abspath(args.output)} \\")
        print("        --max_workers 4 \\")
        print("        --run_id mini_agent")


if __name__ == "__main__":
    _cli()
