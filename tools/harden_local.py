#!/usr/bin/env python3
# SPDX-FileCopyrightText: (c) 2026 Marc Boudames
# SPDX-License-Identifier: Apache-2.0
"""
Run the Tiny Tapeout LibreLane hardening flow locally, in Docker.

    python tools/harden_local.py              # harden and report
    python tools/harden_local.py --open-shell # drop into the run container
    python tools/harden_local.py --clean      # delete the work volume

WHY THIS EXISTS
    CI (.github/workflows/gds.yaml) runs tt-gds-action, which runs
    tt-support-tools' tt_tool.py, which runs LibreLane.  Waiting for a push to
    find out the design does not fit is a slow way to design a chip.  This
    reproduces the same hardening locally so utilization, timing and cell
    counts come back in minutes.

HOW IT MATCHES CI
    Same LibreLane version, same PDK revision, and the same generated config
    values that tt_tool.py --create-user-config produces.  The DIE_AREA and
    DEF template are read out of tt-support-tools itself rather than copied
    into this file, so they cannot silently drift.

    It is still a reproduction, not the authority.  CI remains the thing that
    decides whether a submission is good.

WHY DOCKER VOLUMES AND NOT A BIND MOUNT
    LibreLane writes tens of thousands of files.  This repository lives in a
    OneDrive folder, and bind-mounting it would (a) crawl, because Windows
    paths reach Linux containers over a network filesystem, and (b) hand
    OneDrive a few hundred megabytes of build output to sync.  So sources are
    copied into a Linux-native volume, the flow runs there, and only the small
    artifacts we actually want come back.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import tarfile
import subprocess
import sys
from pathlib import Path

# --- pinned to match .github/workflows/gds.yaml ---------------------------
LIBRELANE_VERSION = "3.0.14"      # tt-gds-action input 'librelane-version'
IMAGE = f"ghcr.io/librelane/librelane:{LIBRELANE_VERSION}"
PDK = "sky130A"
PDK_FAMILY = "sky130"
TT_TOOLS_REPO = "https://github.com/TinyTapeout/tt-support-tools.git"
TT_TOOLS_REF = "main"             # tt-gds-action input 'tools-ref'

WORK_VOLUME = "tt-work"
PDK_VOLUME = "tt-pdk"
CONTAINER = "tt-harden"

REPO = Path(__file__).resolve().parent.parent
CACHE = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "tt-jamguard"
TOOLS_CACHE = CACHE / "tt-support-tools"
OUT_DIR = CACHE / "runs"          # run artifacts land here, outside OneDrive


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, **kw)


def out(cmd: list[str]) -> str:
    return subprocess.run(cmd, check=True, capture_output=True,
                          text=True).stdout.strip()


def quiet(cmd: list[str]) -> int:
    return subprocess.run(cmd, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode


def dexec(*args: str, check: bool = True) -> int:
    """Run a command inside the work container, streaming its output."""
    return subprocess.run(["docker", "exec", CONTAINER, *args],
                          check=check).returncode


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------

def require_docker() -> None:
    if quiet(["docker", "version"]) != 0:
        sys.exit(
            "Docker is not responding. Start Docker Desktop and try again.\n"
            '  Start-Process "C:\\Program Files\\Docker\\Docker\\Docker Desktop.exe"'
        )


def pinned_pdk_rev() -> str:
    """Read the open_pdks revision LibreLane pins, from LibreLane itself."""
    script = (
        "import librelane,os,yaml;"
        "p=os.path.join(os.path.dirname(librelane.__file__),'pdk_hashes.yaml');"
        "print(yaml.safe_load(open(p))['" + PDK_FAMILY + "'])"
    )
    return out(["docker", "run", "--rm", IMAGE, "python3", "-c", script])


def ensure_pdk() -> None:
    quiet(["docker", "volume", "create", PDK_VOLUME])
    present = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{PDK_VOLUME}:/pdk", IMAGE,
         "bash", "-c", f"test -d /pdk/{PDK}/libs.ref && echo yes || echo no"],
        capture_output=True, text=True).stdout.strip()
    if present == "yes":
        print(f"[pdk] {PDK} already present in volume {PDK_VOLUME}")
        return

    rev = pinned_pdk_rev()
    print(f"[pdk] fetching {PDK} at open_pdks {rev[:12]} (this is a big "
          "download, once only)")
    run(["docker", "run", "--rm", "-v", f"{PDK_VOLUME}:/pdk",
         "python:3.12-slim", "bash", "-c",
         "pip install --quiet volare && "
         f"volare enable --pdk {PDK_FAMILY} --pdk-root /pdk {rev}"])


def ensure_tools() -> None:
    """Clone or update tt-support-tools into a host cache."""
    if (TOOLS_CACHE / ".git").exists():
        print(f"[tools] updating {TOOLS_CACHE}")
        run(["git", "-C", str(TOOLS_CACHE), "fetch", "--depth", "1",
             "origin", TT_TOOLS_REF])
        run(["git", "-C", str(TOOLS_CACHE), "checkout", "-q", "FETCH_HEAD"])
    else:
        print(f"[tools] cloning tt-support-tools@{TT_TOOLS_REF}")
        TOOLS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--depth", "1", "--branch", TT_TOOLS_REF,
             TT_TOOLS_REPO, str(TOOLS_CACHE)])
    sha = out(["git", "-C", str(TOOLS_CACHE), "rev-parse", "--short", "HEAD"])
    print(f"[tools] tt-support-tools at {sha}")


# ---------------------------------------------------------------------------
# config generation (the tt_tool.py --create-user-config equivalent)
# ---------------------------------------------------------------------------

def read_info_yaml() -> dict:
    try:
        import yaml
    except ImportError:
        sys.exit("pyyaml is needed on the host:\n"
                 "  .venv\\Scripts\\python -m pip install pyyaml")
    with open(REPO / "info.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)["project"]


def build_merged_config(tiles_override=None) -> dict:
    """Reproduce src/config_merged.json exactly as tt_tool.py would.

    tt_tool.py builds src/user_config.json from info.yaml plus a per-PDK tile
    size table, then merges src/config.json under it.  Both the tile size and
    the DEF template name are taken from the cached tt-support-tools checkout
    so this stays correct if Tiny Tapeout changes them.
    """
    import yaml

    info = read_info_yaml()
    tiles = tiles_override or info["tiles"]

    with open(TOOLS_CACHE / "tech" / PDK / "tile_sizes.yaml",
              encoding="utf-8") as fh:
        tile_sizes = yaml.safe_load(fh)
    if tiles not in tile_sizes:
        sys.exit(f"info.yaml tiles={tiles!r} is not in tile_sizes.yaml")
    die_area = tile_sizes[tiles]

    def_template = f"dir::../tt/tech/{PDK}/def/tt_block_{tiles}_pg.def"
    if not (TOOLS_CACHE / "tech" / PDK / "def"
            / f"tt_block_{tiles}_pg.def").exists():
        sys.exit(f"DEF template for {tiles} not found in tt-support-tools")

    # src/config.json repeats the key "//" for its comments, and json keeps the
    # last one, which is harmless because every real setting is unique.
    with open(REPO / "src" / "config.json", encoding="utf-8") as fh:
        config = json.load(fh)

    config.update({
        "DESIGN_NAME": info["top_module"],
        "VERILOG_FILES": [f"dir::{s}" for s in info["source_files"]],
        "DIE_AREA": die_area,
        "FP_DEF_TEMPLATE": def_template,
        "VDD_PIN": "VPWR",
        "GND_PIN": "VGND",
        "RT_MAX_LAYER": "met4",
    })
    print(f"[config] {info['top_module']}  tiles={tiles}  "
          f"DIE_AREA='{die_area}'")
    print(f"[config] sources: {info['source_files']}")
    return config


# ---------------------------------------------------------------------------
# container lifecycle
# ---------------------------------------------------------------------------

def start_container(fresh_work: bool) -> None:
    quiet(["docker", "rm", "-f", CONTAINER])
    if fresh_work:
        quiet(["docker", "volume", "rm", "-f", WORK_VOLUME])
    quiet(["docker", "volume", "create", WORK_VOLUME])
    run(["docker", "run", "-d", "--name", CONTAINER,
         "-v", f"{WORK_VOLUME}:/work",
         "-v", f"{PDK_VOLUME}:/pdk",
         "-e", "PDK_ROOT=/pdk",
         "-w", "/work",
         IMAGE, "sleep", "infinity"],
        stdout=subprocess.DEVNULL)


def copy_in(config: dict) -> None:
    dexec("bash", "-c", "rm -rf /work/src /work/tt && mkdir -p /work/src")
    run(["docker", "cp", str(REPO / "src") + "/.", f"{CONTAINER}:/work/src"])
    run(["docker", "cp", str(REPO / "info.yaml"), f"{CONTAINER}:/work/info.yaml"])
    run(["docker", "cp", str(TOOLS_CACHE) + "/.", f"{CONTAINER}:/work/tt"])

    staged = CACHE / "config_merged.json"
    staged.parent.mkdir(parents=True, exist_ok=True)
    with open(staged, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
        fh.write("\n")
    run(["docker", "cp", str(staged),
         f"{CONTAINER}:/work/src/config_merged.json"])


def harden() -> int:
    print("\n[librelane] starting flow "
          "(expect roughly 5-15 minutes for a 1x1 tile)\n")
    # The run directory must already exist, and tt_tool.py makes it before
    # LibreLane, so do the same here.
    return dexec(
        "bash", "-c",
        "cd /work && rm -rf runs/wokwi && mkdir -p runs/wokwi && "
        "python3 -m librelane --pdk-root /pdk "
        "--run-tag wokwi --force-run-dir /work/runs/wokwi "
        "--hide-progress-bar src/config_merged.json",
        check=False,
    )


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------

def copy_out() -> None:
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR, ignore_errors=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Everything under final/ is small and is what the submission is made of.
    quiet(["docker", "cp", f"{CONTAINER}:/work/runs/wokwi/final",
           str(OUT_DIR / "final")])
    # The per-step logs and reports, for diagnosing failures.
    dexec("bash", "-c",
          "cd /work/runs/wokwi && tar czf /work/reports.tgz "
          "$(ls -d */ | head -200) 2>/dev/null || true", check=False)
    quiet(["docker", "cp", f"{CONTAINER}:/work/reports.tgz",
           str(OUT_DIR / "reports.tgz")])

    # The gate-level netlist is what test/run_local.py --gl needs.  Take the
    # post-place-and-route netlist (final/pnl/*.pnl.v), which is what
    # tt-support-tools uses for sky130 (Sky130Tech.netlist_type == "pnl") and
    # therefore what CI's gl_test runs against.  final/nl/ holds the
    # pre-layout netlist, without the physical cells.
    dest = REPO / "test" / "gate_level_netlist.v"
    for sub, pattern in (("pnl", "*.pnl.v"), ("nl", "*.nl.v")):
        found = sorted((OUT_DIR / "final" / sub).glob(pattern))
        if found:
            shutil.copyfile(found[0], dest)
            print(f"[out] {found[0].name} -> {dest.relative_to(REPO)}")
            break
    else:
        print("[out] no gate-level netlist found; --gl will not run")
    print(f"[out] artifacts in {OUT_DIR}")


def density_gate() -> None:
    """Warn when the design is heading for a detailed-placement failure.

    This exists because a 1x2 revision of this project died at
    OpenROAD.ResizerTimingPostCTS with "[DPL-0036] Detailed placement
    failed", nine stages after the flow already knew.  What goes wrong is not
    setup timing and not logic area.  The post-CTS resizer always inflates
    the design by about 12 percent, because PL_RESIZER_HOLD_SLACK_MARGIN asks
    it to build 100 ps of hold margin and it does so with eight-site
    sky130_fd_sc_hd__dlygate4sd3_1 delay cells, one for roughly every
    three quarters of a flip-flop.  If the post-CTS design is already dense,
    there is nowhere to legalise them, and the run dies after about twenty
    minutes of work.

    Global placement prints three numbers that predict this outcome.  The
    failing run and the passing run differed sharply on all three, so these
    thresholds are measured rather than guessed:

        GPL-1004 minimum feasible density     0.86 failed, 0.71 passed
        GPL-1007 suggested density for 90%    1.07 failed, 0.79 passed
        GPL-1012 routability inflation       11.4% failed,  0.4% passed

    A GPL-1007 value above 1.0 is arithmetically unplaceable and is the
    clearest single signal of the three.

    The useful size rule that falls out of it: a flip-flop in this design
    really costs about 31 square microns placed, not the 21 its library cell
    occupies, once its share of hold buffer and clock buffer is counted.  So
    a 1x2 tile holds roughly 360 flip-flops, and that is the budget to design
    against.
    """
    # The stage logs ship inside reports.tgz, so read the one we need
    # straight out of the archive rather than unpacking a thousand files.
    text = ""
    tgz = OUT_DIR / "reports.tgz"
    if tgz.exists():
        try:
            with tarfile.open(tgz) as tf:
                for name in tf.getnames():
                    if ("28-openroad-globalplacement" in name
                            and name.endswith(".log")):
                        fh = tf.extractfile(name)
                        if fh is not None:
                            text = fh.read().decode("utf-8", "replace")
                        break
        except (tarfile.TarError, OSError):
            return
    if not text:
        return

    checks = []
    for tag, pattern, limit, label in (
        ("GPL-1004", r"Minimum Feasible Density[: ]+([0-9.]+)", 0.75,
         "minimum feasible density"),
        ("GPL-1007", r"For 90% usage of free space:\s*([0-9.]+)", 1.00,
         "density needed for 90% free space"),
        ("GPL-0019", r"Utilization:\s*([0-9.]+)\s*%", 75.0,
         "global placement utilization, percent"),
    ):
        hit = re.search(pattern, text)
        if hit:
            val = float(hit.group(1))
            checks.append((val > limit, f"{label} {val:.3f} "
                                        f"(stay under {limit:.2f})"))

    hit = re.search(r"artificial inflation:[^(]*\(\+?([0-9.]+)%\)", text)
    if hit:
        val = float(hit.group(1))
        checks.append((val > 2.0, f"routability inflation {val:.2f}% "
                                  "(stay under 2%)"))

    bad = [msg for over, msg in checks if over]
    if not bad:
        if checks:
            print("\n  placement headroom looks healthy: "
                  + ", ".join(msg for _, msg in checks))
        return

    print("\n  PLACEMENT HEADROOM WARNING")
    for msg in bad:
        print(f"    {msg}")
    print("    The post-CTS resizer inflates this design by about 12 percent\n"
          "    to build hold margin, and at this density it may not be able\n"
          "    to legalise the delay cells it needs.  Shrink the design.\n"
          "    Flip-flops are the lever: one costs about 31 square microns\n"
          "    placed, so a 1x2 tile holds roughly 360 of them.")


def report() -> None:
    metrics_path = OUT_DIR / "final" / "metrics.json"
    if not metrics_path.exists():
        print("[report] no metrics.json; the flow probably failed early")
        return
    with open(metrics_path, encoding="utf-8") as fh:
        m = json.load(fh)

    interesting = [
        ("design__instance__count", "cells"),
        ("design__instance__count__sequential", "flip-flops"),
        ("design__instance__area", "cell area (um^2)"),
        ("design__die__area", "die area (um^2)"),
        ("design__instance__utilization", "utilization"),
        ("timing__setup__ws", "setup worst slack (ns)"),
        ("timing__hold__ws", "hold worst slack (ns)"),
        ("timing__setup__vios", "setup violations"),
        ("timing__hold__vios", "hold violations"),
        ("route__drc_errors", "routing DRC errors"),
        ("route__wirelength", "wirelength (um)"),
        ("antenna__violating__nets", "antenna violations"),
        ("power__total", "power (W)"),
    ]
    print("\n" + "=" * 62)
    print("LibreLane results")
    print("=" * 62)
    for key, label in interesting:
        if key in m:
            val = m[key]
            if isinstance(val, float):
                val = f"{val:.4g}"
            print(f"  {label:28s} {val}")

    cells = m.get("design__instance__count")
    util = m.get("design__instance__utilization")
    if util is not None:
        print(f"\n  utilization is {util * 100:.1f}% of the tile "
              f"({cells} cells).")

    ws = m.get("timing__setup__ws")
    if ws is not None and ws < 0:
        print(f"\n  NEGATIVE SETUP SLACK ({ws:.3f} ns): raise CLOCK_PERIOD in "
              "src/config.json rather than redesigning.")

    density_gate()
    print("=" * 62)
    print("\nNext:  python test/run_local.py --gl   (gate-level simulation)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--clean", action="store_true",
                    help="delete the work volume and container, then exit")
    ap.add_argument("--open-shell", action="store_true",
                    help="start the container and exec a shell in it")
    ap.add_argument("--keep-work", action="store_true",
                    help="reuse the existing work volume instead of wiping it")
    ap.add_argument("--tiles", default=None,
                    help="override info.yaml tiles, for trying a different "
                         "die size without editing the repo")
    ap.add_argument("--report-only", action="store_true",
                    help="re-print the report from the last run's artifacts")
    args = ap.parse_args()

    if args.report_only:
        report()
        return 0

    require_docker()

    if args.clean:
        quiet(["docker", "rm", "-f", CONTAINER])
        quiet(["docker", "volume", "rm", "-f", WORK_VOLUME])
        print("removed container and work volume "
              f"(PDK volume {PDK_VOLUME} kept)")
        return 0

    ensure_pdk()
    ensure_tools()
    config = build_merged_config(args.tiles)

    start_container(fresh_work=not args.keep_work)
    copy_in(config)

    if args.open_shell:
        print(f"container {CONTAINER} is up; /work has src/, tt/ and the "
              "merged config")
        return subprocess.run(["docker", "exec", "-it", CONTAINER,
                               "bash"]).returncode

    rc = harden()
    copy_out()
    report()
    if rc != 0:
        print(f"\nLibreLane exited {rc}. Reports are in "
              f"{OUT_DIR / 'reports.tgz'}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
