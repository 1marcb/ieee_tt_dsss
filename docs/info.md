<!---

This file is used to generate your project datasheet. Please fill in the information below and delete any unused
sections.

You can also include images in this folder and reference them in the markdown. Each image must be less than
512 kb in size, and the combined size of all images must be less than 1 MB.
-->

## How it works

JAMGUARD is a complete direct-sequence spread-spectrum (DSSS) modem in two
tiles: a transmitter that smears each data bit across 31 chips, a correlating
receiver that puts it back together, and a jammer so you can attack your own
link on the bench without any extra equipment. Three optional layers sit on
top of the basic link, and each one can be switched on or off by itself:

- **FEC**: every byte is sent as two interleaved SECDED(8,4) Hamming
  codewords, so a single bad bit per codeword is corrected and two are
  detected.
- **Framing**: a sync word in front of every frame lets the receiver find byte
  boundaries by itself. Together with the self-acquiring correlator, the
  receiver then needs nothing but the chip stream, with no epoch wire.
- **Frequency hopping**: a hop sequence derived from the code ID, with a
  partial-band jammer so you can watch hopping escape a narrowband attack.

The full chain is: encode → frame → spread → (hop) → jam → despread → find
sync → correct.

### Spreading

Each data bit is multiplied by a 31-chip **Gold code**. In logic terms the
transmitted chip is

```
chip[k] = code[k] XOR data_bit        k = 0 .. 30
```

so a `0` sends the code and a `1` sends its inverse. One code period covers
exactly one data bit, which makes the bit rate `f_clk / 31`.

The code comes from two 5-bit maximal-length LFSRs XORed together:

| LFSR | polynomial | taps |
|------|-----------------------------------|------------------------|
| A | x^5 + x^2 + 1 | `a[4] ^ a[1]` |
| B | x^5 + x^4 + x^3 + x^2 + 1 | `b[4]^b[3]^b[2]^b[1]` |

These two are a **preferred pair**, which is the property that makes the 31
codes usable simultaneously: their cross-correlation only ever takes the values
`{-9, -1, +7}` out of a possible ±31. LFSR-A always starts from seed `1`.
`CODE_ID` is the seed of LFSR-B, and the 31 non-zero seeds give 31 distinct
codes. `CODE_ID` is therefore the shared secret of the link.

### Despreading

The receiver runs its own copy of the generator and counts how many of the 31
chips agreed:

```
M = number of chips where received == local code      0 <= M <= 31
R = 2M - 31                                           signed correlation
```

Two independent decisions come out of `M`:

- **data**: the bit is `1` when `M < 16`. Because `M` is a 5-bit number and 16
  is `2^4`, this test is literally `~M[4]`, so the threshold comparator costs
  nothing.
- **lock**: the link is believed when `|R| >= 19`, i.e. `M >= 25` or `M <= 6`.

The data decision is a majority vote over 31 chips, so it survives **up to 15
corrupted chips out of 31 (48%)**. That margin is the processing gain,
`10*log10(31) = 14.9 dB`.

The lock test is deliberately much stricter than the data test. An impostor on
a different `CODE_ID` produces `M` in `{11,12,15,16,19,20}`, and a receiver that
has not yet found the chip phase also sees windows straddling two data bits,
whose partial correlations reach `|R| = 17`. A threshold of 19 rejects both. A
2-bit confidence counter adds hysteresis so a single jammed window does not drop
an established lock.

### Acquisition

With `ACQ_EN = 1` the receiver needs no epoch strobe. It correlates
continuously, and every window that fails the lock test causes it to skip one
incoming chip, walking its local phase by one. All 31 phases are covered in at
most about 1000 chips, and nothing is delivered until lock is declared.

With `ACQ_EN = 0` the receiver is slaved to `RX_EPOCH` and always accumulates,
even unlocked. That is the lab mode: it lets you measure raw bit error rate past
the point where a real receiver would have given up.

### Framing

Acquisition finds the **chip** phase, but not where a byte starts. Without
framing, a receiver that acquires on its own starts counting bits wherever it
locked, so the bytes come out rotated.

With `FRAME_EN = 1` every frame starts with the 8-bit sync word `0xE4` (the
7-bit Barker code `1110010` followed by a `0`). The receiver searches the
decided bit stream for it, accepting at most one bit error, and only then
counts payload bits. The sync word does not overlap itself, and the hunting
register is cleared to zero after each frame, so a partly received sync word
can never match. The model proves both properties, and the tests confirm the
receiver recovers every byte from a stream.

### Forward error correction

With `FEC_EN = 1` each byte is split into two nibbles, and each nibble becomes
an 8-bit extended Hamming codeword:

```
codeword = {d3, d2, d1, d0, p2, p1, p0, q}
p0 = d0^d1^d3    p1 = d0^d2^d3    p2 = d1^d2^d3    q = parity of the other 7
```

The minimum distance is 4, so each codeword corrects any single error and
detects any double error. The two codewords are bit-interleaved, high codeword
on odd positions and low on even, so a burst of two adjacent bad bits lands as
one correctable error in each. A frame is 16 coded bits, or 24 with the sync
word.

The FEC sits *after* despreading, so it fixes the bits the 31-chip majority
vote got wrong. That matters when the jammer pushes the chip error rate close
to 50%:

| jam level | P(flip) | raw byte errors | byte errors with FEC |
|-----------|---------|-----------------|-----------------------|
| fine 0 | 25.0% | 1.0% | 0.01% |
| fine 1 | 28.1% | 3.8% | 0.13% |
| fine 2 | 31.3% | 11% | 1.1% |
| fine 3 | 37.5% | 47% | 23% |

(Theory for independent chip flips, printed by `test/jamguard_model.py`. In
simulation at fine level 2, 40 random bytes: 4 wrong without FEC, 1 wrong with
FEC, 8 codewords corrected.)

### Frequency hopping

With `FHSS_EN = 1` the transmitter carries a channel number, `1..63`, from a
6-bit maximal LFSR (`x^6 + x^5 + 1`) seeded with `{CODE_ID, 1}`. It hops once
per data bit, or once per chip with `HOP_FAST = 1`. The sequence is seeded at
reset and carries on from frame to frame. There is no RF on the chip, so the
channel number is shown on status page 2, where an external synthesizer, or
you, can follow it.

What makes it measurable is the **partial-band jammer** (`PBJ_EN = 1`): the
on-chip jammer then only corrupts chips sent on channels inside its band
(`PBJ_BAND`: channels 1–7, 1–15, 1–31 or 1–47). With hopping off the
transmitter parks on channel 1, inside every band, which models a jammer that
has found a fixed-frequency link. Simulation at the strongest level (50%
flips) and the narrowest band, over 16 random bytes:

| transmitter | bit error rate | bytes wrong |
|-------------|----------------|-------------|
| parked on channel 1 | 55% | 16 / 16 |
| hop once per bit | 7.8% | 7 / 16 |
| hop once per bit, FEC on | 0% | 0 / 16 |
| hop once per chip | 0% | 0 / 16 |

Hopping per bit means whole bits land in the band and are lost, and FEC
repairs them. Hopping per chip spreads the damage across every bit, about 6%
of chips each, and the 31-chip majority vote absorbs it without FEC at all.

### Jammer

A 17-bit LFSR (`x^17 + x^14 + 1`) advances five steps every clock, so every
chip gets five fresh pseudo-random bits, and a chip is flipped when those fall
below a threshold. Chip flips are therefore independent, the textbook binary
symmetric channel. The coarse levels sit in the four regimes of the link, and
the fine levels (`JAM_FINE = 1`) straddle the decode cliff, where FEC matters:

| JAM_LVL | P(flip) | mean flips / 31 | behaviour |
|---------|---------|-----------------|-----------------------------|
| 0 | 6.3% | 1.9 | locks, no errors |
| 1 | 12.5% | 3.9 | locks, no errors |
| 2 | 34.4% | 10.7 | **lock lost, still mostly decodes** |
| 3 | 50.0% | 15.5 | link breaks |
| fine 0 | 25.0% | 7.8 | lock lost, raw BER 0.1% |
| fine 1 | 28.1% | 8.7 | raw BER 0.5% |
| fine 2 | 31.3% | 9.7 | raw BER 1.4% |
| fine 3 | 37.5% | 11.6 | raw BER 7.7% |

## How to test

### Configuration is latched during reset

While `rst_n` is low the chip continuously samples the pins, and whatever is
present when reset releases becomes the configuration. `uio[5]` selects which
of two pages the pins are writing. **Set page 0, then optionally page 1, then
release reset**, holding each page for at least two clocks.

Writing page 0 also clears page 1. A plain reset with `uio[5]` low therefore
always gives the basic DSSS link with every extra feature off.

**Page 0** (`uio[5] = 0`)

| pin | field | meaning |
|-----------------|--------------|-----------------------------------------|
| `ui_in[4:0]` | `CODE_ID` | transmitter code, 1..31 (0 becomes 1) |
| `ui_in[6:5]` | `JAM_LVL` | jammer strength 0..3 |
| `ui_in[7]` | `ACQ_EN` | 1 = self-acquiring, 0 = epoch-slaved |
| `uio_in[4:0]` | `RX_CODE_ID` | receiver code, **0 means "same as TX"** |

**Page 1** (`uio[5] = 1`)

| pin | field | meaning |
|-----------------|--------------|-----------------------------------------|
| `ui_in[0]` | `FEC_EN` | SECDED(8,4) × 2 on every byte |
| `ui_in[1]` | `FRAME_EN` | sync word in front of every frame |
| `ui_in[2]` | `FHSS_EN` | frequency hopping on |
| `ui_in[3]` | `PBJ_EN` | jammer is partial-band, not broadband |
| `ui_in[4]` | `JAM_FINE` | use the fine jam ladder |
| `ui_in[6:5]` | `DISP_PAGE` | status page shown when `DISP_SEL = 1` |
| `ui_in[7]` | `TX_PINS_OFF` | 1 = tri-state `uio[7:6]`, see demo 7 |
| `uio_in[0]` | `HOP_FAST` | 0 = hop per bit, 1 = hop per chip |
| `uio_in[2:1]` | `PBJ_BAND` | jammed channels 1–7, 1–15, 1–31, 1–47 |

The transmitter and the receiver of a link must agree on `FEC_EN` and
`FRAME_EN`.

### Run mode

| pin | function |
|---|---|
| `ui_in[7:0]` | byte to transmit, sampled when a frame starts |
| `uo_out[7:0]` | received byte (`DISP_SEL = 0`) or status page (`DISP_SEL = 1`) |
| `uio[0]` in | `TX_START`: rising edge sends one byte, hold high to stream |
| `uio[1]` in | `RX_CHIP`: received chip stream |
| `uio[2]` in | `RX_EPOCH`: strobe on chip 0 of each code period |
| `uio[3]` in | `LOOPBACK_EN`: transmitter wired into receiver on chip |
| `uio[4]` in | `JAM_EN`: on-chip jammer on |
| `uio[5]` in | `DISP_SEL` |
| `uio[6]` out | `TX_CHIP`: transmitted chip stream |
| `uio[7]` out | `TX_EPOCH`: strobe on chip 0 of each code period |

### Status pages

| `DISP_PAGE` | bit 7 | bit 6 | bit 5 | bit 4 | bits 3–2 | bits 1–0 |
|---|---|---|---|---|---|---|
| 0 | VALID | LOCKED | HUNTING | CORR[4] | CORR[3:2] | CORR[1:0] |
| 1 | VALID | LOCKED | IN_FRAME | FEC_ERR | FEC_FIX[1:0] | FRAMES[1:0] |
| 2 | TX_BUSY | JAM_NOW | CH[5] | CH[4] | CH[3:2] | CH[1:0] |
| 3 | raw received byte, before error correction | | | | | |

- `CORR` is the match count `M` of the last 31-chip window: 31 or 0 is a
  perfect correlation.
- `FEC_FIX` has one bit per codeword (high, low) that needed a correction in
  the last byte. `FEC_ERR` means a codeword had two errors and could not be
  corrected.
- `FRAMES` counts delivered frames modulo 4, so a host polling the pins can
  tell when a new byte has arrived.
- `CH` is the transmitter's current hop channel, and `JAM_NOW` is high on
  chips the jammer is flipping.

### Demo 1: loopback, one board

1. Reset with page 0 only: `CODE_ID = 7`, `JAM_LVL = 0`, `ACQ_EN = 0`,
   `RX_CODE_ID = 0`.
2. Set `LOOPBACK_EN = 1`, `JAM_EN = 0`, `DISP_SEL = 0`.
3. Put a byte on `ui_in[7:0]` and pulse `TX_START`.
4. About 248 clocks later the same byte appears on `uo_out`. With
   `DISP_SEL = 1` the status byte shows `VALID = 1`, `LOCKED = 1`,
   `CORR = 31`, or 0 because a `1` data bit inverts the code.

### Demo 2: the jammer

Repeat demo 1 with `JAM_EN = 1`, resetting with each `JAM_LVL` in turn. Levels
0 and 1 are error-free with `LOCKED = 1`. Level 2 loses lock but most of the
data still comes through. Level 3 breaks it. Watch `CORR` fall as the jammer
gets stronger: you are watching the processing gain being spent.

### Demo 3: foreign code rejection

Reset with `CODE_ID = 7` but `RX_CODE_ID = 19`, and `ACQ_EN = 1`. Run demo 1.
`LOCKED` never asserts, `VALID` never asserts, and `uo_out` stays at `0x00`.
Set `RX_CODE_ID` back to 0 and it works again.

### Demo 4: FEC against the jammer

Reset with page 0 `JAM_LVL = 2`, `ACQ_EN = 0`, and page 1 `JAM_FINE = 1`,
`DISP_PAGE = 1`. With `LOOPBACK_EN = 1` and `JAM_EN = 1`, send a series of
bytes: some arrive wrong. Reset again with `FEC_EN = 1` added. Now `FEC_FIX`
flickers on as codewords are corrected and the bytes arrive right. With
`DISP_PAGE = 3` instead, `DISP_SEL` flips between the raw byte and the
corrected byte, so you can see the bad bits being repaired.

### Demo 5: a receiver that needs nothing but the chips

Reset with `ACQ_EN = 1` in page 0 and `FRAME_EN = 1` (optionally `FEC_EN = 1`)
in page 1. Run loopback with `TX_START` held high to stream. The receiver finds
the chip phase by serial search and the byte boundaries from the sync word.
`HUNTING` (page 0) clears when it locks and `IN_FRAME` (page 1) pulses as frames
arrive. Across two boards this needs only the `TX_CHIP` wire, a shared clock
and ground: no epoch wire.

### Demo 6: frequency hopping against a narrowband jammer

Reset with page 0 `JAM_LVL = 3`, `ACQ_EN = 0`, and page 1 `PBJ_EN = 1`,
`PBJ_BAND = 0`, `DISP_PAGE = 2`. With `JAM_EN = 1` and loopback on, the
parked transmitter (`FHSS_EN = 0`) is wiped out. Add `FHSS_EN = 1` and most
bytes get through. Add `FEC_EN = 1`, or `HOP_FAST = 1`, and they all do. With
`DISP_SEL = 1` and a slow clock, page 2 shows the channel walking through the
hop sequence and `JAM_NOW` lighting up when it lands in the jammed band.

### Demo 7: two boards

Both chips run the same design, so by default both drive `uio[6]` and
`uio[7]`. Wiring two boards together carelessly can put one output driver
against another, with only their on resistance limiting the current.

**Set `TX_PINS_OFF` (page 1, `ui_in[7]`) on the receiving board.** That board
then tri-states `uio[7:6]` for the whole run, `uio_oe` reads `0x00`, and it
drives nothing on the bus. It still receives normally and still runs loopback
internally.

Use individual jumper wires rather than a ribbon. On the demo board the `uio`
pins are on a PMOD header and may also be driven by the on-board
microcontroller.

| board A | | board B |
|---|---|---|
| `uio[6]` TX_CHIP (out) | to | `uio[1]` RX_CHIP (in) |
| `uio[7]` TX_EPOCH (out) | to | `uio[2]` RX_EPOCH (in), optional with `FRAME_EN` + `ACQ_EN` |
| `clk` | shared | `clk` |
| `gnd` | shared | `gnd` |

**The clock must be shared.** The receiver samples one chip per clock and has
no clock recovery. Two free-running oscillators 100 ppm apart would slip a chip
every ten thousand, so the link would work for a while and then fail
periodically.

Give both the same `CODE_ID` and the same `FEC_EN` / `FRAME_EN`, and bytes set
on A appear on B. Change B's `RX_CODE_ID` and the link goes dead.

### Simulation

```
cd test
make -B                   # RTL, 19 tests
python jamguard_model.py  # proves the coding-theory claims on their own
```

`test/jamguard_model.py` is an independent software model, and the cocotb tests
compare the hardware against it rather than against hand-written expected
values. It also proves the preferred-pair property, the 15-of-31 jam margin,
that the lock threshold clears the worst misaligned correlation, that SECDED
corrects every single error and flags every double, that interleaving fixes
every 2-bit burst, and that a partly received sync word can never match.

## External hardware

None required. The DIP switches, 7-segment display and PMOD headers on the
standard Tiny Tapeout demo board are enough for every demo except the
two-board link, which needs one or two jumper wires plus a shared clock and
ground.
