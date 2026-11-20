# SPDX-FileCopyrightText: (c) 2026 Marc Boudames
# SPDX-License-Identifier: Apache-2.0
"""
Run the cocotb suite without GNU make.

The Tiny Tapeout template drives cocotb through test/Makefile, which is what
CI uses.  Windows has no make, so this script reproduces the same two builds
through cocotb's Python runner API:

    python run_local.py              # RTL simulation  (same as: make -B)
    python run_local.py --gl         # gate level      (same as: make -B GATES=yes)
    python run_local.py -k loopback  # run only tests whose name matches

The gate-level mode needs two things the GDS run produces:
  * test/gate_level_netlist.v   (copied out of runs/wokwi/final/pnl/)
  * a sky130A PDK at $PDK_ROOT  (volare installs one; see harden_local.py)

Nothing here is used by CI, so it can never make CI pass while the real flow
would fail -- the Makefile stays the single source of truth for that.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

TEST_DIR = Path(__file__).parent.resolve()
SRC_DIR = TEST_DIR.parent / "src"
PROJECT_SOURCES = ["project.v"]
TOPLEVEL = "tb"
TEST_MODULE = "test"

# winget drops Icarus here and does not add it to PATH.
ICARUS_HINTS = [
    Path(r"C:\iverilog\bin"),
    Path(r"C:\Program Files\iverilog\bin"),
    Path(r"C:\Program Files (x86)\iverilog\bin"),
]


def ensure_icarus() -> None:
    if shutil.which("iverilog"):
        return
    for d in ICARUS_HINTS:
        if (d / "iverilog.exe").exists() or (d / "iverilog").exists():
            os.environ["PATH"] = f"{d}{os.pathsep}{os.environ['PATH']}"
            return
    sys.exit(
        "iverilog not found. Install it with:  winget install --id Icarus.Verilog\n"
        "or add its bin directory to PATH."
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gl", action="store_true",
                    help="simulate the hardened gate-level netlist")
    ap.add_argument("-k", "--testcase", default=None,
                    help="run only this test (comma separated, exact names)")
    ap.add_argument("--waves", action="store_true", default=True,
                    help="dump test/tb.fst (on by default)")
    args = ap.parse_args()

    ensure_icarus()
    from cocotb_tools.runner import get_runner

    sources = [TEST_DIR / "tb.v"]
    defines: dict[str, str | int] = {}
    build_args: list[str] = [f"-I{SRC_DIR}"]
    build_dir = TEST_DIR / "sim_build" / ("gl" if args.gl else "rtl")

    if args.gl:
        pdk_root = os.environ.get("PDK_ROOT")
        if not pdk_root:
            sys.exit("PDK_ROOT is not set. Run harden_local.py first, or point "
                     "PDK_ROOT at a sky130A install.")
        netlist = TEST_DIR / "gate_level_netlist.v"
        if not netlist.exists():
            sys.exit(f"{netlist} is missing. Copy it from the GDS run:\n"
                     r"  runs\wokwi\final\pnl\*.nl.v  ->  test\gate_level_netlist.v")
        cells = Path(pdk_root) / "sky130A" / "libs.ref" / "sky130_fd_sc_hd" / "verilog"
        sources = [
            cells / "primitives.v",
            cells / "sky130_fd_sc_hd.v",
            netlist,
            TEST_DIR / "tb.v",
        ]
        # Same defines the Makefile passes for GATES=yes.
        defines.update(GL_TEST=1, FUNCTIONAL=1, USE_POWER_PINS=1, SIM=1)
        build_args.append("-DUNIT_DELAY=#1")
    else:
        sources = [SRC_DIR / name for name in PROJECT_SOURCES] + sources

    missing = [str(s) for s in sources if not Path(s).exists()]
    if missing:
        sys.exit("missing source files:\n  " + "\n  ".join(missing))

    runner = get_runner("icarus")
    runner.build(
        verilog_sources=sources,
        hdl_toplevel=TOPLEVEL,
        defines=defines,
        build_args=build_args,
        build_dir=build_dir,
        always=True,          # equivalent to make -B
        waves=args.waves,
    )

    results = runner.test(
        hdl_toplevel=TOPLEVEL,
        test_module=TEST_MODULE,
        testcase=args.testcase,
        build_dir=build_dir,
        test_dir=TEST_DIR,
        results_xml="results.xml",
        waves=args.waves,
    )

    # Mirror what CI does: the runner exits 0 even when assertions fail, so
    # the XML is the authority.
    from cocotb_tools.check_results import get_results

    total, failed = get_results(Path(results))
    mode = "GATE-LEVEL" if args.gl else "RTL"
    print(f"\n{mode}: {total - failed}/{total} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
