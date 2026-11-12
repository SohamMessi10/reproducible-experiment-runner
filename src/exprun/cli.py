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


COMMANDS = {"check": check_command, "run": run_command, "list": list_command, "show": show_command,
            "compare": compare_command, "rerun": rerun_command}


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return COMMANDS[args.command](args)


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
