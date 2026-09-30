"""Run the EdgeMind test suite: everything, one feature, or a few, with a per-feature summary at the end.

    python tests/run_tests.py                      # every feature (starts an isolated stack, ~10-15 min)
    python tests/run_tests.py --list               # list the features
    python tests/run_tests.py offline              # one feature
    python tests/run_tests.py sync conflicts       # several features
    python tests/run_tests.py --fast               # component tests only: no servers, ~1 min
    python tests/run_tests.py conflicts -k merge   # anything after the features is passed to pytest
    python tests/run_tests.py --keep ...           # keep the stack's temp folder (logs, shards) afterwards

Plain pytest works too:  python -m pytest tests/test_05_offline.py -v
"""
import argparse
import ast
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def features():
    """{name: (file, one-line description)} from tests/test_NN_<name>.py and each file's docstring."""
    out = {}
    for path in sorted(HERE.glob("test_[0-9][0-9]_*.py")):
        name = path.stem.split("_", 2)[2]
        doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""
        first = " ".join(doc.split("\n\n")[0].split())
        out[name] = (path, first if len(first) <= 110 else first[:107] + "...")
    return out


class Summary:
    """pytest plugin: collect outcomes per feature file."""

    def __init__(self):
        self.by_file = {}
        self.failures = []

    def pytest_runtest_logreport(self, report):
        f = report.nodeid.split("::", 1)[0].replace("\\", "/").rsplit("/", 1)[-1]
        row = self.by_file.setdefault(f, {"passed": 0, "failed": 0, "skipped": 0, "error": 0, "secs": 0.0})
        row["secs"] += report.duration
        if report.when == "call":
            key = report.outcome  # passed / failed / skipped
        elif report.failed:
            key = "error"  # setup/teardown failed (e.g. the stack did not start)
        elif report.skipped:
            key = "skipped"
        else:
            return
        row[key] += 1
        if key in ("failed", "error"):
            crash = getattr(report.longrepr, "reprcrash", None)
            msg = crash.message if crash else str(report.longrepr).strip().splitlines()[-1]
            self.failures.append((report.nodeid, key, msg.splitlines()[0][:200]))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    feats = features()
    ap = argparse.ArgumentParser(description="Run EdgeMind tests by feature.", add_help=True)
    ap.add_argument("names", nargs="*", help=f"features to run (default: all): {', '.join(feats)}")
    ap.add_argument("--list", action="store_true", help="list the features and exit")
    ap.add_argument("--fast", action="store_true", help="component tests only (no Qdrant Server / processes)")
    ap.add_argument("--system", action="store_true", help="system tests only (against the real stack)")
    ap.add_argument("--keep", action="store_true", help="keep the stack's temp folder afterwards")
    ap.add_argument("-q", "--quiet", action="store_true", help="less pytest output")
    args, passthrough = ap.parse_known_args()

    if args.list:
        print("Features (python tests/run_tests.py <feature> ...):\n")
        for name, (path, desc) in feats.items():
            print(f"  {name:28} {desc}")
        return 0
    unknown = [n for n in args.names if n not in feats]
    if unknown:
        print(f"Unknown feature(s): {', '.join(unknown)}. Use --list to see them.")
        return 2
    try:
        import pytest
    except ImportError:
        print("pytest is not installed. Run:  python -m pip install -r requirements-dev.txt")
        return 2

    chosen = args.names or list(feats)
    files = [str(feats[n][0]) for n in chosen]
    pargs = files + ["-p", "no:cacheprovider", "-q" if args.quiet else "-v", "--rootdir", str(ROOT),
                     "-c", str(ROOT / "pytest.ini")]
    if args.fast:
        pargs += ["-m", "not system"]
    elif args.system:
        pargs += ["-m", "system"]
    if args.keep:
        os.environ["EDGEMIND_TEST_KEEP"] = "1"
    pargs += passthrough

    summary = Summary()
    t0 = time.time()
    code = pytest.main(pargs, plugins=[summary])
    took = time.time() - t0

    print("\n" + "=" * 78)
    print(f"EdgeMind test summary ({took / 60:.1f} min)")
    print("=" * 78)
    print(f"  {'feature':28} {'passed':>6} {'failed':>6} {'errors':>6} {'skipped':>7}   result")
    for name in chosen:
        f = feats[name][0].name
        row = summary.by_file.get(f)
        if not row:
            print(f"  {name:28} {'-':>6} {'-':>6} {'-':>6} {'-':>7}   " + ("system tests only" if args.fast else "not run"))
            continue
        bad = row["failed"] + row["error"]
        result = "FAIL" if bad else ("PASS" if row["passed"] else "SKIPPED")
        print(f"  {name:28} {row['passed']:>6} {row['failed']:>6} {row['error']:>6} {row['skipped']:>7}   {result}")
    if summary.failures:
        print("\nWhat went wrong (details for each one are in the FAILURES section above):")
        for nodeid, kind, msg in summary.failures:
            print(f"  - {nodeid}\n      {kind}: {msg}")
    print("=" * 78)
    return int(code)


if __name__ == "__main__":
    sys.exit(main())
