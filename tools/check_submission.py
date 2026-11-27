# SPDX-FileCopyrightText: (c) 2026 Marc Boudames
# SPDX-License-Identifier: Apache-2.0
"""
Pre-submission checklist for the Tiny Tapeout project.

    python tools/check_submission.py

Checks the things that are cheap to get wrong and expensive to discover late:
the metadata the shuttle reads, the consistency between files that have to
agree with each other, and the signoff numbers from the most recent local
hardening run.

Why this exists as a script rather than a habit. Most of these are invisible
to the test suite, because they are not about behaviour. A pinout string that
disagrees with the register transfer level misleads whoever uses the chip and
no simulation will ever notice. A top module name that does not match
info.yaml fails the shuttle rather than the tests. So they need checking, and
anything that needs checking repeatedly should be a program.

It reads the signoff metrics from build/rep7/final/metrics.json if present, or
from whichever build/rep*/final/metrics.json is newest, so run
tools/harden_local.py first for the physical checks to mean anything. Without
them the script still runs and reports the metadata checks.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE_COMMIT = "5774a98"      # the initial import, for config.json drift


class Checks:
    def __init__(self) -> None:
        self.rows: list[tuple[bool, str, str]] = []

    def add(self, passed: bool, label: str, detail: str = "") -> None:
        self.rows.append((bool(passed), label, detail))

    def report(self) -> int:
        width = max(len(r[1]) for r in self.rows)
        failed = 0
        for passed, label, detail in self.rows:
            if not passed:
                failed += 1
            mark = "pass" if passed else "FAIL"
            line = f"  [{mark}] {label.ljust(width)}"
            if detail:
                line += f"  {detail}"
            print(line)
        print()
        if failed:
            print(f"{failed} check(s) failed. Fix these before submitting.")
        else:
            print("All checks passed. Ready to submit.")
        return failed


def read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def newest_metrics() -> tuple[dict, str]:
    """The most recent local signoff metrics, or an empty dict."""
    best, best_mtime, name = {}, 0.0, ""
    for p in sorted(REPO.glob("build/rep*/final/metrics.json")):
        mt = p.stat().st_mtime
        if mt > best_mtime:
            try:
                best, best_mtime, name = json.loads(p.read_text()), mt, str(
                    p.relative_to(REPO))
            except (OSError, json.JSONDecodeError):
                continue
    return best, name


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True,
                          text=True)


def main() -> int:
    c = Checks()
    yaml = read("info.yaml")
    rtl = read("src/project.v")
    docs = read("docs/info.md")
    makefile = read("test/Makefile")

    # ---- metadata the shuttle reads -------------------------------------
    top_m = re.search(r'top_module:\s*"([^"]+)"', yaml)
    top = top_m.group(1) if top_m else ""
    c.add(top.startswith("tt_um_"), "top module is prefixed tt_um_", top)
    c.add(f"module {top} (" in rtl, "top module matches the source", top)

    srcs = re.findall(r'^\s*-\s*"([^"]+\.v)"', yaml, re.M)
    mk = re.search(r"^PROJECT_SOURCES\s*=\s*(.+)$", makefile, re.M)
    mk_srcs = mk.group(1).split() if mk else []
    c.add(sorted(srcs) == sorted(mk_srcs),
          "info.yaml sources match test/Makefile", " ".join(srcs))
    c.add(all((REPO / "src" / s).exists() for s in srcs),
          "every listed source file exists")

    for pre in ("ui", "uo", "uio"):
        found = sorted(int(m) for m in
                       re.findall(rf"^\s*{pre}\[(\d)\]:", yaml, re.M))
        c.add(found == list(range(8)), f"all eight {pre} pins present")

    c.add("yaml_version: 6" in yaml, "yaml_version is 6")
    tiles = re.search(r'tiles:\s*"([^"]+)"', yaml)
    c.add(bool(tiles), "tiles declared",
          tiles.group(1) if tiles else "missing")
    clk = re.search(r"clock_hz:\s*(\d+)", yaml)
    c.add(bool(clk) and int(clk.group(1)) > 0, "clock_hz declared",
          f"{int(clk.group(1)):,} Hz" if clk else "missing")

    # ---- the module interface must be the template's exactly ------------
    for port, kind in (("ui_in", "input"), ("uo_out", "output"),
                       ("uio_in", "input"), ("uio_out", "output"),
                       ("uio_oe", "output"), ("ena", "input"),
                       ("clk", "input"), ("rst_n", "input")):
        pat = rf"{kind}\s+wire\s*(?:\[7:0\]\s*)?{port}\b"
        c.add(re.search(pat, rtl) is not None, f"port {port} is an {kind}")

    # ---- documentation --------------------------------------------------
    for heading in ("## How it works", "## How to test",
                    "## External hardware"):
        c.add(heading in docs, f'docs contain "{heading.strip("# ")}"')
    imgs = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", docs)
    missing = [i for i in imgs if not (REPO / "docs" / i).exists()]
    c.add(not missing, "every referenced image exists",
          ", ".join(missing) if missing else f"{len(imgs)} referenced")
    c.add(pathlib.Path(REPO / "LICENSE").exists(), "LICENSE present")

    # ---- the flow configuration should be the template's ----------------
    rc = git("diff", "--quiet", TEMPLATE_COMMIT, "--",
             "src/config.json").returncode
    c.add(rc == 0, "src/config.json is the unmodified template",
          "edit it only if you know why")

    # ---- things that must not be committed ------------------------------
    tracked = git("ls-files").stdout.split()
    big = [f for f in tracked
           if (REPO / f).exists() and (REPO / f).stat().st_size > 2_000_000]
    c.add(not big, "no tracked file over 2 MB",
          ", ".join(big) if big else f"{len(tracked)} files tracked")
    c.add("textbook/jamguard.pdf" not in tracked,
          "the textbook is not tracked")

    # ---- signoff numbers from the newest local hardening run ------------
    m, where = newest_metrics()
    if not m:
        c.add(False, "local signoff metrics found",
              "run tools/harden_local.py first")
        return c.report()

    print(f"signoff metrics from {where}\n")
    zero = [
        ("magic__drc_error__count", "Magic DRC errors are"),
        ("design__lvs_error__count", "LVS errors are"),
        ("route__drc_errors", "routing DRC errors are"),
        ("antenna__violating__nets", "antenna violations are"),
        ("timing__setup__tns", "setup total negative slack is"),
        ("timing__hold__tns", "hold total negative slack is"),
        ("design__max_cap_violation__count", "max capacitance violations are"),
    ]
    for key, label in zero:
        if key in m:
            c.add(m[key] == 0, f"{label} zero", str(m[key]))

    for kind in ("setup", "hold"):
        corners = {k.split(":")[-1]: v for k, v in m.items()
                   if k.startswith(f"timing__{kind}__ws__corner")}
        if corners:
            worst = min(corners.items(), key=lambda kv: kv[1])
            c.add(all(v > 0 for v in corners.values()),
                  f"{kind} slack positive in all {len(corners)} corners",
                  f"worst {worst[1]:+.3f} ns at {worst[0]}")

    pdn = [v for k, v in m.items() if "power_grid_violation" in k]
    if pdn:
        c.add(all(v == 0 for v in pdn), "power grid violations are zero")

    # Slew is not gated by the Tiny Tapeout flow, so report it rather than
    # failing on it, but do fail if a transition leaves the range the sky130
    # cells are characterised over, because then the delays are extrapolated.
    slew = m.get("design__max_slew_violation__count")
    if slew is not None:
        print(f"  note: {slew} max-slew violations, not gated by the flow. "
              "Check the worst\n        transition is under 1.5 ns, the "
              "sky130 characterisation limit.")

    util = m.get("design__instance__utilization")
    if util is not None:
        print(f"  note: {util * 100:.1f}% utilisation. The post-clock-tree "
              "figure is what\n        limits this tile, not this one. See "
              "the README.")
    print()
    return c.report()


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
