# JAMGUARD, a DSSS anti-jam link core for Tiny Tapeout SKY 26e

A direct sequence spread spectrum modem on a single 1×1 Tiny Tapeout
tile: a Gold code spreader, a correlating despreader, and a built-in jammer so
the anti-jam margin can be measured with no extra equipment.

| | |
|---|---|
| Shuttle | Tiny Tapeout **SKY 26e** (sky130A), closes 2026-11-30 |
| Area | **1×1 tile**, 161.00 × 111.52 µm |
| Size | **109 flip-flops, 473 generic cells** (Yosys, technology independent) |
| Top module | `tt_um_1marcb_jamguard` |
| Clock | 10 MHz declared, hardened against a 20 ns period (50 MHz) for margin |
| Code | length-31 Gold code, preferred pair `[5,2]` / `[5,4,3,2]` |
| Processing gain | 10·log₁₀(31) = **14.9 dB** |
| Jam tolerance | decodes through **15 of 31 corrupted chips (48%)** |
| Foreign-code rejection | cross-correlation bounded at 9/31, lock needs 19/31 |

See [docs/info.md](docs/info.md) for the datasheet and the bench demos.

## Repository layout

```
src/project.v              the whole design, one file
src/config.json            LibreLane settings (template defaults, unmodified)
info.yaml                  Tiny Tapeout project metadata and pinout
docs/info.md               datasheet, generated into the TT project page
test/jamguard_model.py     independent software model + coding-theory proofs
test/test.py               cocotb suite, runs on RTL and on the netlist
test/tb.v, test/Makefile   the template harness CI uses
test/run_local.py          run the suite without GNU make (Windows)
tools/harden_local.py      run the LibreLane hardening locally, in Docker
tools/synth_stat.ys        quick Yosys cell/flip-flop count
tools/fpga_local.py        iCE40UP5K build via Yosys + nextpnr (WASM, no install)
```

## Verifying the design

The claims this chip makes are based from coding theory claims, and thus checked
independently of the hardware:

```bash
python test/jamguard_model.py
```

This proves, from scratch, that both polynomials are primitive, that the pair
is *preferred* (correlations confined to `{-9,-1,+7}`, meeting the bound
`t(n) = 1 + 2^((n+1)/2) = 9`), that the 31 seeds give 31 distinct codes, that a
31-chip majority vote survives exactly 15 errors, and that the lock threshold
clears the worst **misaligned** correlation a free-running receiver can see.

Then the hardware is compared against that model, never against hand-written
expected values:

```bash
cd test && make -B                 # what CI runs
python test/run_local.py           # same thing, no make needed (Windows)
python test/run_local.py --gl      # against the hardened netlist
```

Nine tests: reset state, the 248-chip transmit frame chip-for-chip, a
256-byte loopback sweep, the exact jam cliff at J=15/16, foreign-code rejection
both aligned and free-running, serial-search acquisition from a wrong phase,
the four-tier jammer ladder, and control inputs driven at random sub-cycle
offsets.

### Clock domain crossing audit

Every external pin that feeds a control decision passes through a two-stage
synchroniser. Three places sample an input pin directly instead:

| site | signal | why it is acceptable |
|---|---|---|
| `cfg_a <= ui_in` | configuration | loaded only while `rst_n` is low, from switches a human sets beforehand |
| `cfg_b <= uio_in[5:0]` | configuration | same |
| `tx_shift <= ui_in` | transmit data | the data byte, static during a frame by construction |

The distinction that matters is that none of those feed a state machine. They
land in data registers, so a sample taken while a switch is moving yields a
wrong value, not an illegal state. The classic fatal crossing bug, where one
asynchronous signal reconverges on two paths and a state machine sees it two
different ways, cannot occur here because every control input is
synchronised. Adding synchronisers to `ui_in` would cost 16 flip-flops and
would not remove the underlying race, which is inherent to sampling a
mechanical switch.

## Local hardening

```bash
python tools/harden_local.py     # ASIC: LibreLane, area and timing
python tools/fpga_local.py       # FPGA: iCE40UP5K synthesis and timing
```

Reproduces the CI hardening on this machine using the same LibreLane version
and PDK revision, and prints utilization, timing slack and cell counts. It
needs Docker running. Run outputs are kept in `%LOCALAPPDATA%\tt-jamguard`
deliberately, because LibreLane writes tens of thousands of files and this
lives in a synced folder.

## Local setup from scratch

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r test/requirements.txt pyyaml yowasp-yosys
winget install --id Icarus.Verilog          # the simulator
# Docker Desktop, for tools/harden_local.py
```

## Verification status

| check | result |
|---|---|
| Software model self-check (coding theory proved from scratch) | **pass** |
| cocotb RTL simulation | **9/9** |
| Verilator `--lint-only -Wall` | **0 warnings** |
| **cocotb gate-level simulation** (post-PnR netlist + sky130 cells) | **9/9** |
| LibreLane hardening | **pass**, 42.0% utilisation |
| Static timing, all 9 corners | setup **+10.27 ns**, hold **+0.104 ns** |
| Routing DRC / Magic DRC / antenna | **0 / 0 / 0** |
| GDS + precheck + gl\_test in CI | pending first push |
| iCE40UP5K build (Yosys + nextpnr) | **pass**, 199/5280 LCs, 34.9 MHz |
| FPGA on real hardware | pending a board |

RTL and gate-level run the *same* nine tests with the *same* coverage,
all 256 bytes, every jam level, both foreign-code cases. Gate level takes
about 15 seconds.

### Post-layout results

| metric | value |
|---|---|
| sequential cells | 109 (identical to the synthesis count) |
| standard cells | 797, of which 313 combinational |
| fill / tap cells | 2736 / 225 (inert, filling the unused 58%) |
| utilisation | 42.0% |
| worst setup slack | +10.27 ns at a 20 ns period (slow corner) |
| worst hold slack | +0.104 ns (fast corner) |
| wirelength | 11 078 µm |
| total power | 0.56 mW |

Two benign warnings, deliberately not "fixed": one max-fanout violation on
`clkbuf_0_clk` (the clock-tree root buffer, created by CTS, not by the RTL),
and ten max-slew violations of -0.008 ns against a 0.75 ns limit in the
slowest corner. Both come from the reset net, which drives 86 of the 109
flip-flops and is the critical path. With +10.27 ns of slack at 20 ns, and
90 ns at the declared 10 MHz, reducing reset fanout would mean deleting
resets, which would trade away the X-free gate-level simulation those resets
buy. That is the wrong trade.

## Remaining work

- [ ] Push and confirm `test`, `gds`, `precheck`, `gl_test` green in CI
- [ ] Load the bitstream on an iCE40 board and run the loopback + jammer demo
      (`tools/fpga_local.py` already builds it, and this is the step that
      exercises genuinely asynchronous inputs)
- [ ] Submit in the Tiny Tapeout app, apply the IEEE CS coupon, resubmit final commit
- [ ] Optional: SymbiYosys formal properties

## Licence

Apache-2.0, see [LICENSE](LICENSE).
