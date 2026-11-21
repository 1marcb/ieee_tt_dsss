<!---

This file is used to generate your project datasheet. Please fill in the information below and delete any unused
sections.

You can also include images in this folder and reference them in the markdown. Each image must be less than
512 kb in size, and the combined size of all images must be less than 1 MB.
-->

## How it works

JAMGUARD is a complete direct-sequence spread-spectrum (DSSS) modem in one tile:
a transmitter that smears each data bit across 31 chips, a correlating receiver
that puts it back together, and a jammer so you can attack your own link on the
bench without any extra equipment.

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
`code_id` is the seed of LFSR-B, and the 31 non-zero seeds give 31 distinct
codes. `code_id` is therefore the shared secret of the link.

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
a different `code_id` produces `M` in `{11,12,15,16,19,20}`, and a receiver that
has not yet found the chip phase also sees windows straddling two data bits,
whose partial correlations reach `|R| = 17`. A threshold of 19 rejects both. A
2-bit confidence counter adds hysteresis so a single jammed window does not drop
an established lock.

### Acquisition

With `ACQ_EN = 1` the receiver needs no epoch strobe. It correlates
continuously, and every window that fails the lock test causes it to skip one
incoming chip, walking its local phase by one. All 31 phases are covered in
bounded time, and nothing is delivered until lock is declared.

With `ACQ_EN = 0` the receiver is slaved to `RX_EPOCH` and always accumulates,
even unlocked. That is the lab mode: it lets you measure raw bit error rate past
the point where a real receiver would have given up.

### Jammer

An 8-bit LFSR (`x^8 + x^6 + x^5 + x^4 + 1`, period 255) runs every clock and
flips a chip whenever its low 5 bits fall below a threshold. Since
`gcd(255, 31) = 1` the jam pattern never aligns with the code period. The four
levels are placed in the link's four distinct regimes:

| JAM_LVL | P(flip) | mean flips / 31 | mean M | behaviour |
|---------|---------|-----------------|--------|-----------------------------|
| 0 | 6.3% | 1.9 | 29.1 | locks, no errors |
| 1 | 12.5% | 3.9 | 27.1 | locks, no errors |
| 2 | 34.4% | 10.7 | 20.3 | **lock lost, still decodes** |
| 3 | 50.0% | 15.5 | 15.5 | link breaks |

Level 2 is the interesting one: it is past the lock threshold but still inside
the decode threshold, which is exactly the gap between "I can read this" and "I
can prove who sent it".

## How to test

### Configuration is latched during reset

While `rst_n` is low the chip continuously samples the pins. Whatever is present
when reset releases becomes the configuration, so **set the switches first, then
release reset**:

| pin | field | meaning |
|-----------------|--------------|-----------------------------------------|
| `ui_in[4:0]` | `CODE_ID` | transmitter code, 1..31 (0 becomes 1) |
| `ui_in[6:5]` | `JAM_LVL` | jammer strength 0..3 |
| `ui_in[7]` | `ACQ_EN` | 1 = self-acquiring, 0 = epoch-slaved |
| `uio_in[4:0]` | `RX_CODE_ID` | receiver code, **0 means "same as TX"** |
| `uio_in[5]` | reserved | tie low |

After reset the pins take on their run-mode meanings (see the pinout table).

### Demo 1: loopback, one board

1. Reset with `CODE_ID = 7`, `JAM_LVL = 0`, `ACQ_EN = 0`, `RX_CODE_ID = 0`.
2. Set `LOOPBACK_EN = 1`, `JAM_EN = 0`, `DISP_SEL = 0`.
3. Put a byte on `ui_in[7:0]` and pulse `TX_START`.
4. About 248 clocks later the same byte appears on `uo_out`, and with
   `DISP_SEL = 1` the status byte shows `VALID = 1`, `LOCKED = 1`, `CORR = 31`
   (or 0, because a `1` data bit inverts the code, giving a perfect negative
   correlation).

Hold `TX_START` high to stream continuously.

### Demo 2: the jammer

Repeat demo 1 with `JAM_EN = 1`, resetting with each `JAM_LVL` in turn. Levels 0
and 1 are error-free with `LOCKED = 1`. Level 2 loses lock but the data still
comes through. Level 3 breaks it. Watch `CORR` on the status byte fall as the
jammer gets stronger. You are watching the processing gain being spent.

### Demo 3: foreign code rejection

Reset with `CODE_ID = 7` but `RX_CODE_ID = 19`, and `ACQ_EN = 1`. Run demo 1.
`LOCKED` never asserts, `VALID` never asserts, and `uo_out` stays at `0x00`. The
transmitter is right there in loopback and the receiver still refuses it,
because it does not have the code. Set `RX_CODE_ID` back to 0 and it works
again.

### Demo 4: acquisition

With `ACQ_EN = 1` and `LOOPBACK_EN = 1`, note that the link comes up with no
`RX_EPOCH` connected at all. `HUNTING` (status bit 5) is high while the
correlator is searching and clears when it locks.

### Demo 5: two boards

Wire board A's `TX_CHIP` (`uio[6]`) to board B's `RX_CHIP` (`uio[1]`), and
`TX_EPOCH` (`uio[7]`) to `RX_EPOCH` (`uio[2]`). **Share `clk` and `gnd`**: one
chip is transmitted per clock, so the two boards must be clocked together.
Give both the same `CODE_ID` and bytes typed on A appear on B. Change B's
`RX_CODE_ID` and the link goes dead.

If you only wire `TX_CHIP` and set `ACQ_EN = 1` on the receiver, it will find
the phase by itself, but byte framing then starts wherever acquisition
happened, so the received bytes are rotated. Byte framing needs either the epoch
wire or a preamble, which is a layer this chip deliberately leaves out.

### Simulation

```
cd test
make -B                   # RTL
python jamguard_model.py  # proves the coding-theory claims on their own
```

`test/jamguard_model.py` is an independent software model, and the cocotb tests
compare the hardware against it rather than against hand-written expected
values. It also verifies the preferred-pair property, the 15-of-31 jam margin,
and that the lock threshold clears the worst misaligned correlation.

## External hardware

None required. The DIP switches, 7-segment display and PMOD headers on the
standard Tiny Tapeout demo board are enough for every demo except the
two-board link, which needs three jumper wires plus a shared clock and ground.
