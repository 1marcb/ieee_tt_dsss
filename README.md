# JAMGUARD, a DSSS anti-jam link core for Tiny Tapeout SKY 26e

A direct sequence spread spectrum modem with its own test equipment built in,
on a 1×2 Tiny Tapeout tile. It spreads each data bit over a Gold code of 31 or
127 chips, despreads it with three parallel correlators so it can find and
then *hold* the chip phase on its own, finds byte boundaries from a sync word,
corrects single-bit errors, hops across 63 channels, and carries four kinds of
on-chip jammer so the anti-jam margin can be measured on a bench with no
instruments at all.

Every feature beyond the basic link is a mode bit, off by default, so a plain
reset still gives the original behaviour.

| | |
|---|---|
| Shuttle | Tiny Tapeout **SKY 26e** (sky130A), closes 2026-11-30 |
| Area | **1×2 tile**, 161.00 × 225.76 µm, **73.8% utilised** |
| Size | **352 flip-flops, 2620 standard cells** (post-layout) |
| Top module | `tt_um_1marcb_jamguard` |
| Clock | 10 MHz declared, hardened against a 20 ns period (50 MHz) for margin |
| Codes | 31 chips from a degree-5 preferred pair, or 127 from a degree-7 pair |
| Processing gain | 10·log₁₀(31) = **14.91 dB**, or 10·log₁₀(127) = **21.04 dB** |
| Jam tolerance, decode | **15 of 31 chips (48.4%)**, or **63 of 127 (49.6%)** |
| Jam tolerance, lock | 6 of 31 (19.4%), or **39 of 127 (30.7%)** |
| Foreign-code rejection | bounded at 9/31 and 17/127, both bounds attained |
| Code tracking | early/late correlators, one chip of correction per window |
| FEC | 2 × SECDED(8,4), bit-interleaved: corrects 1 error per codeword, any 2-bit burst |
| Framing | sync word `0xE4` (Barker 7 + 0), 1 bit error tolerated |
| Frequency hopping | 63 channels, per-bit or per-chip dwell, seeded from the code ID |
| Jammer | broadband, pulsed, periodic and follower, any of them band-limited |
| Measurement | PRBS-9 bit error rate tester, mean correlation magnitude, foreign-code peak |

See [docs/info.md](docs/info.md) for the datasheet and the bench demos.

## What makes this more than a spreader and a correlator

Three things in the receiver are worth singling out, because they are what
turn a demonstration into something usable.

**It tracks the chip phase, not just acquires it.** Two boards with their own
crystals drift apart. At 100 parts per million the chip phase slips once every
10 000 chips, which at 31 chips per bit is one slip every 40 bytes, and a slip
costs the receiver its lock. The receiver therefore runs three correlators over
every window: on time, one chip advanced, and one chip retarded. If a
neighbour would have locked when the on-time correlator did not, the next
window is lengthened or shortened by one chip, which pulls the phase back
inside a single code period.

That matters asymmetrically. The serial search can only ever *retard* the
local phase, so a slip in the advancing direction takes up to L−1 further
slips to walk the long way round. Measured on the hardware with one chip
dropped every four frames, delivery goes from 5 of 16 bytes to 11 of 16 once
tracking is on.

**It measures its own link.** A PRBS-9 generator feeds the transmitter and a
self-synchronising checker on the receive side counts prediction failures
against a 16-bit byte counter, so a bit error rate is a reading rather than an
estimate. A fourth correlator runs a *different* code of the same family over
the same windows and holds the peak it sees, which turns the cross-correlation
bound from a claim into a measurement. And the mean correlation magnitude over
256 windows is the link margin, readable on eight pins by eye.

**It defeats the matched attack, measurably.** The follower jammer replays the
chip stream back after a selectable delay, which is a jammer using the
victim's own code. It gains nothing, because a Gold code correlated against a
shifted copy of itself gives at most t(n), never the code length. On the
hardware that is 0 bit errors in 32 at replay delays of 1 and 4 chips, and in
the model the worst replay correlation is 17 of 127 against a lock threshold
of 49.

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
tools/synth_stat.ys        quick Yosys cell/flip-flop estimate
tools/fpga_local.py        iCE40UP5K build via Yosys + nextpnr (WASM, no install)
```

## Verifying the design

The claims this chip makes are based from coding theory claims, and thus checked
independently of the hardware:

```bash
python test/jamguard_model.py
```

It takes about two minutes and proves, from scratch and for **both** code
families, that all four polynomials are primitive, that each pair is
*preferred* with its correlations confined to three values meeting the bound
`t(n) = 1 + 2^((n+1)/2)` exactly, that the L seeds give L distinct codes, that
a majority vote over L chips survives exactly (L-1)/2 errors, and that the
lock threshold clears the worst **misaligned** correlation a free-running
receiver can see.

It also proves the pieces the hardware's cheaper tricks depend on: that the
subtractor-free magnitude fold `(m[hb] ? m : ~m) & (half-1)` equals the
arithmetic distance from the midpoint for every reachable match count, that
`|R| = 2·magn + 1`, that the chip-phase discriminator points toward alignment
at every offset, and that a replayed chip stream cannot reach the lock
threshold, which is why the follower jammer fails.

For the coding and framing it proves that SECDED has minimum distance 4,
corrects every single error and flags every double, that interleaving repairs
every 2-bit burst, that a partly received sync word can never match, that the
jammer and hop LFSRs are maximal length, that five steps per clock really do
decorrelate consecutive jammer draws, that the periodic jammer matches the
broadband one's average density, that PRBS-9 has period 511, and that one
injected bit error produces exactly three prediction failures, which is the
calibration the bit error rate readout depends on. It finishes with the
theoretical bit error rate at every jam level for both code lengths.

Pass `--quick` to skip the exhaustive misaligned-window searches, which
dominate the runtime at L = 127.

Then the hardware is compared against that model, never against hand-written
expected values:

```bash
cd test && make -B                 # what CI runs
python test/run_local.py           # same thing, no make needed (Windows)
python test/run_local.py --gl      # against the hardened netlist
```

Thirty-one tests, every one of which also runs against the hardened netlist.

The first ten cover the basic link: reset state, the 248-chip transmit frame
chip-for-chip, a 256-byte loopback sweep, the exact jam cliff at J=15/16,
foreign-code rejection both aligned and free-running, serial-search
acquisition from a wrong phase, the four-tier jammer ladder, control inputs
driven at random sub-cycle offsets, and the output drivers being tri-stated by
`TX_PINS_OFF`.

Eleven more cover coding, framing and hopping: the configuration pages, every
frame format chip-for-chip, loopback in every mode (all 256 bytes with FEC and
framing), SECDED against injected single, double and burst errors, FEC against
the on-chip jammer, a receiver with no epoch wire starting at random chip
offsets, sync-word error tolerance, the hop sequence chip by chip, the
partial-band jammer against parked and hopping transmitters, a page-1-only
reset, and three frames streamed back to back.

The last nine cover the long codes and the instrumentation:

| test | what it pins down |
|---|---|
| `test_long_code_chip_sequence` | 1016 chips of a 127-chip frame against the degree-7 model. Loopback cannot catch a wrong tap, because both sides would use it |
| `test_long_code_loopback` | both lengths through the *same* jammer: 10 bit errors in 256 at L=31, **0** at L=127 |
| `test_long_code_lock_band` | at 34.4% of chips flipped, L=127 decodes perfectly while correctly refusing to claim lock |
| `test_long_code_rejects_foreign` | no false lock over 3048 chips of a foreign long code |
| `test_chip_phase_tracking` | one chip dropped every four frames: 5 of 16 bytes without tracking, **11 of 16** with |
| `test_cross_correlation_monitor` | the monitor reads magnitude **0**, exactly the -1 zero-shift cross-correlation, and never false locks |
| `test_prbs_bert` | the PRBS-9 sequence bit-exact, and the error counter equal to the model's own checker |
| `test_rx_buffer` | depth, ordering and the overflow flag, without assuming which byte arrives first |
| `test_jammer_kinds` | all four kinds against the model, and the follower causing **0** bit errors |

### Clock domain crossing audit

Every external pin that feeds a control decision passes through a two-stage
synchroniser. Three places sample an input pin directly instead:

| site | signal | why it is acceptable |
|---|---|---|
| `cfg_a`, `cfg2a`, `cfg3a` `<= ui_in` | configuration | loaded only while `rst_n` is low, from switches a human sets beforehand |
| `cfg_b`, `cfg2b`, `cfg3b` `<= uio_in[4:0]` | configuration | same |
| `tx_shift <= ui_in` | transmit data | the data byte, static during a frame by construction |

The distinction that matters is that none of those feed a state machine. They
land in data registers, so a sample taken while a switch is moving yields a
wrong value, not an illegal state. The classic fatal crossing bug, where one
asynchronous signal reconverges on two paths and a state machine sees it two
different ways, cannot occur here because every control input is
synchronised. Adding synchronisers to `ui_in` would cost 16 flip-flops and
would not remove the underlying race, which is inherent to sampling a
mechanical switch. On a tile where a flip-flop really costs 31 µm² placed,
that is 500 µm² spent on something that does not fix the problem.

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
| Software model self-check (coding theory proved from scratch, both families) | **pass** |
| cocotb RTL simulation | **31/31** |
| Verilator `--lint-only -Wall` | **0 warnings** (besides the template's file name) |
| **cocotb gate-level simulation** (post-PnR netlist + sky130 cells) | **31/31** |
| LibreLane hardening, 1×2 | **pass**, 75.3% utilisation |
| Static timing, all 9 corners | setup **+0.47 ns**, hold **+0.11 ns**, 0 violations |
| Routing DRC / Magic DRC / antenna / LVS | **0 / 0 / 0 / match uniquely** |
| iCE40UP5K build (Yosys + nextpnr) | **pass** |
| FPGA on real hardware | pending a board |

RTL and gate level run the *same* thirty-one tests with the *same* coverage, which
is the point: a test that only runs at RTL cannot tell you whether the thing
being fabricated works.

### Signoff, 1×2 build (local LibreLane 3.0.14, same version and PDK as CI)

| check | result |
|---|---|
| **LVS (netgen)** | **circuits match uniquely**, 2222 devices and 2248 nets on both sides, 0 errors |
| Magic DRC | **0** errors, full ruleset |
| detailed routing DRC | **0** errors, converged over 5 iterations |
| illegal overlap | **0** |
| antenna violations | **0** nets, **0** diodes inserted |
| setup violations | **0** in all 9 corners |
| hold violations | **0** in all 9 corners |
| max capacitance violations | **0** |
| max fanout violations | 20, every one a clock-tree leaf buffer, not gated |
| max slew violations | **281**, plus 11 at one typical corner, see below |
| IR drop | 0.078 mV worst on 1.8 V |

LVS matching uniquely is the line that matters most. It says the extracted
layout and the synthesised netlist are the same circuit, with no ambiguity in
the correspondence.

### Setup slack across all nine corners

| corner | setup slack (ns) | slew violations |
|---|---|---|
| max_ss_100C_1v60 | **+0.47** | 281 |
| nom_ss_100C_1v60 | +0.65 | 222 |
| min_ss_100C_1v60 | +0.82 | 165 |
| max_tt_025C_1v80 | +10.54 | 11 |
| nom_tt_025C_1v80 | +10.66 | 0 |
| min_tt_025C_1v80 | +10.78 | 0 |
| max_ff_n40C_1v95 | +12.38 | 0 |
| nom_ff_n40C_1v95 | +12.46 | 0 |
| min_ff_n40C_1v95 | +12.54 | 0 |

The typical corners, which are the only ones the flow gates on, close with
about 10.5 ns. The slow corner is where the tight figure lives, and reaching
it needs the worst silicon at 1.60 V and 100 degrees simultaneously while
running five times faster than the part is specified for.

The spread is almost entirely process and supply, not wire extraction: the
slow-slow corner at 1.60 V is 9 ns worse than typical at 1.80 V, while the
pessimistic and optimistic RC extractions of the *same* corner differ by only
0.3 ns. That is the useful lesson about which corner to design against.

### Post-layout results

| metric | value |
|---|---|
| sequential cells | 352 |
| standard cells | 2666: 1344 combinational, 394 timing-repair buffers, 54 clock buffers, 58 inverters, 456 taps |
| physical-only cells | 2779 fill and decap |
| utilisation | 75.3% of the 1×2 tile |
| worst setup slack | +0.47 ns at a 20 ns period (max_ss_100C_1v60) |
| worst hold slack | +0.110 ns (ff corners) |
| total negative slack | **0** for setup and for hold, in all nine corners |
| wirelength | 54 051 µm |
| total power | 1.77 mW |

### How full the tile actually is

Two different questions get called utilisation, and they have different
answers.

**Physically, the tile is 100% occupied, and always was.** The fill insertion
stage places 2740 fill and decoupling cells into every gap, so the final core
area used is 34 255 µm² of a 34 255 µm² core, exactly. That number carries no
information about a design.

**The useful figure is the logic share, and it went from 38.5% to 73.8%.**
That is where the tile now stands, and three runs establish that it is at the
ceiling rather than short of it:

| | post-synthesis | post-CTS | post-resizer | outcome |
|---|---|---|---|---|
| this design | 59.8% | **67.2%** | **75.3%** | routes, 0 DRC |
| an earlier variant | 58.8% | 66.2% | 73.8% | routes, 0 DRC |
| the variant that died | 72.1% | **80.4%** | would need 89.8% | **placement failed** |

The mechanism is the one described below: the post-CTS resizer inflates the
design by about 12% to build hold margin, using eight-site delay cells, and
the legaliser cannot place them if the post-CTS design is already dense. So
the binding number is the **post-CTS** column, and the bracket is tight. 67.2%
is proven to work and 80.4% is proven to fail.

Which means the practical limit is roughly **360 flip-flops on a 1x2 tile**,
and this design uses 352. There is about 600 µm² of headroom, which is some
50 cells or 20 flip-flops. Not nothing, but not a feature either.

Going meaningfully further needs one of two things, and both were considered
and declined:

- **Cut the hold margin.** Frees about 1200 µm². Declined, because a hold
  violation is the only timing failure with no field workaround, for the
  reason given below.
- **Add logic anyway and hope.** Declined, because the failure mode is already
  observed, costs twenty minutes per attempt, and the deadline is 2026-11-29.

So 73.8% is not a design that stopped short. It is the density at which this
flow, on this tile, with this hold margin, places and routes reliably. The
remaining space is doing a job: it is the decoupling that keeps the worst IR
drop at 84 µV.

### A byte arriving on the same clock as a pop

Found by a line-by-line audit of the newest logic, after everything was
already green, and fixed. It is the kind of defect no amount of passing tests
surfaces, because reaching it needs two independent events on one clock edge.

The receive buffer is two registers and a slide. A push requires the count
below two and a pop requires it above zero, so **the two can only ever
coincide at a count of exactly one** and at a count of one only `fifo0` holds
a valid byte. The original code unconditionally slid the tail down:

```verilog
if (fifo_pop) begin
  fifo0 <= fifo1;                    // wrong when fifo1 is not valid
  if (fifo_push) fifo1 <= frame_byte;
end
```

So a simultaneous push and pop promoted a **stale slot to the head** and
parked the arriving byte where the count says nothing is. The next pop
returned a byte that was never transmitted, and the real one arrived a frame
late behind it. A silent duplicate, not a drop.

Writing the test first exposed a second problem, in the test. The first
version filtered observations to payload values, which hid the defect it was
written to find, because a corrupted head reads as the reset value `0x00`.
With the filter removed the failure is unmistakable, and it reproduced at four
of six pop phases.

Fixing that revealed a third thing, a regression in pin behaviour rather than
a data error. On a pop that empties the buffer, sliding the tail down also put
`0x00` on the data port, where the earlier holding-register design kept the
last byte forever. All three cases are now distinct:

| count | event | head becomes |
|---|---|---|
| 2 | pop alone | the tail slides down |
| 1 | pop and push | the arriving byte, since there is no tail |
| 1 | pop alone | unchanged, holding the byte just popped |

`test_rx_buffer_push_pop_same_clock` locks it down. It measures the clock on
which the first byte is delivered, then re-runs placing the pop edge at the
next frame boundary, sweeping the synchroniser delay to be certain of landing
on the coincidence rather than hoping for it.

One behaviour is left as it is and documented rather than changed: a frame
arriving on the same clock as a pop from a **full** buffer is dropped and
flagged, although space was freed that instant. Accepting it would make the
push condition depend on the pop and add a fourth case to the block, and the
payoff is one byte in a rare coincidence. Dropping and flagging is honest,
whereas silently duplicating was not.

### Two flip-flops bought 1.98 ns of setup slack

The first successful 1×2 hardening of the expanded design closed with only
**+0.083 ns** of setup slack. The worst path, read off
`55-openroad-stapostpnr/max_ss_100C_1v60/max.rpt`, ran:

```
frame_en (a config flop) -> tx_data_bit -> tx_chip -> loopback mux
  -> jammer XOR -> rx_chip -> match -> 7-bit accumulator add
  -> magnitude fold -> threshold comparator -> receiver control flop
```

In loopback mode the *entire transmitter* and the *entire receiver* were in
one combinational path, because `tx_chip` is combinational out of the
transmitter's state and the loopback mux fed it straight into the correlator.
Fed from the real pin instead, the receiver starts from a synchroniser output,
so the transmitter half of that path does not exist.

Registering the loopback tap makes loopback behave like the pin it is
pretending to be. Cost: two flip-flops, for `tx_chip_q` and `tx_epoch_q`,
delayed by the same single cycle so their alignment is untouched. Setup slack
went from +0.083 ns to **+2.059 ns**, and wirelength fell from 52 730 µm to
51 481 µm as a side effect. The flop count went from 350 to 352, which is the
cleanest possible confirmation that nothing else changed.

### What actually limits the clock, and two fixes that were reverted

Worth recording because the measurements contradicted sound-looking reasoning
twice.

The worst setup path was chased across four hardening runs, and the start
point moved every time while the slack did not improve:

| run | change | worst path starts at | setup slack |
|---|---|---|---|
| 3 | baseline | an unnamed internal register | +2.059 ns |
| 4 | receive buffer fix | `cfg_a[6]`, the jammer threshold decode | +0.466 ns |
| 5 | three config decodes registered | the same threshold path | +1.278 ns |
| 6 | `jam_hit` registered instead | `loopback_en`, the input multiplexer | +0.098 ns |

Those differences are causal, not noise. Re-running the flow on the reverted
source reproduced the run 4 numbers **exactly**, to four significant figures
on every metric, which establishes that the flow is deterministic for a given
input. So the receive buffer fix really did cost 1.59 ns, the decode registers
really did recover 0.81 ns, and registering `jam_hit` really did lose 0.37 ns.
A design with several paths clustered within half a nanosecond of each other
is genuinely sensitive to small changes, which is a sharper statement than
calling it noisy.

Every one of those paths ends in the same place and is within 0.4 ns of
19.9 ns:

```
match -> 7-bit accumulate -> magnitude fold -> threshold compare -> flop
```

That is the correlator deciding, in the clock that delivered the chip, whether
the chip agreed and whether the window has locked. It is irreducible at one
chip per clock. Both attempted fixes were widening entrances to a corridor
whose length is the constraint, and the second did exactly what it was
designed to do: it removed the jammer from the path and exposed the next
entrance to the same corridor.

**Both timing changes were reverted**, and the deciding argument was routing
robustness rather than slack. Registering the three configuration decodes
genuinely recovered 0.81 ns, but it cost twelve flip-flops, and on this tile a
flip-flop is 31 µm² placed, so routability inflation went from 0.80% to 6.75%
against the 11.4% that accompanied the placement failure described below.
Registering `jam_hit` was one flip-flop and removed more logic, yet measured
0.37 ns worse while also changing observable behaviour.

The trade is asymmetric. Setup is gated only at the typical corners, where
slack is about 11 ns either way, so a thin slow-corner figure does not fail
the build. A routing or placement failure produces no artifact at all. Given a
choice between 0.81 ns of margin at a corner nothing gates and staying far
from the one failure mode already observed on this tile, the second is worth
more.

The real fix is to pipeline the correlator decision, taking the lock and
tracking comparisons from the registered accumulator one clock later. That
ripples into the framing logic and is not a change to make two days before a
deadline on a design that already works.

**Why the thin slack is still fine.** The flow signs off at 20 ns, which is
50 MHz, and this design declares 10 MHz. A 19.9 ns correlator chain means the
part runs correctly to about 50 MHz and is specified at a fifth of that, so
the operating margin is 80 ns on a 100 ns period. The typical corner, which is
the one the flow gates on, closes with about 11 ns. Reaching the 0.1 ns figure
needs the worst silicon at 1.60 V and 100 degrees simultaneously, while
running five times faster than the part is specified for.

### The density limit is hold buffers, not logic

An earlier, larger version of this design failed at
`OpenROAD.ResizerTimingPostCTS` with `[DPL-0036] Detailed placement failed`,
twenty minutes into the run. The cause is not what it looks like.

Both the failing and the passing runs report `[RSZ-0098] No setup violations
found`. The area was consumed entirely by **hold** fixing. The post-CTS
resizer found 239 endpoints with hold violations and inserted **263
`dlygate4sd3_1` delay cells**, each 8 sites wide, to fix a real violation of
about 11 ps. It does that because `PL_RESIZER_HOLD_SLACK_MARGIN = 0.1` asks
it to build 100 ps of margin, and the final hold slack lands at exactly
0.1000 ns, which proves every picosecond was manufactured and none was spare.

The scaling is stable across both runs and is the useful design rule:

| quantity | measured |
|---|---|
| hold-violating endpoints | ≈ 0.68 × flip-flop count |
| hold buffers inserted | ≈ 0.75 × flip-flop count |
| hold-buffer area | ≈ 7.5 µm² per flip-flop |
| clock-buffer area | ≈ 2.1 µm² per flip-flop |
| `dfxtp_2` library area | 21.3 µm² |
| **true placed cost of one flip-flop** | **≈ 31 µm²** |

So a flip-flop costs 45% more than its library cell, and **flip-flops, not
gates, are what fill this tile**. A 1×2 tile holds roughly 360 of them. This
design uses 352.

Why so many hold buffers? Because a design built out of LFSRs, shift
registers and accumulators is mostly *short* paths between flops, and short
paths are exactly what needs hold padding. The 11% of the core spent on delay
cells is a structural consequence of the architecture, not a flow defect.

**The hold margin is deliberately left at the template default of 0.1 ns.**
Cutting it to 0.05 would free about 1200 µm², and that trade was considered
and declined. A hold violation is the one timing failure that cannot be
recovered from after fabrication: setup failures are fixed by slowing the
clock, and hold failures are not, because the hold constraint does not contain
the clock period at all. Running this chip at its declared 10 MHz instead of
the 50 MHz it is signed off at adds **no** hold margin whatsoever. Spending
11% of a tile on insurance against the only unfixable failure mode is the
right purchase.

### Catching that failure nine stages early

Global placement prints three numbers that predict the detailed-placement
failure long before it happens. `tools/harden_local.py` now checks all of them
after every run:

| signal | passing run | failing run | gate |
|---|---|---|---|
| GPL-1004 minimum feasible density | 0.700 | 0.86 | < 0.75 |
| GPL-1007 density needed for 90% free space | 0.789 | 1.07 | < 1.00 |
| GPL-0019 global placement utilisation | 69.7% | 85.6% | < 75% |
| GPL-1012 routability artificial inflation | 1.50% | 11.4% | < 2% |

A GPL-1007 value above 1.0 is arithmetically unplaceable and is the clearest
single signal of the four. The check is in the local tool only, not in
`src/config.json`, so it changes nothing about the submitted flow.

### The clock tree, and where the skew penalty actually comes from

Measured at the worst corner, `max_ss_100C_1v60`:

| | |
|---|---|
| Sinks on `clk` | 352, clustered to 33 |
| Clock buffers built by CTS | 38, plus 14 more for load balancing |
| Insertion delay to the launch flop | 1.259 ns |
| Insertion delay to the capture flop | 1.106 ns |
| **Raw tree skew** | **0.153 ns** |
| Configured clock uncertainty | 0.250 ns |
| Reconvergence pessimism given back | 0.054 ns |
| **Total setup skew charged** | **0.349 ns** |

The useful observation is the split. Only 0.153 ns of that 0.349 ns is the
clock tree. The dominant term is the flat 0.250 ns `clock uncertainty`
allowance, which is a signoff constant rather than a measured property of this
design, and it is 72% of the penalty. So building a shallower or better
balanced tree would recover almost nothing, and effort spent there would be
misdirected. For a single-clock design of 352 flops that allowance is
conservative, but it is Tiny Tapeout's configuration, and the 0.15 ns it
could yield is not worth deviating from a proven flow for.

### Every max-fanout violation is in the clock tree

The build reports 20 max-fanout violations, and all 20 are `clkbuf_leaf_*`
cells driving 11 to 14 flip-flops against a limit of 10. **Not one data net
violates fanout.** The limit is a design-repair default that clock tree
synthesis does not apply to its own tree, because it is clustering sinks
geometrically to minimise skew rather than to satisfy a fanout rule. The thing
that actually matters about those nets is their skew, which is checked
separately and reported above.

### The max-slew violations, and why they are left alone

The build reports 281 max-slew violations at `max_ss_100C_1v60`, 222 at
`nom_ss`, 165 at `min_ss`, and 11 at `max_tt`. The count rose from 149 on the
smaller design, which is expected: more logic on the same tile means longer
average wires.

The 11 at a typical corner are new and worth being precise about rather than
alarmed by. The worst of them is **0.763 ns against the 0.750 ns limit**, so
13 picoseconds over a limit that is itself conservative by a factor of two.

Why they are acceptable:

- The 0.75 ns limit is LibreLane's conservative default, and the violations
  are small. The worst transition anywhere in the design is **1.206 ns**, on a
  net the flow buffered to fix a fanout violation. The sky130 standard cells
  are characterised up to 1.5 ns, so 1.206 ns is inside the characterised
  tables and every delay quoted for those nets is interpolated rather than
  extrapolated. That is read off the report rather than assumed, and it is
  worth re-reading after any change, because the claim only holds while the
  worst case stays under 1.5 ns and nothing in the flow enforces it.
- Timing closes with +2.06 ns of setup slack in that same corner, and the chip
  is declared at 10 MHz, a 100 ns period against the 20 ns it is signed off
  at.
- The flow does not gate on them: `MAX_SLEW_VIOLATION_CORNERS` is `['']`, an
  empty pattern that matches no corner. This is Tiny Tapeout's stock
  configuration, not a local waiver, and it is reported here rather than
  quietly relied on.
- Tiny Tapeout's precheck does not check slew.

Three flow-level fixes were tried and measured on the earlier build before
settling on this: a 40% slew margin, excluding the weak `clkdlybuf4s` cells,
and adding the slow corner to `PNR_CORNERS`. The third produced byte-identical
results, which refutes the natural hypothesis that the slow corner was simply
not being optimised for. None was clean, so `src/config.json` stays at the
template defaults.

### One thing that got worse: on-die decoupling

The smaller design left about 6000 `decap_3` filler cells on the tile. This
one leaves **1791**, because logic and hold buffers took the space. DC supply
integrity is unaffected and measured: worst IR drop is 84 µV on 1.8 V, which
is 0.005%, against an average current of about 0.9 mA.

What is reduced is the local charge reservoir that absorbs the di/dt of 352
flip-flops switching together, and supply ripple is not modelled in static
timing at all. At the declared 10 MHz, five times slower than signoff, this is
not a concern. It is recorded because it is the one power-integrity figure
that moved in the wrong direction, and a reader comparing the two builds
deserves to see it rather than discover it.

### What the flow does not check

Worth stating plainly, since a wall of zeros invites the wrong conclusion:

- **KLayout DRC and the Magic-versus-KLayout XOR are both disabled** by
  `RUN_KLAYOUT_DRC: 0` and `RUN_KLAYOUT_XOR: 0` in `src/config.json`. That
  file is the unmodified Tiny Tapeout template, and both lines sit below its
  "DO NOT CHANGE ANYTHING BELOW THIS POINT" banner. So Magic is the sole DRC
  authority and the two GDS writers were never proven geometrically
  equivalent. Every Tiny Tapeout project ships this way and the shuttle runs
  its own precheck on submission, so deviating from the proven template three
  days before the deadline would add risk, not assurance.
- The manufacturability report's green DRC tick covers Magic only, and the
  flow says so: `klayout__drc_error__count not reported`.
- IR drop is computed without `VSRC_LOC_FILES`, which the flow warns about.
  For a Tiny Tapeout tile the harness owns top-level power, so the 84 µV
  figure should be read as "nowhere near a problem" rather than as a rigorous
  number.
- The long-wire checker is inert: `ERROR_ON_LONG_WIRE` is true but
  `WIRE_LENGTH_THRESHOLD` is unset, so it is skipped.

## Remaining work

- [x] `test`, `gds`, `precheck`, `gl_test`, `fpga`, `docs` and `viewer` green in CI
- [x] 1×2 features: FEC, sync-word framing, frequency hopping, partial-band jammer
- [x] 127-chip Gold family, with its lock threshold derived exhaustively before any RTL was written
- [x] Chip-phase tracking, so two boards with their own crystals keep a link
- [x] Built-in PRBS-9 bit error rate tester, correlation-magnitude meter and foreign-code monitor
- [x] Four jammer kinds including the follower, the matched attack
- [x] Fill the tile: 73.8% utilisation, 352 of a practical ~360 flip-flops
- [x] Close the slow corner: setup slack +0.083 ns → +2.06 ns for two flip-flops
- [x] Gate-level simulation of all thirty tests against the hardened netlist
- [ ] Load the bitstream on an iCE40 board and run the loopback + jammer demo
      (`tools/fpga_local.py` already builds it, and this is the step that
      exercises genuinely asynchronous inputs)
- [ ] Submit in the Tiny Tapeout app **as 1×2**, confirm the coupon covers two tiles, resubmit final commit
- [ ] Optional: SymbiYosys formal properties

## Licence

Apache-2.0, see [LICENSE](LICENSE).
