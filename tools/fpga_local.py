#!/usr/bin/env python3
# SPDX-FileCopyrightText: (c) 2026 Marc Boudames
# SPDX-License-Identifier: Apache-2.0
"""
Build the design for an iCE40UP5K locally, the way the fpga workflow does.

    python tools/fpga_local.py

An FPGA build is the cheapest check that does not exist in simulation. It
runs the same Verilog through a different synthesiser (Yosys targeting
iCE40 rather than sky130), which catches constructs that happen to work in
one technology, and it reports a real maximum clock frequency from
place-and-route timing analysis.

Loading the bitstream onto a board additionally exercises the thing
simulation structurally cannot: genuinely asynchronous inputs arriving at
arbitrary times relative to the clock, which is what the two-flop
synchronisers exist for.

Everything runs through the WASM builds of Yosys and nextpnr, so there is no
native toolchain to install.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BUILD = REPO / "build" / "fpga"
# tools/harden_local.py caches tt-support-tools here; reuse it.
TOOLS_CACHE = Path(
    __import__("os").environ.get("LOCALAPPDATA", str(Path.home()))
) / "tt-jamguard" / "tt-support-tools"

TOP = "tt_fpga_top"
DEVICE = "up5k"
PACKAGE = "sg48"
FREQ_MHZ = 10          # matches clock_hz in info.yaml


def venv_bin(name: str) -> str:
    for cand in (REPO / ".venv" / "Scripts" / f"{name}.exe",
                 REPO / ".venv" / "bin" / name):
        if cand.exists():
            return str(cand)
    found = shutil.which(name)
    if found:
        return found
    sys.exit(f"{name} not found. Install it with:\n"
             f"  .venv/Scripts/python -m pip install yowasp-yosys "
             f"yowasp-nextpnr-ice40")


def top_module() -> str:
    import yaml
    with open(REPO / "info.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)["project"]["top_module"]


def main() -> int:
    if not TOOLS_CACHE.exists():
        sys.exit(f"tt-support-tools not cached at {TOOLS_CACHE}.\n"
                 "Run tools/harden_local.py once first.")

    BUILD.mkdir(parents=True, exist_ok=True)
    name = top_module()

    # The Tiny Tapeout FPGA wrapper instantiates a placeholder name; the
    # workflow substitutes the real module. Do the same.
    wrapper_src = (TOOLS_CACHE / "fpga" / "tt_fpga_top.v").read_text(
        encoding="utf-8")
    wrapper = wrapper_src.replace("__tt_um_placeholder", name)
    (BUILD / "tt_fpga_top.v").write_text(wrapper, encoding="utf-8")

    # The WASM builds of Yosys and nextpnr can only see the working
    # directory, so every path handed to them is relative to the repo root
    # and every input is copied inside it first.
    shutil.copyfile(TOOLS_CACHE / "fpga" / "tt_fpga_top.pcf",
                    BUILD / "tt_fpga_top.pcf")
    rel = BUILD.relative_to(REPO).as_posix()
    pcf = f"{rel}/tt_fpga_top.pcf"
    json_out = f"{rel}/jamguard.json"
    asc_out = f"{rel}/jamguard.asc"

    ys = BUILD / "synth.ys"
    ys.write_text(
        "read_verilog -sv src/project.v\n"
        f"read_verilog -sv {rel}/tt_fpga_top.v\n"
        f"synth_ice40 -top {TOP} -json {json_out}\n"
        "stat\n",
        encoding="utf-8")

    print("=" * 62)
    print(f"iCE40 {DEVICE}/{PACKAGE} build of {name}")
    print("=" * 62)

    print("\n[1/2] Yosys synth_ice40")
    syn = subprocess.run(
        [venv_bin("yowasp-yosys"), "-s", f"{rel}/synth.ys"],
        capture_output=True, text=True, cwd=REPO)
    if syn.returncode != 0:
        print(syn.stdout[-4000:])
        print(syn.stderr[-2000:])
        return syn.returncode

    # Report the cell summary rather than the whole log.
    tail = syn.stdout[syn.stdout.rfind("=== tt_fpga_top ==="):]
    for line in tail.splitlines():
        if re.match(r"\s+(Number of cells|SB_\w+|\$\w+)", line) or \
           "Number of" in line:
            print("   " + line.strip())

    print("\n[2/2] nextpnr-ice40 place, route and timing")
    pnr = subprocess.run(
        [venv_bin("yowasp-nextpnr-ice40"), f"--{DEVICE}",
         "--package", PACKAGE,
         "--json", json_out, "--pcf", pcf,
         "--asc", asc_out, "--freq", str(FREQ_MHZ),
         "--pcf-allow-unconstrained"],
        capture_output=True, text=True, cwd=REPO)
    log = pnr.stdout + pnr.stderr
    if pnr.returncode != 0:
        print(log[-5000:])
        return pnr.returncode

    for line in log.splitlines():
        if any(k in line for k in (
                "Max frequency", "Device utilisation", "ICESTORM_LC",
                "ICESTORM_RAM", "SB_IO", "Info: Max delay",
                "Total path delay", "constraint")):
            print("   " + line.replace("Info: ", "").rstrip())

    print("\n" + "=" * 62)
    m = re.findall(r"Max frequency for clock\s+'([^']+)':\s+([\d.]+)\s*MHz", log)
    for clk, mhz in m:
        margin = float(mhz) / FREQ_MHZ
        print(f"  {clk}: {mhz} MHz  ({margin:.1f}x the {FREQ_MHZ} MHz target)")
    print(f"  bitstream ASCII: {REPO / asc_out}")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
