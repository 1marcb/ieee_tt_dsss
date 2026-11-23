# JAMGUARD, a DSSS anti-jam link core for Tiny Tapeout SKY 26e

A direct sequence spread spectrum modem on a 1×2 Tiny Tapeout tile: a Gold
code spreader, a correlating despreader that finds both chip and byte timing
on its own, SECDED forward error correction, a frequency-hopping sequence, and
a built-in jammer (broadband or partial-band) so the anti-jam margin can be
measured with no extra equipment.

Every feature beyond the basic link is a mode bit, off by default, so a plain
reset still gives the original 1×1 behaviour.

| | |
|---|---|
| Shuttle | Tiny Tapeout **SKY 26e** (sky130A), closes 2026-11-30 |
| Area | **1×2 tile**, 161.00 × 225.76 µm, **37.7% utilised** |
| Size | **168 flip-flops, 1486 standard cells** (post-layout) |
| Top module | `tt_um_1marcb_jamguard` |
| Clock | 10 MHz declared, hardened against a 20 ns period (50 MHz) for margin |
| Code | length-31 Gold code, preferred pair `[5,2]` / `[5,4,3,2]` |
| Processing gain | 10·log₁₀(31) = **14.9 dB** |
| Jam tolerance | decodes through **15 of 31 corrupted chips (48%)** |
| Foreign-code rejection | cross-correlation bounded at 9/31, lock needs 19/31 |
| FEC | 2 × SECDED(8,4), bit-interleaved: corrects 1 error per codeword, any 2-bit burst |
| Framing | sync word `0xE4` (Barker 7 + 0), 1 bit error tolerated |
| Frequency hopping | 63 channels, per-bit or per-chip dwell, seeded from the code ID |

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
For the 1×2 features it also proves that the SECDED code has minimum distance
4, corrects every single error and flags every double, that interleaving
repairs every 2-bit burst, that a partly received sync word can never match,
and that the jammer and hop LFSRs are maximal length. It finishes with the
theoretical bit and byte error rate at every jam level, with and without FEC.

Then the hardware is compared against that model, never against hand-written
expected values:

```bash
cd test && make -B                 # what CI runs
python test/run_local.py           # same thing, no make needed (Windows)
python test/run_local.py --gl      # against the hardened netlist
```

Nineteen tests. The original ten: reset state, the 248-chip transmit frame
chip-for-chip, a 256-byte loopback sweep, the exact jam cliff at J=15/16,
foreign-code rejection both aligned and free-running, serial-search
acquisition from a wrong phase, the four-tier jammer ladder, control inputs
driven at random sub-cycle offsets, and the output drivers being tri-stated by
`TX_PINS_OFF`. Nine more for the 1×2 features: the configuration pages, every
frame format chip-for-chip, loopback in every mode (all 256 bytes with FEC and
framing), SECDED against injected single, double and burst errors, FEC
against the on-chip jammer, a receiver with no epoch wire starting at random
chip offsets, sync-word error tolerance, the hop sequence chip by chip, and
the partial-band jammer against parked and hopping transmitters.

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
| cocotb RTL simulation | **19/19** |
| Verilator `--lint-only -Wall` | **0 warnings** (besides the template's file name) |
| **cocotb gate-level simulation** (post-PnR netlist + sky130 cells) | **19/19** |
| LibreLane hardening, 1×2 | **pass**, 37.7% utilisation |
| Static timing, all 9 corners | setup **+6.77 ns**, hold **+0.105 ns** |
| Routing DRC / Magic DRC / antenna / LVS | **0 / 0 / 0 / match** |
| GDS + precheck + gl\_test in CI | pending push of the 1×2 build |
| iCE40UP5K build (Yosys + nextpnr) | **pass**, 21.2 MHz, 2.1× the 10 MHz target |
| FPGA on real hardware | pending a board |

RTL and gate-level run the *same* nineteen tests with the *same* coverage.
Gate level takes about 50 seconds.

### Signoff, 1×2 build (local LibreLane 3.0.14, same version and PDK as CI)

| check | result |
|---|---|
| **LVS (netgen)** | **circuits match**, 0 errors |
| Magic DRC | **0** errors |
| detailed routing DRC | **0** errors |
| antenna violations | **0** nets |
| setup violations | **0** in all 9 corners |
| hold violations | **0** in all 9 corners |
| max capacitance violations | **0** |
| max slew violations | **80**, slow-slow corners only, see below |
| IR drop | 0.064 mV worst on 1.8 V |

LVS matching is the line that matters most. It says the extracted layout and
the synthesised netlist are the same circuit.

### Post-layout results

| metric | value |
|---|---|
| sequential cells | 168 |
| standard cells | 1486, of which 566 combinational, 209 timing-repair buffers, 57 clock buffers |
| utilisation | 37.7% of the 1×2 tile |
| worst setup slack | +6.77 ns at a 20 ns period (max_ss_100C_1v60) |
| worst hold slack | +0.105 ns (ff corners) |
| wirelength | 22 773 µm |
| total power | 1.26 mW |

### The max-slew violations, and why they are left alone

The 1×2 build reports 80 max-slew violations: 80 at `max_ss_100C_1v60`, 62 at
`nom_ss`, 44 at `min_ss`, and **none** at any typical or fast corner. The worst
is 1.03 ns against a 0.75 ns limit, on nets the resizer buffered with the weak
`clkdlybuf4s25_1` cell. The taller tile has longer wires than the 1×1 build,
and LibreLane's design repair only optimises for the typical corner.

Why they are acceptable:

- The 0.75 ns limit is LibreLane's conservative default. The sky130 standard
  cells are characterised up to 1.5 ns, so 1.03 ns is still inside the
  characterised tables and the timing numbers are not extrapolated.
- Timing closes with +6.77 ns of setup slack at a 20 ns period in that same
  corner, and the chip is declared at 10 MHz, a 100 ns period.
- The worst net is `fec_en`, a configuration bit that only changes during
  reset.
- Tiny Tapeout's precheck does not check slew.

Three flow-level fixes were tried and measured before settling on this: a 40%
slew margin (80 → 62), excluding the `clkdlybuf4s` cells (80 → 145) and adding
the slow corner to `PNR_CORNERS` (no change). None was clean, so
`src/config.json` stays at the template defaults.

### Protection against output contention

Two boards running this design both drive `uio[6]` and `uio[7]`, so wiring
them together carelessly puts one output driver against another with only
the drivers' on resistance limiting the current. Holding `uio_in[5]` high
during reset sets `TX_PINS_OFF`, which tri-states both output pins for the
whole run. `uio_oe` then reads `0x00` and the part drives nothing anywhere
on the bidirectional bus, while still receiving normally and still running
loopback internally.

It costs one flip-flop that the design was already carrying as a reserved
configuration field. The alternative of driving the pins only while
transmitting was rejected because it leaves the chip and epoch lines
floating between frames, and a floating epoch input can glitch a correlation
window into existence.

## Remaining work

- [x] `test`, `gds`, `precheck`, `gl_test`, `fpga`, `docs` and `viewer` all green in CI (1×1 build)
- [x] 1×2 features: FEC, sync-word framing, frequency hopping, partial-band jammer
- [ ] Push the 1×2 build and confirm every workflow is green again
- [ ] Load the bitstream on an iCE40 board and run the loopback + jammer demo
      (`tools/fpga_local.py` already builds it, and this is the step that
      exercises genuinely asynchronous inputs)
- [ ] Submit in the Tiny Tapeout app **as 1×2**, confirm the coupon covers two tiles, resubmit final commit
- [ ] Optional: SymbiYosys formal properties

## Licence

Apache-2.0, see [LICENSE](LICENSE).
