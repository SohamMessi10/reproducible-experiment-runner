"""The `exprun` command."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from exprun import __version__

EXIT_OK = 0
EXIT_FAILED = 1        # a run failed or was interrupted, or a file couldn't be read or written
EXIT_USAGE = 2         # bad usage or an invalid config: refused before anything runs
EXIT_NOT_REPRODUCED = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="exprun", description="Run experiments from a config file and record "
                                     "what is needed to reproduce and compare them.")
    parser.add_argument("--version", action="version", version=f"exprun {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    check = commands.add_parser("check", help="validate a config and show what would identify its run")
    check.add_argument("config", type=Path, help="an experiment config (TOML)")
    check.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE",
                       help="override a param, such as model.C=10 (a TOML value; quote strings); repeatable")
    check.add_argument("--json", action="store_true", help="print the result as JSON")

    run = commands.add_parser("run", help="run an experiment and record it")
    run.add_argument("config", type=Path, help="an experiment config (TOML)")
    run.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE",
                     help="override a param, such as model.C=10 (a TOML value; quote strings); repeatable")
    run.add_argument("--runs", type=Path, default=Path("runs"), help="the runs folder (default: ./runs)")
    run.add_argument("--timeout", type=float, metavar="SECONDS", help="stop the task after this long")
    run.add_argument("--quiet", action="store_true", help="don't echo the task's output (it is still logged)")

    sweep = commands.add_parser("sweep", help="run an experiment once for each combination of param values")
    sweep.add_argument("config", type=Path, help="an experiment config (TOML)")
    sweep.add_argument("--grid", action="append", default=[], metavar="KEY=V1,V2,...", required=True,
                       help="values to try for a param, such as model.C=0.1,1,10 (TOML values; quote strings), or "
                            "seed=1,2,3; repeatable: every combination runs")
    sweep.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE",
                       help="override a param for every run; repeatable")
    sweep.add_argument("--workers", type=int, default=1, metavar="N", help="run up to N at a time (default 1)")
    sweep.add_argument("--runs", type=Path, default=Path("runs"), help="the runs folder (default: ./runs)")
    sweep.add_argument("--timeout", type=float, metavar="SECONDS", help="stop each run after this long")
    sweep.add_argument("--sort", metavar="METRIC", help="order the results by this metric (ascending)")
    sweep.add_argument("--descending", action="store_true", help="with --sort: largest first")
    sweep.add_argument("--quiet", action="store_true",
                       help="don't echo the tasks' output (it is still logged; with --workers > 1 it never is)")

    results = commands.add_parser("sweep-results", help="show the results of a sweep as a table")
    results.add_argument("sweep", nargs="?", default="latest", help="a sweep id, a unique start of one, or 'latest' "
                         "(the default)")
    results.add_argument("--runs", type=Path, default=Path("runs"), help="the runs folder (default: ./runs)")
    results.add_argument("--sort", metavar="METRIC", help="order the results by this metric (ascending)")
    results.add_argument("--descending", action="store_true", help="with --sort: largest first")
    results.add_argument("--json", action="store_true", help="print the results as JSON")

    listing = commands.add_parser("list", help="list runs, newest first")
    listing.add_argument("--runs", type=Path, default=Path("runs"), help="the runs folder (default: ./runs)")
    listing.add_argument("--experiment", metavar="NAME", help="only runs of this experiment")
    listing.add_argument("--status", choices=["running", "succeeded", "failed", "interrupted"],
                         help="only runs with this status")
    listing.add_argument("--limit", type=int, default=20, metavar="N", help="show at most N runs (0: all; default 20)")
    listing.add_argument("--json", action="store_true", help="print the runs as JSON")

    show = commands.add_parser("show", help="show one run in full")
    show.add_argument("run", help="a run id, a unique start of one, or 'latest'")
    show.add_argument("--runs", type=Path, default=Path("runs"), help="the runs folder (default: ./runs)")
    show.add_argument("--json", action="store_true", help="print the manifest")

    rerun = commands.add_parser("rerun", help="run a past run again from its manifest and check it reproduces")
    rerun.add_argument("run", help="a run id, a unique start of one, or 'latest'")
    rerun.add_argument("--runs", type=Path, default=Path("runs"), help="the runs folder (default: ./runs)")
    rerun.add_argument("--tolerance", type=float, default=0.0, metavar="REL",
                       help="accept numeric metrics within this relative difference, such as 1e-9 (judges metrics "
                            "only); default: exact")
    rerun.add_argument("--allow-code-change", action="store_true",
                       help="run even if the task's code changed, to test whether the current code reproduces it")
    rerun.add_argument("--timeout", type=float, metavar="SECONDS", help="stop the task after this long")
    rerun.add_argument("--quiet", action="store_true", help="don't echo the task's output (it is still logged)")

    demo = commands.add_parser("demo", help="write a ready-to-run demo (handwritten digits, scikit-learn)")
    demo.add_argument("folder", type=Path, help="where to write it (created if needed; nothing is overwritten)")

    compare = commands.add_parser("compare", help="show what differs between runs")
    compare.add_argument("runs_to_compare", nargs="+", metavar="RUN", help="two or more runs (ids, unique starts, 'latest')")
    compare.add_argument("--runs", type=Path, default=Path("runs"), help="the runs folder (default: ./runs)")
    compare.add_argument("--all", action="store_true", help="also show values that are the same")
    compare.add_argument("--json", action="store_true", help="print the comparison as JSON")
    return parser


def _short(sha: str | None) -> str:
    return "–" if sha is None else sha.removeprefix("sha256:")[:12]


def check_command(args) -> int:
    from exprun.codeversion import TaskError
    from exprun.config import ConfigError, load
    from exprun.hashing import InputError
    from exprun.identity import identify

    try:
        config = load(args.config, args.overrides)
        identity = identify(config)
    except (ConfigError, TaskError, InputError) as exc:
        print(f"exprun: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except OSError as exc:
        print(f"exprun: {exc}", file=sys.stderr)
        return EXIT_FAILED
    if args.json:
        print(json.dumps({"config": str(config.path), "name": config.name, "description": config.description,
                          "seed": config.seed, **identity.to_dict()}, indent=1, ensure_ascii=False))
        return EXIT_OK
    code = identity.code
    git = code.get("git")
    where = Path(code["file"]).name
    if "distribution" in code:
        where += f", {code['distribution']} {code['version']}"
    if git is not None:
        where += f", git {git['commit'][:7] if git['commit'] else 'with no commits'}"
        if git["dirty"]:
            where += " + uncommitted changes"
    print(f"config:      {config.path}")
    print(f"experiment:  {config.name}" + (f" ({config.description})" if config.description else ""))
    print(f"task:        {config.task}  ({where})")
    print(f"seed:        {config.seed if config.seed is not None else 'not set: one is drawn and recorded at run time'}")
    print(f"params:      {json.dumps(config.params, ensure_ascii=False)}")
    if identity.inputs:
        print("inputs:")
        width = max(len(n) for n in identity.inputs)
        for name, record in identity.inputs.items():
            files = f", {record['files']:,} files" if record["kind"] == "folder" else ""
            print(f"  {name:<{width}}  {record['path']}  ({record['bytes']:,} bytes{files})  {_short(record['sha256'])}")
    else:
        print("inputs:      none")
    if identity.fingerprint is None:
        print("fingerprint: computed at run time, once the seed is drawn")
    else:
        print(f"config hash: {_short(identity.config_hash)}")
        print(f"fingerprint: {_short(identity.fingerprint)}")
    return EXIT_OK


def _count(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _metric(value) -> str:
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def run_command(args) -> int:
    from exprun.codeversion import TaskError
    from exprun.config import ConfigError, load
    from exprun.hashing import InputError
    from exprun.runner import execute
    from exprun.store import RunStore

    if args.timeout is not None and not args.timeout > 0:
        print("exprun: --timeout must be a positive number of seconds", file=sys.stderr)
        return EXIT_USAGE
    try:
        config = load(args.config, args.overrides)
    except ConfigError as exc:
        print(f"exprun: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except OSError as exc:
        print(f"exprun: {exc}", file=sys.stderr)
        return EXIT_FAILED

    def started(folder, manifest) -> None:
        seed = manifest["spec"]["seed"]
        print(f"run {folder.id}  {config.name}  (seed {seed}{', drawn' if manifest['seed_source'] == 'drawn' else ''})",
              file=sys.stderr, flush=True)

    try:
        folder, manifest = execute(config, RunStore(args.runs), overrides=args.overrides, timeout=args.timeout,
                                   quiet=args.quiet, on_start=started)
    except (ConfigError, TaskError, InputError) as exc:
        print(f"exprun: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except OSError as exc:
        print(f"exprun: {exc}", file=sys.stderr)
        return EXIT_FAILED
    status, duration = manifest["status"], manifest["duration_s"]
    print(f"{status} in {duration:.2f} s", file=sys.stderr)
    if manifest["metrics"]:
        print("metrics: " + "  ".join(f"{k}={_metric(v)}" for k, v in manifest["metrics"].items()))
    if manifest["error"]:
        print(f"error: {manifest['error']['type']}: {manifest['error']['message']}", file=sys.stderr)
    outputs = manifest["outputs"]
    print(f"run folder: {folder.path}" + (f"  ({len(outputs)} output file{'s' if len(outputs) != 1 else ''})"
                                          if outputs else ""), file=sys.stderr)
    return EXIT_OK if status == "succeeded" else EXIT_FAILED


def sweep_command(args) -> int:
    from exprun import sweep as sweeps
    from exprun.codeversion import TaskError
    from exprun.config import ConfigError
    from exprun.hashing import InputError
    from exprun.report import sweep_table, table
    from exprun.store import RunStore

    if args.timeout is not None and not args.timeout > 0:
        print("exprun: --timeout must be a positive number of seconds", file=sys.stderr)
        return EXIT_USAGE
    if args.workers < 1:
        print("exprun: --workers must be 1 or more", file=sys.stderr)
        return EXIT_USAGE
    try:
        grid = sweeps.parse_grid(args.grid)
        points, seed_source = sweeps.plan(args.config, args.overrides, grid)
    except (sweeps.SweepError, ConfigError, TaskError, InputError) as exc:
        print(f"exprun: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except OSError as exc:
        print(f"exprun: {exc}", file=sys.stderr)
        return EXIT_FAILED
    if args.sort is not None and args.sort in {key for key, _ in grid}:
        print(f"exprun: --sort takes a metric; {args.sort} is a swept param", file=sys.stderr)
        return EXIT_USAGE
    workers = min(args.workers, len(points))
    sweep = sweeps.Sweep(sweeps.new_sweep_id(), points, seed_source, grid)
    seeds = {"sweep": "swept", "config": f"seed {points[0].seed} from the config",
             "drawn": f"seed {points[0].seed}, drawn for every run"}[seed_source]
    print(f"sweep {sweep.id}: {_count(len(points), 'run')} ({' x '.join(str(len(v)) for _, v in grid)}), "
          f"{workers} at a time, {seeds}", file=sys.stderr, flush=True)
    width = len(str(len(points)))
    finished = 0

    def started(point, folder, manifest) -> None:
        if workers == 1:
            print(f"[{point.index + 1:>{width}}/{len(points)}] run {folder.id}  {sweeps.describe(point.values)}",
                  file=sys.stderr, flush=True)

    def done(outcome) -> None:
        nonlocal finished
        finished += 1
        point, manifest = outcome.point, outcome.manifest
        if manifest is None:
            summary = f"could not run: {outcome.error}"
        else:
            metrics = "  ".join(f"{k}={_metric(v)}" for k, v in list((manifest["metrics"] or {}).items())[:3])
            error = manifest["error"]
            summary = f"{manifest['id']}  {manifest['status']} in {manifest['duration_s']:.2f} s" + (
                f"  {metrics}" if metrics else f"  {error['type']}: {error['message']}" if error else "")
        print(f"({finished:>{width}}/{len(points)} done) {sweeps.describe(point.values)}  {summary}",
              file=sys.stderr, flush=True)

    try:
        sweeps.run(sweep, RunStore(args.runs), workers=workers, timeout=args.timeout,
                   quiet=args.quiet or workers > 1, on_start=started, on_done=done)
    except OSError as exc:
        print(f"exprun: {exc}", file=sys.stderr)
        return EXIT_FAILED
    by_index = {outcome.point.index: outcome for outcome in sweep.outcomes}
    rows = []
    for point in points:
        outcome = by_index.get(point.index)
        manifest = outcome.manifest if outcome else None
        status = manifest["status"] if manifest else ("not started" if outcome is None else "could not run")
        rows.append((manifest, status, point.values))
    header, lines = sweep_table([key for key, _ in grid], rows, args.sort, args.descending)
    print()
    for line in table(lines, header=header):
        print(line)
    failures = [o for o in sweep.outcomes if o.manifest is None or o.manifest["status"] != "succeeded"]
    unstarted = sweep.not_started
    if failures or unstarted:
        parts = [f"{_count(len(failures), 'run')} did not succeed"] if failures else []
        if unstarted:
            parts.append(f"{len(unstarted)} not started")
        print(f"sweep {sweep.id}: " + ", ".join(parts) + (" (stopped)" if sweep.stopped else ""), file=sys.stderr)
        return EXIT_FAILED
    print(f"sweep {sweep.id}: all {_count(len(points), 'run')} succeeded", file=sys.stderr)
    return EXIT_OK


def sweep_results_command(args) -> int:
    from exprun import sweep as sweeps
    from exprun.report import sweep_table, table
    from exprun.store import RunStore

    store = RunStore(args.runs)
    try:
        sweep_id = sweeps.resolve(store, args.sweep)
    except LookupError as exc:
        print(f"exprun: {exc.args[0]}", file=sys.stderr)
        return EXIT_USAGE
    found = sweeps.members(store, sweep_id)
    first = found[0][1]["sweep"]
    keys = list(first["grid"])
    if args.sort is not None and args.sort in keys:
        print(f"exprun: --sort takes a metric; {args.sort} is a swept param", file=sys.stderr)
        return EXIT_USAGE
    rows = [(manifest, status, manifest["sweep"]["point"]) for _, manifest, status in found]
    if args.json:
        print(json.dumps({"sweep": sweep_id, "grid": first["grid"], "size": first["size"],
                          "runs": [{"id": m["id"], "point": m["sweep"]["point"], "status": status,
                                    "duration_s": m["duration_s"], "metrics": m["metrics"]}
                                   for m, status, _ in rows]}, indent=1, ensure_ascii=False))
        return EXIT_OK
    header, lines = sweep_table(keys, rows, args.sort, args.descending)
    print(f"sweep {sweep_id}: {len(found)} of {_count(first['size'], 'run')} recorded")
    for line in table(lines, header=header):
        print(line)
    return EXIT_OK


def list_command(args) -> int:
    from exprun.report import LIST_HEADER, list_row, table
    from exprun.store import ManifestError, RunStore

    if args.limit < 0:
        print("exprun: --limit must be 0 (all) or more", file=sys.stderr)
        return EXIT_USAGE
    store = RunStore(args.runs)
    rows, records = [], []
    for folder in reversed(store.all()):
        try:
            manifest = folder.reconcile()       # records runs whose runner died as interrupted
            status = folder.effective_status(manifest)
        except ManifestError:
            manifest, status = None, "unreadable"
        if args.experiment and (manifest is None or manifest["experiment"]["name"] != args.experiment):
            continue
        if args.status and status != args.status:
            continue
        rows.append(list_row(folder.id, manifest, status))
        records.append({"id": folder.id, "status": status, "experiment": manifest and manifest["experiment"]["name"],
                        "started_at": manifest and manifest.get("started_at"),
                        "duration_s": manifest and manifest.get("duration_s"),
                        "metrics": manifest and manifest.get("metrics"), "fingerprint": manifest and manifest["fingerprint"]})
    total = len(rows)
    if args.limit:
        rows, records = rows[: args.limit], records[: args.limit]
    if args.json:
        print(json.dumps(records, indent=1, ensure_ascii=False))
        return EXIT_OK
    if not rows:
        filtered = " matching those filters" if args.experiment or args.status else ""
        print(f"no runs{filtered} in {store.root}" + ("" if filtered else ": start one with `exprun run CONFIG`"))
        return EXIT_OK
    for line in table(rows, header=LIST_HEADER):
        print(line)
    if total > len(rows):
        print(f"(showing the newest {len(rows)} of {total}; --limit 0 shows all)")
    return EXIT_OK


def _load_runs(store, references):
    """(folder, manifest, status) for each reference, or an error message and exit code."""
    from exprun.store import ManifestError, RunNotFound

    found = []
    for reference in references:
        try:
            folder = store.resolve(reference)
        except RunNotFound as exc:
            return None, (str(exc), EXIT_USAGE)
        try:
            manifest = folder.reconcile()
        except ManifestError as exc:
            return None, (str(exc), EXIT_FAILED)
        if manifest is None:
            return None, (f"run {folder.id} has no manifest yet ({folder.effective_status()})", EXIT_FAILED)
        found.append((folder, manifest, folder.effective_status(manifest)))
    return found, None


def show_command(args) -> int:
    from exprun.report import show_lines
    from exprun.store import RunStore

    found, error = _load_runs(RunStore(args.runs), [args.run])
    if error:
        print(f"exprun: {error[0]}", file=sys.stderr)
        return error[1]
    folder, manifest, status = found[0]
    if args.json:
        print(json.dumps(manifest, indent=1, ensure_ascii=False))
        return EXIT_OK
    for line in show_lines(manifest, status, str(folder.path)):
        print(line)
    return EXIT_OK


def compare_command(args) -> int:
    from exprun.report import ABSENT, compare, compare_lines
    from exprun.store import RunStore

    if len(args.runs_to_compare) < 2:
        print("exprun: compare needs at least two runs", file=sys.stderr)
        return EXIT_USAGE
    found, error = _load_runs(RunStore(args.runs), args.runs_to_compare)
    if error:
        print(f"exprun: {error[0]}", file=sys.stderr)
        return error[1]
    if len({folder.id for folder, _, _ in found}) < len(found):
        print("exprun: the same run is named twice", file=sys.stderr)
        return EXIT_USAGE
    manifests = [manifest for _, manifest, _ in found]
    if args.json:
        result = compare(manifests, args.all)

        def plain(value):
            return None if value is ABSENT else value

        for section in result["sections"].values():
            for row in section["rows"]:
                row["values"] = [plain(v) for v in row["values"]]
        for metric in result["metrics"]:
            metric["values"] = [plain(v) for v in metric["values"]]
        print(json.dumps({"runs": [m["id"] for m in manifests], **result}, indent=1, ensure_ascii=False))
        return EXIT_OK
    for line in compare_lines(manifests, [status for _, _, status in found], args.all):
        print(line)
    return EXIT_OK


def rerun_command(args) -> int:
    from exprun.report import compare, section_lines, show_metric
    from exprun.reproduce import CannotRerun, judge, prepare
    from exprun.runner import execute
    from exprun.store import RunStore

    if not (args.tolerance >= 0 and args.tolerance < 1):
        print("exprun: --tolerance must be a relative difference from 0 up to (not including) 1", file=sys.stderr)
        return EXIT_USAGE
    if args.timeout is not None and not args.timeout > 0:
        print("exprun: --timeout must be a positive number of seconds", file=sys.stderr)
        return EXIT_USAGE
    store = RunStore(args.runs)
    found, error = _load_runs(store, [args.run])
    if error:
        print(f"exprun: {error[0]}", file=sys.stderr)
        return error[1]
    original_folder, original, _ = found[0]
    try:
        config, changes = prepare(original, original_folder.path, args.allow_code_change)
    except CannotRerun as exc:
        print(f"exprun: can't rerun {original['id']}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    for change in changes:
        print(f"note: {change} (allowed by --allow-code-change)", file=sys.stderr)

    def started(folder, manifest) -> None:
        print(f"rerun of {original['id']} (seed {manifest['spec']['seed']}) as run {folder.id}", file=sys.stderr,
              flush=True)

    try:
        folder, manifest = execute(config, store, overrides=original["config"].get("overrides") or [],
                                   timeout=args.timeout, quiet=args.quiet, on_start=started, rerun_of=original)
    except OSError as exc:
        print(f"exprun: {exc}", file=sys.stderr)
        return EXIT_FAILED
    result = judge(original, manifest, args.tolerance)
    result["of"] = original["id"]
    result["code_changes"] = changes
    manifest["reproduction"] = result
    folder.write(manifest)
    print(f"{manifest['status']} in {manifest['duration_s']:.2f} s", file=sys.stderr)
    outcome = result["outcome"]
    if result["reproduced"]:
        metrics, outputs = len(original.get("metrics") or {}), len(original.get("outputs") or {})
        detail = (f"{_count(metrics, 'metric')} equal, {_count(outputs, 'output file')} byte-identical"
                  if outcome == "identical" else
                  f"largest relative difference {result['max_relative_difference']:.3g} <= {args.tolerance:g}")
        print(f"reproduced: {outcome} ({detail})")
        return EXIT_OK
    print(f"NOT reproduced: {outcome}")
    if manifest["error"]:
        print(f"  the rerun failed: {manifest['error']['type']}: {manifest['error']['message']}")
    for entry in result["metric_differences"]:
        extra = f"  (relative difference {entry['relative_difference']:.3g})" if "relative_difference" in entry else ""
        print(f"  {entry['metric']}: {show_metric(entry['original'])} -> {show_metric(entry['rerun'])}{extra}")
    for path in result["output_differences"]:
        print(f"  output {path} differs")
    differences = section_lines(compare([original, manifest]), ["original", "rerun"])
    if differences:
        print("what else differs:")
        for line in differences[1:]:
            print("  " + line if line else "")
    else:
        print("config, data, code and environment are the same as recorded: the task itself isn't deterministic "
              "(an unseeded random source, the clock, thread scheduling ...)")
    return EXIT_NOT_REPRODUCED


def demo_command(args) -> int:
    from exprun.demo import export

    try:
        written = export(args.folder)
    except ModuleNotFoundError as exc:
        print(f"exprun: the demo needs scikit-learn and NumPy ({exc.name} is missing): "
              'pip install "reproducible-experiment-runner[demo]"', file=sys.stderr)
        return EXIT_USAGE
    except FileExistsError as exc:
        print(f"exprun: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except OSError as exc:
        print(f"exprun: can't write the demo ({exc})", file=sys.stderr)
        return EXIT_FAILED
    for path in written:
        print(f"wrote {path}")
    print(f"next: exprun run {args.folder / 'logistic.toml'}")
    return EXIT_OK


COMMANDS = {"check": check_command, "run": run_command, "sweep": sweep_command,
            "sweep-results": sweep_results_command, "list": list_command, "show": show_command,
            "compare": compare_command, "rerun": rerun_command, "demo": demo_command}


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return COMMANDS[args.command](args)


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
