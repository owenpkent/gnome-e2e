#!/usr/bin/env python3
"""Run the gnome-e2e suites against the VM.

  ./run-e2e.py                     use an already running VM (vm/start.sh)
  ./run-e2e.py --fresh             start a fresh overlay VM, run, stop it
  ./run-e2e.py --fresh --sysext    ... and install the toolbox sysext first
  ./run-e2e.py -k pie -v           select tests by substring, verbose
  ./run-e2e.py --suite smoke       smoke | clicklock | all (default: all)

ClickLock mode: --sysext, or --suite clicklock. Such a run must actually test
ClickLock: a guest without the click-lock-enabled key or a Settings without the
"Click Lock" row is a failure (no skip, no gsettings fallback), a skipped test
makes the exit code nonzero, and so does a selection without any ClickLock test.
--allow-skips turns that off (skips tolerated, gsettings fallback allowed). A
plain run (smoke on stock Fedora) keeps skipping the ClickLock tests.

Exit code: 0 all selected tests passed; 1 a failure or error, or (ClickLock
mode) a skip; 2 the guest was not reachable or the selection is unusable.

Artifacts: var/artifacts/<timestamp>/<test id>/ with screenshots (PNG), the
guest journal slice (full and gnome-shell/gdm filtered), the probe log and
result.txt; summary.json at the top.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from e2e import testcase  # noqa: E402


class Result(unittest.TextTestResult):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.outcomes = []
        self._start = {}

    def startTest(self, test):
        self._start[test] = time.monotonic()
        super().startTest(test)

    def _rec(self, test, status, detail=""):
        self.outcomes.append({"test": test.id(), "status": status,
                              "seconds": round(time.monotonic() - self._start.get(test, 0), 1),
                              "detail": detail})

    def addSuccess(self, test):
        super().addSuccess(test); self._rec(test, "pass")

    def addFailure(self, test, err):
        super().addFailure(test, err); self._rec(test, "fail", self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err); self._rec(test, "error", self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason); self._rec(test, "skip", reason)


def sh(*argv, check=True):
    return subprocess.run(argv, check=check)


def select(suite, pattern):
    loader = unittest.TestLoader()
    names = {"smoke": ["tests.test_smoke"], "clicklock": ["tests.test_clicklock"],
             "all": ["tests.test_smoke", "tests.test_clicklock"]}[suite]
    tests = unittest.TestSuite()
    for name in names:
        tests.addTests(loader.loadTestsFromName(name))

    def flatten(s):
        for t in s:
            if isinstance(t, unittest.TestSuite):
                yield from flatten(t)
            else:
                yield t

    picked = [t for t in flatten(tests) if not pattern or pattern in t.id()]
    return unittest.TestSuite(picked)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fresh", action="store_true", help="vm/start.sh before, vm/stop.sh after")
    ap.add_argument("--keep-vm", action="store_true", help="with --fresh: leave the VM running")
    ap.add_argument("--sysext", nargs="?", const="toolbox", metavar="DIR",
                    help="run vm/install-sysext.sh first (DIR: --from-dir; default: from the toolbox)")
    ap.add_argument("--suite", choices=["smoke", "clicklock", "all"], default="all")
    ap.add_argument("--allow-skips", action="store_true",
                    help="ClickLock mode (--sysext / --suite clicklock): tolerate skipped tests "
                         "and the gsettings fallback (default: they fail the run)")
    ap.add_argument("-k", dest="pattern", default="", help="substring of the test id")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--artifacts", help="artifact directory (default var/artifacts/<timestamp>)")
    args = ap.parse_args()

    suite = select(args.suite, args.pattern)
    if args.list:
        for t in suite:
            print(t.id())
        return 0

    clicklock_mode = args.sysext is not None or args.suite == "clicklock"
    require_clicklock = clicklock_mode and not args.allow_skips
    if require_clicklock and not any("test_clicklock" in t.id() for t in suite):
        print("ClickLock mode (--sysext / --suite clicklock) but no ClickLock test is selected "
              "(--suite/-k); add --allow-skips to run anyway", file=sys.stderr)
        return 2

    ctx = testcase.Context(args.artifacts, require_clicklock=require_clicklock)
    testcase.set_context(ctx)
    print(f"artifacts: {ctx.root}")
    started = False
    rc = 1
    try:
        if args.fresh:
            sh(os.path.join(ROOT, "vm/stop.sh"), check=False)
            sh(os.path.join(ROOT, "vm/start.sh"))
            started = True
        if args.sysext:
            cmd = [os.path.join(ROOT, "vm/install-sysext.sh")]
            if args.sysext != "toolbox":
                cmd += ["--from-dir", args.sysext]
            sh(*cmd)
        if not ctx.guest.up():
            print("guest not reachable on ssh (vm/start.sh, or use --fresh)", file=sys.stderr)
            return 2
        ctx.guest.push_package()
        info = ctx.guest.run(
            "bash", "-c",
            "uname -r; . /etc/os-release; echo $PRETTY_NAME; "
            "rpm -q mutter gnome-shell gnome-control-center gsettings-desktop-schemas libinput; "
            "systemd-sysext status --no-pager 2>&1 | head -n 20; "
            "gsettings range org.gnome.desktop.a11y.mouse click-lock-enabled 2>&1 | head -n 1",
            session=False, check=False).stdout
        with open(os.path.join(ctx.root, "guest-info.txt"), "w") as f:
            f.write(info)
        runner = unittest.TextTestRunner(verbosity=2 if args.verbose else 1,
                                         resultclass=Result, stream=sys.stdout)
        t0 = time.time()
        result = runner.run(suite)
        rc = 0 if result.wasSuccessful() else 1
        if require_clicklock and result.skipped:
            print(f"ClickLock mode: {len(result.skipped)} skipped test(s) count as failure "
                  "(--allow-skips to tolerate)", file=sys.stderr)
            rc = 1
        summary = {"started": time.strftime("%FT%T", time.localtime(t0)),
                   "seconds": round(time.time() - t0, 1),
                   "clicklock_mode": clicklock_mode,
                   "allow_skips": args.allow_skips,
                   "exit_code": rc,
                   "counts": {"run": result.testsRun, "failures": len(result.failures),
                              "errors": len(result.errors), "skipped": len(result.skipped)},
                   "skips": [{"test": o["test"], "reason": o["detail"]}
                             for o in result.outcomes if o["status"] == "skip"],
                   "tests": result.outcomes}
        with open(os.path.join(ctx.root, "summary.json"), "w") as f:
            json.dump(summary, f, indent=2)
    finally:
        ctx.close()
        if started and not args.keep_vm:
            sh(os.path.join(ROOT, "vm/stop.sh"), check=False)
    return rc


if __name__ == "__main__":
    sys.exit(main())
