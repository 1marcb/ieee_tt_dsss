# SPDX-FileCopyrightText: (c) 2026 Marc Boudames
# SPDX-License-Identifier: Apache-2.0
"""
cocotb test suite for tt_um_1marcb_jamguard.

Every assertion compares the DUT against jamguard_model.py, never against a
second copy of the same arithmetic written out by hand.

These tests touch *only* top-level pins.  Internal signal names do not survive
synthesis, so probing them would pass at RTL and fail at gate level.  Waits are
expressed in clock cycles rather than absolute time for the same reason.
"""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, Timer

from jamguard_model import (
    L,
    LONG,
    SHORT,
    JAM_BROADBAND,
    JAM_FOLLOWER,
    JAM_PERIODIC,
    JAM_PULSED,
    JAM_THRESH,
    LOCK_HI,
    LOCK_LO,
    PARK_CH,
    FrameReceiver,
    despread_bits,
    despread_byte,
    fec_decode_payload,
    fec_encode_byte,
    frame_bits,
    gold_code,
    hop_channels,
    in_band,
    Jammer,
    prbs_bytes,
    prbs_check,
    spread_bits,
    spread_byte,
    spread_frame,
)

CLK_NS = 100  # 10 MHz, the clock rate declared in info.yaml

# uio bit assignments, mirroring the header of src/project.v
UIO_BITS = {
    "tx_start": 0,
    "rx_chip": 1,
    "rx_epoch": 2,
    "loopback": 3,
    "jam_en": 4,
    "disp_sel": 5,
}

# uio outputs driven by the DUT
TX_CHIP_BIT = 6
TX_EPOCH_BIT = 7


class Uio:
    """Keeps a shadow copy of uio_in so individual bits can be poked."""

    def __init__(self, dut):
        self.dut = dut
        self.val = 0
        self.dut.uio_in.value = 0

    def set(self, **kwargs):
        for name, level in kwargs.items():
            bit = 1 << UIO_BITS[name]
            self.val = (self.val | bit) if level else (self.val & ~bit & 0xFF)
        self.dut.uio_in.value = self.val


def tx_chip(dut):
    return (int(dut.uio_out.value) >> TX_CHIP_BIT) & 1


def tx_epoch(dut):
    return (int(dut.uio_out.value) >> TX_EPOCH_BIT) & 1


def status_fields(byte_val):
    """Unpack the status byte: {valid, locked, hunting, m_last[4:0]}."""
    return {
        "valid": (byte_val >> 7) & 1,
        "locked": (byte_val >> 6) & 1,
        "hunting": (byte_val >> 5) & 1,
        "m_last": byte_val & 0x1F,
    }


def start_clock(dut):
    """Start the clock exactly once per test."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())


def mode_byte(fec=False, framed=False, fhss=False, pbj=False, jam_fine=False,
              disp_page=0, tx_pins_off=False):
    """Page 1 ui_in value, see the header of src/project.v."""
    return (int(fec) | int(framed) << 1 | int(fhss) << 2 | int(pbj) << 3
            | int(jam_fine) << 4 | (disp_page & 3) << 5 | int(tx_pins_off) << 7)


async def reset(dut, tx_code, jam_level=0, acq=False, rx_code=0,
                tx_pins_off=False, fec=False, framed=False, fhss=False,
                pbj=False, disp_page=0, hop_fast=False, pbj_band=0,
                page1=None, len127=False, trk=False, prbs=False,
                rx_pop=False, mon=False, jam_kind=0, fol_delay=0,
                pulse_duty=0):
    """Reset the DUT and latch a configuration off the pins.

    The configuration flops track the pins the whole time rst_n is low, so
    the value present when reset releases is what gets kept.  uio_in[5]
    selects page 0 or the extension group, and on an extension page
    uio_in[4:3] picks which one.  Page 0 (code IDs, jam level, acquisition)
    is written first, which also clears every extension page, then each
    extension page is written only if any of its fields are non-zero.

    jam_level 0..3 is the coarse ladder, 4..7 the fine one (sets jam_fine).
    page1=True forces a page-1 write even when every mode field is zero.

    tx_code and rx_code are up to 7 bits.  Their low five bits ride on page
    0 exactly as they always have, and the high two go on page 2, so a
    configuration that only uses the 31-chip codes writes nothing to page 2
    and is byte for byte the configuration this design shipped with.
    """
    uio = Uio(dut)
    dut.ena.value = 1
    dut.rst_n.value = 0

    # Page 0
    dut.ui_in.value = (
        ((1 if acq else 0) << 7) | ((jam_level & 3) << 5) | (tx_code & 0x1F)
    )
    uio.val = rx_code & 0x1F
    dut.uio_in.value = uio.val
    await ClockCycles(dut.clk, 6)

    async def write_page(sub, ui_val, uio_low):
        dut.ui_in.value = ui_val
        uio.val = (1 << 5) | (sub << 3) | uio_low
        dut.uio_in.value = uio.val
        await ClockCycles(dut.clk, 4)

    # Page 1, the mode byte
    mb = mode_byte(fec, framed, fhss, pbj, jam_level >= 4, disp_page,
                   tx_pins_off)
    m2 = int(hop_fast) | (pbj_band & 3) << 1
    if page1 or (page1 is None and (mb or m2)):
        await write_page(0, mb, m2)

    # Page 2, long codes, tracking, pattern generator and the receive buffer
    p2a = ((tx_code >> 5) & 3) | ((rx_code >> 5) & 3) << 2 \
        | int(len127) << 4 | int(trk) << 5 | int(prbs) << 6 \
        | int(rx_pop) << 7
    p2b = int(mon) | ((disp_page >> 2) & 1) << 2
    if p2a or p2b:
        await write_page(1, p2a, p2b)

    # Page 3, the extended jammer
    p3a = (jam_kind & 3) | (fol_delay & 3) << 2 | (pulse_duty & 3) << 5 \
        | ((disp_page >> 3) & 1) << 7
    if p3a:
        await write_page(2, p3a, 0)

    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    # Clear the config byte off ui_in and drop the rx_code bits off uio_in so
    # the run-mode meaning of those pins takes over.
    dut.ui_in.value = 0
    uio.val = 0
    dut.uio_in.value = 0

    # Let the two-stage input synchronisers settle.
    await ClockCycles(dut.clk, 3)
    await FallingEdge(dut.clk)
    return uio


def frame_len(fec=False, framed=False):
    """Bits per frame, header included."""
    return (16 if fec else 8) + (8 if framed else 0)


async def send_byte(dut, uio, value, slack=12, nbits=8, big_l=L):
    """Pulse tx_start, then wait out the whole nbits x big_l chip frame."""
    dut.ui_in.value = value
    uio.set(tx_start=1)
    await FallingEdge(dut.clk)
    await FallingEdge(dut.clk)
    uio.set(tx_start=0)
    await ClockCycles(dut.clk, nbits * big_l + slack)
    await FallingEdge(dut.clk)


def page1_fields(byte_val):
    """Unpack status page 1: {valid, locked, in_frame, fec_err, fix[1:0], frames[1:0]}."""
    return {
        "valid": (byte_val >> 7) & 1,
        "locked": (byte_val >> 6) & 1,
        "in_frame": (byte_val >> 5) & 1,
        "fec_err": (byte_val >> 4) & 1,
        "fec_fix": (byte_val >> 2) & 3,
        "frames": byte_val & 3,
    }


async def read_status(dut, uio):
    """Flip DISP_SEL to the status page, read it, flip back."""
    uio.set(disp_sel=1)
    await ClockCycles(dut.clk, 3)
    await FallingEdge(dut.clk)
    val = int(dut.uo_out.value)
    uio.set(disp_sel=0)
    await ClockCycles(dut.clk, 3)
    await FallingEdge(dut.clk)
    return val


async def drive_chips(dut, uio, chips, epoch=True, watch=None, big_l=L):
    """Feed a chip stream in on rx_chip_in, one chip per clock.

    rx_chip_in and rx_epoch_in pass through synchronisers of identical depth,
    so the two arrive at the correlator still aligned and no compensation is
    needed here.  `watch` is called with (index, status_byte) every clock.

    The loop body is "drive, then wait for the next falling edge", which only
    presents each chip to exactly one rising edge if we enter it just after a
    falling edge.  This leading wait establishes that position from either
    starting phase. Without it the first chip of the stream is overwritten
    before any rising edge samples it, and the whole stream shifts by one.
    """
    await FallingEdge(dut.clk)
    for i, c in enumerate(chips):
        uio.set(rx_chip=c, rx_epoch=1 if (epoch and i % big_l == 0) else 0)
        await FallingEdge(dut.clk)
        if watch is not None:
            watch(i, int(dut.uo_out.value))
    uio.set(rx_chip=0, rx_epoch=0)


# ---------------------------------------------------------------------------
# 1. Reset and configuration
# ---------------------------------------------------------------------------

@cocotb.test()
async def test_reset_and_config(dut):
    """After reset the receiver must be idle, unlocked and showing zeros."""
    start_clock(dut)
    uio = await reset(dut, tx_code=7)

    assert int(dut.uo_out.value) == 0, "rx_data should be 0 out of reset"
    assert int(dut.uio_oe.value) == 0b1100_0000, (
        f"uio_oe must be 0b11000000, got {int(dut.uio_oe.value):#010b}"
    )
    assert tx_chip(dut) == 0 and tx_epoch(dut) == 0, "transmitter must be idle"

    uio.set(disp_sel=1)
    await ClockCycles(dut.clk, 3)
    st = status_fields(int(dut.uo_out.value))
    assert st["locked"] == 0 and st["valid"] == 0 and st["m_last"] == 0, st
    dut._log.info("reset state clean: %s", st)


# ---------------------------------------------------------------------------
# 2. Transmitter chip sequence
# ---------------------------------------------------------------------------

@cocotb.test()
async def test_tx_chip_sequence(dut):
    """The emitted 248-chip frame must equal the model's, chip for chip."""
    code_id = 7
    data = 0xA5

    start_clock(dut)
    uio = await reset(dut, tx_code=code_id)

    dut.ui_in.value = data
    uio.set(tx_start=1)
    await FallingEdge(dut.clk)
    await FallingEdge(dut.clk)
    uio.set(tx_start=0)

    # Hunt for the first epoch strobe, which is coincident with chip 0.
    for _ in range(32):
        await FallingEdge(dut.clk)
        if tx_epoch(dut):
            break
    else:
        raise AssertionError("tx_epoch_out never asserted after tx_start")

    chips, epochs = [tx_chip(dut)], [0]
    for i in range(1, 8 * L):
        await FallingEdge(dut.clk)
        chips.append(tx_chip(dut))
        if tx_epoch(dut):
            epochs.append(i)

    expected = spread_byte(data, code_id)
    assert chips == expected, (
        "chip stream mismatch at chip "
        f"{next(i for i, (a, b) in enumerate(zip(chips, expected)) if a != b)}"
    )
    assert epochs == [i * L for i in range(8)], f"epoch strobes at {epochs}"

    # The MSB of 0xA5 is 1, so the first code period must be the inverted
    # Gold code.  Check that polarity convention explicitly.
    code = gold_code(code_id)
    assert chips[:L] == [c ^ 1 for c in code], "MSB-first inversion is wrong"
    dut._log.info("248 chips and 8 epoch strobes match the model exactly")


# ---------------------------------------------------------------------------
# 3. Loopback round trip
# ---------------------------------------------------------------------------

@cocotb.test()
async def test_loopback_round_trip(dut):
    """Spread and despread on chip: every byte must come back unchanged."""
    code_id = 13
    start_clock(dut)
    uio = await reset(dut, tx_code=code_id, acq=False)
    uio.set(loopback=1, jam_en=0, disp_sel=0)
    await ClockCycles(dut.clk, 3)

    # The full 256-byte sweep is 65k clocks, cheap at RTL but slow on the
    # gate-level netlist, so thin it out there.
    # All 256 values, at RTL and at gate level alike.  The gate-level
    # netlist simulates this in about five seconds, so there is no reason
    # for the post-layout test to be weaker than the pre-layout one --
    # gl_test is the check that most resembles the real chip.
    values = list(range(256))

    for value in values:
        await send_byte(dut, uio, value)
        got = int(dut.uo_out.value)
        assert got == value, f"loopback: sent {value:#04x}, got {got:#04x}"

    uio.set(disp_sel=1)
    await ClockCycles(dut.clk, 3)
    st = status_fields(int(dut.uo_out.value))
    assert st["valid"] == 1, "rx_valid should be set after a complete byte"
    assert st["locked"] == 1, "a clean loopback must lock"
    assert st["m_last"] in (0, L), (
        f"clean correlation must be 0 or 31, got {st['m_last']}"
    )
    dut._log.info("round-tripped %d bytes, final status %s", len(values), st)


# ---------------------------------------------------------------------------
# 4. Exact anti-jam margin
# ---------------------------------------------------------------------------

@cocotb.test()
async def test_jam_margin_exact(dut):
    """Corrupt exactly J of 31 chips per window and check the majority vote.

    Errors are injected from Python rather than by the on-chip jammer so the
    count is exact.  The majority vote over 31 chips must be right for every
    J <= 15 and must invert for every J >= 16. That cliff is the whole point
    of the processing gain.
    """
    code_id = 5
    data = 0xA5
    start_clock(dut)

    for j in (0, 1, 7, 14, 15, 16, 17, 24, 31):
        uio = await reset(dut, tx_code=code_id, acq=False)
        uio.set(loopback=0, jam_en=0, disp_sel=0)
        await ClockCycles(dut.clk, 3)

        chips = spread_byte(data, code_id)
        for w in range(8):                       # flip the first j chips of
            for k in range(j):                   # every window
                chips[w * L + k] ^= 1

        await drive_chips(dut, uio, chips)
        await ClockCycles(dut.clk, 6)

        got = int(dut.uo_out.value)
        expect = despread_byte(chips, code_id)
        assert got == expect, f"J={j}: got {got:#04x}, model says {expect:#04x}"

        if j <= 15:
            assert got == data, f"J={j} must still decode cleanly, got {got:#04x}"
        else:
            assert got == (~data & 0xFF), f"J={j} must invert, got {got:#04x}"
        dut._log.info("J=%2d of 31 -> rx 0x%02X  (%s)", j, got,
                      "clean" if j <= 15 else "inverted")


# ---------------------------------------------------------------------------
# 5a. Foreign code rejection, windows aligned
# ---------------------------------------------------------------------------

@cocotb.test()
async def test_foreign_code_aligned(dut):
    """With aligned windows a foreign code must land on the Gold bound set.

    This is the pure CDMA property.  Gold cross-correlation for a preferred
    pair takes only the values {-9,-1,+7}, i.e. M in {11,15,19}.  A data bit
    of 1 inverts the transmitted code, which maps R -> -R and therefore
    M -> 31-M, adding {20,16,12}.  So the complete set of aligned impostor
    match counts is {11,12,15,16,19,20}, every one of them strictly inside
    the lock window of M <= 6 or M >= 25.

    Alignment is guaranteed here by running epoch-slaved, which isolates the
    cross-correlation bound from the acquisition question in 5b.
    """
    start_clock(dut)
    uio = await reset(dut, tx_code=7, rx_code=19, acq=False)
    uio.set(loopback=1, jam_en=0, disp_sel=1)
    await ClockCycles(dut.clk, 3)

    seen = set()
    for _ in range(6):
        dut.ui_in.value = 0xA5
        uio.set(tx_start=1)
        await FallingEdge(dut.clk)
        await FallingEdge(dut.clk)
        uio.set(tx_start=0)
        for _ in range(8 * L + 12):
            await FallingEdge(dut.clk)
            st = status_fields(int(dut.uo_out.value))
            assert st["locked"] == 0, f"locked onto an aligned foreign code: {st}"
            if st["m_last"]:
                seen.add(st["m_last"])

    assert seen, "no correlation windows completed"
    aligned_set = {11, 12, 15, 16, 19, 20}
    assert seen <= aligned_set, (
        f"aligned impostor match counts must come from {sorted(aligned_set)}, "
        f"saw {sorted(seen)}"
    )
    for m in seen:
        assert LOCK_LO < m < LOCK_HI
    dut._log.info("aligned foreign code: match counts %s, never locked",
                  sorted(seen))


# ---------------------------------------------------------------------------
# 5b. Foreign code rejection, receiver free-running
# ---------------------------------------------------------------------------

@cocotb.test()
async def test_foreign_code_acquiring(dut):
    """A free-running receiver must not false-lock onto a foreign code.

    This is the harder case and the reason LOCK is set at |R| >= 19 instead of
    just above the Gold bound of 9.  An unacquired receiver slides its window
    across data-bit boundaries, so it sees sums of two partial correlations,
    those are not covered by the Gold bound and reach |R| = 17.  At a threshold
    of 13 this test fails, because it locks onto the wrong transmitter.
    """
    rx_code = 19
    tx_code = 7                      # what the air actually carries
    start_clock(dut)

    # The receiver is told to listen on rx_code (tx_code field also set to it,
    # unused here), while Python transmits on a different code entirely.
    uio = await reset(dut, tx_code=rx_code, rx_code=0, acq=True)
    uio.set(loopback=0, jam_en=0, disp_sel=1)
    await ClockCycles(dut.clk, 3)

    payload = [0xA5, 0x5A, 0xC3, 0x6B]
    stream = []
    for value in payload:
        stream.extend(spread_byte(value, tx_code))

    worst = {"m": 0}

    def watch(_i, uo):
        st = status_fields(uo)
        assert st["locked"] == 0, (
            f"FALSE LOCK onto foreign code: {st} "
            "(lock threshold is too low for free-running operation)"
        )
        assert st["valid"] == 0, f"delivered a byte from a foreign code: {st}"
        # m_last reads 0 until the first window completes, and a real M of 0
        # would be |R| = 31 and would have tripped the lock assert above, so
        # skipping zeros here only excludes the reset state.
        if st["m_last"]:
            worst["m"] = max(worst["m"], abs(2 * st["m_last"] - L))

    await drive_chips(dut, uio, stream, epoch=False, watch=watch)
    await ClockCycles(dut.clk, 6)

    uio.set(disp_sel=0)
    await ClockCycles(dut.clk, 3)
    assert int(dut.uo_out.value) == 0, "rx_data must stay empty"
    dut._log.info(
        "free-running foreign code rejected over %d chips; worst |R| seen = %d "
        "(lock needs >= %d)", len(stream), worst["m"], L - 2 * LOCK_LO)


# ---------------------------------------------------------------------------
# 6. Serial-search acquisition
# ---------------------------------------------------------------------------

@cocotb.test()
async def test_acquisition_finds_phase(dut):
    """With no epoch strobe at all, the receiver must find the chip phase.

    The stream starts at a deliberately wrong offset.  Each failed window costs
    one slipped chip, so the search covers all 31 phases in bounded time.
    """
    code_id = 11
    data = 0xA5
    offset = 7

    start_clock(dut)
    uio = await reset(dut, tx_code=code_id, acq=True)
    uio.set(loopback=0, jam_en=0, disp_sel=1)
    await ClockCycles(dut.clk, 3)

    stream = (spread_byte(data, code_id) * 12)[offset:]

    locked_after = {"i": None}

    def watch(i, uo):
        if locked_after["i"] is None and status_fields(uo)["locked"]:
            locked_after["i"] = i

    await drive_chips(dut, uio, stream, epoch=False, watch=watch)

    assert locked_after["i"] is not None, "acquisition never locked"
    # 31 phases to try, 31 chips per trial, plus the confidence ramp.
    assert locked_after["i"] < 40 * L, (
        f"acquisition took {locked_after['i']} chips, expected under {40 * L}"
    )

    st = status_fields(int(dut.uo_out.value))
    assert st["locked"] == 1 and st["hunting"] == 0, st
    assert st["m_last"] in (0, L), (
        f"after lock M must be 0 or 31, got {st['m_last']}"
    )
    dut._log.info("acquired after %d chips (%.1f windows), status %s",
                  locked_after["i"], locked_after["i"] / L, st)


# ---------------------------------------------------------------------------
# 7. On-chip jammer ladder
# ---------------------------------------------------------------------------

@cocotb.test()
async def test_onchip_jammer_ladder(dut):
    """Sweep the built-in jammer and check the predicted four-tier behaviour.

    Levels 0 and 1 must hold lock and deliver clean data.  Level 2 should lose
    lock but still decode, because the decision threshold (16) is lower than
    the lock threshold (25).  Level 3 flips half the chips and must break the
    link.
    """
    code_id = 21
    payload = [0x00, 0xFF, 0xA5, 0x5A, 0x01, 0x80, 0x3C, 0xC3]
    start_clock(dut)

    results = {}
    for level in range(4):
        uio = await reset(dut, tx_code=code_id, jam_level=level, acq=False)
        uio.set(loopback=1, jam_en=1, disp_sel=0)
        await ClockCycles(dut.clk, 3)

        bit_errors = 0
        for value in payload:
            await send_byte(dut, uio, value)
            bit_errors += bin(int(dut.uo_out.value) ^ value).count("1")

        uio.set(disp_sel=1)
        await ClockCycles(dut.clk, 3)
        st = status_fields(int(dut.uo_out.value))

        total_bits = 8 * len(payload)
        results[level] = (bit_errors, bit_errors / total_bits, st["locked"])
        dut._log.info(
            "jam level %d (p=%4.1f%%, E[flips]=%4.1f/31): %d/%d bit errors, "
            "BER=%.3f, M=%d, locked=%d",
            level, 100 * JAM_THRESH[level] / 32, L * JAM_THRESH[level] / 32,
            bit_errors, total_bits, results[level][1], st["m_last"], st["locked"],
        )

    assert results[0][0] == 0, f"level 0 must be error free, got {results[0]}"
    assert results[1][0] == 0, f"level 1 must be error free, got {results[1]}"
    assert results[0][2] == 1, "level 0 must hold lock"
    assert results[1][2] == 1, "level 1 must hold lock"
    assert results[2][1] < 0.15, f"level 2 BER unexpectedly high: {results[2]}"
    assert results[3][1] > 0.05, (
        f"level 3 flips half the chips and must break the link, got {results[3]}"
    )
    assert results[3][1] > results[1][1], "BER must grow with jammer strength"
    dut._log.info("jam ladder: %s",
                  {k: f"BER={v[1]:.3f} locked={v[2]}" for k, v in results.items()})


# ---------------------------------------------------------------------------
# 8. Control inputs that are not aligned to the clock
# ---------------------------------------------------------------------------

@cocotb.test()
async def test_async_control_timing(dut):
    """Drive the control pins at times that are not aligned to the clock.

    Every other test in this file changes inputs on a clean half cycle
    boundary, which is a comfortable fiction. The real chip is wired to slide
    switches, jumper wires and possibly a second chip, and those change
    whenever they like. This test asserts and releases tx_start at random sub
    cycle offsets so the pin moves arbitrarily close to a rising edge, and it
    holds the pulse for a deliberately non integer number of cycles.

    What this does catch is the logic bug class an FPGA would catch. An edge
    detector that emits two pulses, a pulse narrower than the synchroniser can
    pass, or control logic that assumes an input is stable for a whole cycle.
    What it cannot do is model metastability, because a digital simulator
    resolves every sample to a clean 0 or 1. Real silicon confidence for that
    part comes from the two flop synchronisers, which Section 7.5 of the
    design notes covers, and ultimately from a board.
    """
    code_id = 9
    start_clock(dut)
    uio = await reset(dut, tx_code=code_id, acq=False)
    uio.set(loopback=1, jam_en=0, disp_sel=0)
    await ClockCycles(dut.clk, 3)

    rng = random.Random(20261129)
    payload = [0x00, 0xFF, 0xA5, 0x5A, 0x3C, 0xC3, 0x01, 0x80]

    # Work in whole picoseconds. The test bench timescale is 1ns/1ps, so an
    # arbitrary float number of nanoseconds is not representable and cocotb
    # rejects it rather than silently rounding.
    period_ps = CLK_NS * 1000

    for value in payload:
        dut.ui_in.value = value

        # Assert somewhere inside a clock period rather than on an edge,
        # including within 1 ps of the rising edge.
        offset = rng.randrange(1, period_ps)
        await Timer(offset, "ps")
        uio.set(tx_start=1)

        # Hold for a width that is never a whole number of cycles.
        width = rng.randrange(period_ps + 1, 3 * period_ps)
        await Timer(width, "ps")
        uio.set(tx_start=0)

        await ClockCycles(dut.clk, 8 * L + 24)
        got = int(dut.uo_out.value)
        assert got == value, (
            f"async timing: sent {value:#04x}, got {got:#04x} "
            f"(start asserted at +{offset} ps, held {width} ps)"
        )

    uio.set(disp_sel=1)
    await ClockCycles(dut.clk, 3)
    st = status_fields(int(dut.uo_out.value))
    assert st["locked"] == 1 and st["valid"] == 1, st
    dut._log.info("%d bytes survived start pulses at arbitrary sub cycle "
                  "offsets and widths", len(payload))


# ---------------------------------------------------------------------------
# 10. The output drivers can be turned off
# ---------------------------------------------------------------------------

@cocotb.test()
async def test_tx_pins_off(dut):
    """TX_PINS_OFF (page 1) must tri-state both output pins for the whole run.

    Two boards running this design both drive uio[7:6]. Wiring them together
    without care therefore puts one output driver against another, with only
    the drivers' on resistance limiting the current. Configuring the
    receiving board with tx_pins_off removes that failure mode electrically
    instead of relying on correct wiring.

    The receiver has to keep working while the drivers are off, because the
    whole point is a board that listens and never talks, so this also runs a
    full byte through the external path and checks it arrives.
    """
    code_id = 5
    data = 0xC3
    start_clock(dut)

    # Default configuration still drives the two output pins.
    uio = await reset(dut, tx_code=code_id)
    assert int(dut.uio_oe.value) == 0b1100_0000, (
        f"default uio_oe should be 0b11000000, got {int(dut.uio_oe.value):#010b}"
    )

    # With tx_pins_off the part drives nothing anywhere on the uio bus.
    uio = await reset(dut, tx_code=code_id, tx_pins_off=True)
    assert int(dut.uio_oe.value) == 0b0000_0000, (
        f"tx_pins_off should give uio_oe 0, got {int(dut.uio_oe.value):#010b}"
    )

    # It must hold for the whole run, not just out of reset, because the
    # pins would otherwise become outputs again at the first transmission.
    uio.set(loopback=1, jam_en=0, disp_sel=0)
    await send_byte(dut, uio, data)
    assert int(dut.uio_oe.value) == 0b0000_0000, (
        "uio_oe must stay 0 while transmitting"
    )
    assert int(dut.uo_out.value) == data, "loopback must still work"

    # And a receive only board must still receive from the external pins.
    uio = await reset(dut, tx_code=code_id, acq=False, tx_pins_off=True)
    uio.set(loopback=0, jam_en=0, disp_sel=0)
    await ClockCycles(dut.clk, 3)
    await drive_chips(dut, uio, spread_byte(data, code_id))
    await ClockCycles(dut.clk, 6)
    got = int(dut.uo_out.value)
    assert got == data, f"receive only path broken: got {got:#04x}"
    assert int(dut.uio_oe.value) == 0b0000_0000

    dut._log.info("tx_pins_off tri-states uio[7:6] for the whole run while "
                  "loopback and the external receive path both still work")


# ===========================================================================
# 1x2 features
# ===========================================================================

# ---------------------------------------------------------------------------
# 11. Configuration pages
# ---------------------------------------------------------------------------

@cocotb.test()
async def test_config_pages(dut):
    """Page 1 must take effect, and a page-0-only reset must clear it again.

    Page 0 clearing page 1 is what keeps the mode registers defined after any
    reset, so a board reset with uio[5] low always comes up in plain DSSS.
    """
    start_clock(dut)

    uio = await reset(dut, tx_code=7, tx_pins_off=True, disp_page=2)
    assert int(dut.uio_oe.value) == 0, "page 1 tx_pins_off not applied"
    uio.set(disp_sel=1)
    await ClockCycles(dut.clk, 3)
    assert int(dut.uo_out.value) == PARK_CH, (
        f"page 2 idle should show the parked channel, got {int(dut.uo_out.value):#04x}"
    )

    # FHSS on: an idle transmitter sits at its hop seed {code_id, 1}.
    uio = await reset(dut, tx_code=7, fhss=True, disp_page=2)
    uio.set(disp_sel=1)
    await ClockCycles(dut.clk, 3)
    assert int(dut.uo_out.value) == (7 << 1) | 1, int(dut.uo_out.value)

    # Page 0 only: every mode field must be back to zero.
    uio = await reset(dut, tx_code=7)
    assert int(dut.uio_oe.value) == 0b1100_0000, "page 0 did not clear page 1"
    uio.set(disp_sel=1)
    await ClockCycles(dut.clk, 3)
    st = status_fields(int(dut.uo_out.value))
    assert st == {"valid": 0, "locked": 0, "hunting": 0, "m_last": 0}, (
        f"disp_page should be back to 0, got {st}"
    )
    dut._log.info("page 1 applies, page 0 clears it")


# ---------------------------------------------------------------------------
# 12. Transmit frame formats
# ---------------------------------------------------------------------------

@cocotb.test()
async def test_tx_frame_formats(dut):
    """Every frame format must match the model chip for chip."""
    code_id = 9
    data = 0x3C
    start_clock(dut)

    for fec, framed in ((True, False), (False, True), (True, True)):
        uio = await reset(dut, tx_code=code_id, fec=fec, framed=framed)
        nbits = frame_len(fec, framed)

        dut.ui_in.value = data
        uio.set(tx_start=1)
        await FallingEdge(dut.clk)
        await FallingEdge(dut.clk)
        uio.set(tx_start=0)
        for _ in range(32):
            await FallingEdge(dut.clk)
            if tx_epoch(dut):
                break
        else:
            raise AssertionError("tx_epoch_out never asserted")

        chips, epochs = [tx_chip(dut)], [0]
        for i in range(1, nbits * L):
            await FallingEdge(dut.clk)
            chips.append(tx_chip(dut))
            if tx_epoch(dut):
                epochs.append(i)
        await ClockCycles(dut.clk, 4)

        expected = spread_frame(data, code_id, fec=fec, framed=framed)
        assert chips == expected, (
            f"fec={fec} framed={framed}: chip mismatch at "
            f"{next(i for i, (a, b) in enumerate(zip(chips, expected)) if a != b)}"
        )
        assert epochs == [i * L for i in range(nbits)], epochs
        dut._log.info("fec=%d framed=%d: %d bits, %d chips match the model",
                      fec, framed, nbits, len(chips))


# ---------------------------------------------------------------------------
# 13. Loopback in every frame format
# ---------------------------------------------------------------------------

@cocotb.test()
async def test_loopback_all_modes(dut):
    """Round trip with FEC and/or framing on.  FEC + framing does all 256."""
    code_id = 13
    start_clock(dut)

    plans = (
        (True, True, list(range(256))),
        (True, False, list(range(0, 256, 4))),
        (False, True, list(range(1, 256, 4))),
    )
    for fec, framed, values in plans:
        uio = await reset(dut, tx_code=code_id, fec=fec, framed=framed,
                          disp_page=1, page1=True)
        uio.set(loopback=1, jam_en=0, disp_sel=0)
        await ClockCycles(dut.clk, 3)
        nbits = frame_len(fec, framed)

        for value in values:
            await send_byte(dut, uio, value, nbits=nbits)
            got = int(dut.uo_out.value)
            assert got == value, (
                f"fec={fec} framed={framed}: sent {value:#04x}, got {got:#04x}"
            )

        st = page1_fields(await read_status(dut, uio))
        assert st["valid"] == 1 and st["locked"] == 1, st
        assert st["fec_err"] == 0 and st["fec_fix"] == 0, (
            f"a clean channel must need no corrections: {st}"
        )
        assert st["frames"] == len(values) % 4, st
        dut._log.info("fec=%d framed=%d: %d bytes round-tripped, status %s",
                      fec, framed, len(values), st)


# ---------------------------------------------------------------------------
# 14. FEC on injected bit errors
# ---------------------------------------------------------------------------

def _flip_bits(bits, positions):
    out = list(bits)
    for p in positions:
        out[p] ^= 1
    return out


@cocotb.test()
async def test_fec_injected_errors(dut):
    """Corrupt whole data bits on purpose and check SECDED against the model.

    A bit is corrupted by inverting all 31 of its chips, which is a decision
    error with no ambiguity.  Positions are in transmission order, where even
    positions belong to the high codeword and odd ones to the low codeword.
    """
    code_id = 3
    value = 0xB7
    start_clock(dut)
    clean = frame_bits(value, fec=True)

    cases = {
        "clean": [],
        "one error, high cw": [0],
        "one error, low cw": [5],
        "adjacent burst, one per cw": [6, 7],
        "parity bit only": [15],
        "two errors in high cw": [0, 2],
        "two errors in both": [0, 4, 1, 3],
    }
    for name, positions in cases.items():
        bits = _flip_bits(clean, positions)
        rx = FrameReceiver(fec=True)
        for b in bits:
            rx.push(b)
        exp_byte, exp_raw, exp_fix, exp_err = rx.out[0]

        for page in (1, 3):
            uio = await reset(dut, tx_code=code_id, fec=True, disp_page=page)
            uio.set(loopback=0, jam_en=0, disp_sel=0)
            await ClockCycles(dut.clk, 3)
            await drive_chips(dut, uio, spread_bits(bits, code_id))
            await ClockCycles(dut.clk, 6)

            got = int(dut.uo_out.value)
            assert got == exp_byte, f"{name}: byte {got:#04x}, model {exp_byte:#04x}"
            status = await read_status(dut, uio)
            if page == 1:
                st = page1_fields(status)
                assert st["fec_fix"] == exp_fix and st["fec_err"] == int(exp_err), (
                    f"{name}: flags {st}, model fix={exp_fix} err={exp_err}"
                )
            else:
                assert status == exp_raw, (
                    f"{name}: raw {status:#04x}, model {exp_raw:#04x}"
                )

        if len(positions) <= 1 or name.startswith("adjacent"):
            assert exp_byte == value, f"{name} must be corrected"
        dut._log.info("%-28s -> rx %#04x raw %#04x fix=%s err=%d",
                      name, exp_byte, exp_raw, format(exp_fix, "02b"),
                      exp_err)


# ---------------------------------------------------------------------------
# 15. FEC against the on-chip jammer
# ---------------------------------------------------------------------------

@cocotb.test()
async def test_fec_beats_jammer(dut):
    """At a jam level that breaks the plain link, FEC must recover it.

    Fine level 6 flips 31% of chips.  Theory (jamguard_model.py) puts the
    raw byte error rate at 11% and the FEC byte error rate at 1.1%.  The
    receiver runs epoch-slaved, because lock is genuinely lost at this
    density, and the point here is the decoder, not acquisition.
    """
    code_id = 17
    level = 6
    rng = random.Random(4242)
    payload = [rng.randrange(256) for _ in range(40)]
    start_clock(dut)

    results = {}
    for fec in (False, True):
        uio = await reset(dut, tx_code=code_id, jam_level=level, acq=False,
                          fec=fec, disp_page=1)
        uio.set(loopback=1, jam_en=1, disp_sel=0)
        await ClockCycles(dut.clk, 3)

        byte_errs = bit_errs = fixes = flagged = 0
        for value in payload:
            await send_byte(dut, uio, value, nbits=frame_len(fec))
            got = int(dut.uo_out.value)
            byte_errs += got != value
            bit_errs += bin(got ^ value).count("1")
            if fec:
                st = page1_fields(await read_status(dut, uio))
                fixes += bin(st["fec_fix"]).count("1")
                flagged += st["fec_err"]
        results[fec] = (byte_errs, bit_errs, fixes, flagged)
        dut._log.info(
            "jam fine level %d (p=%.1f%%), FEC %s: %d/%d bytes wrong, "
            "%d bit errors, %d codewords corrected, %d flagged uncorrectable",
            level, 100 * JAM_THRESH[level] / 32, "on " if fec else "off",
            byte_errs, len(payload), bit_errs, fixes, flagged)

    plain, coded = results[False], results[True]
    assert plain[0] >= 2, f"the plain link should break at this level: {plain}"
    assert coded[2] >= 1, f"FEC should have corrected something: {coded}"
    assert coded[0] * 2 <= plain[0], (
        f"FEC should at least halve the byte error count: plain {plain}, fec {coded}"
    )


# ---------------------------------------------------------------------------
# 16. Fully self-synchronising receiver: no epoch wire at all
# ---------------------------------------------------------------------------

@cocotb.test()
async def test_framed_acquisition_no_epoch(dut):
    """Chip phase from serial search, byte phase from the sync word.

    The stream starts at a random chip offset partway into a frame, with no
    epoch strobe at all.  Once locked and synced, every byte delivered must be
    the right byte in the right order, with no rotation.  Before sync this
    would deliver rotated bytes, which is exactly what framing fixes.
    """
    code_id = 23
    payload = [0x11, 0x2E, 0x93, 0x4C, 0xD5, 0x6A, 0xF7, 0x08, 0xB9, 0x5D,
               0xE2, 0x37, 0x84, 0xCB]
    rng = random.Random(1129)
    start_clock(dut)

    for fec in (False, True):
        nbits = frame_len(fec, True)
        for _trial in range(2):
            offset = rng.randrange(1, nbits * L)
            uio = await reset(dut, tx_code=code_id, acq=True, fec=fec,
                              framed=True)
            uio.set(loopback=0, jam_en=0, disp_sel=0)
            await ClockCycles(dut.clk, 3)

            stream = []
            for v in payload:
                stream.extend(spread_frame(v, code_id, fec=fec, framed=True))
            stream = stream[offset:]

            seen = []

            def watch(_i, uo, seen=seen):
                if uo != 0 and (not seen or seen[-1] != uo):
                    seen.append(uo)

            await drive_chips(dut, uio, stream, epoch=False, watch=watch)
            await ClockCycles(dut.clk, 6)

            # Frames lost before the first delivery: the one cut by the
            # offset, those that pass during a worst-case serial search
            # (31 phases of 31 chips, plus two windows of confidence ramp),
            # and the one in progress when lock is declared.
            frame_chips = nbits * L
            max_lost = 2 + -(-(33 * L) // frame_chips)
            assert len(seen) >= len(payload) - max_lost, (
                f"fec={fec} offset={offset}: only {len(seen)} bytes: "
                f"{[hex(v) for v in seen]}"
            )
            start = payload.index(seen[0]) if seen[0] in payload else -1
            assert start >= 0 and payload[start:start + len(seen)] == seen, (
                f"fec={fec} offset={offset}: delivered {[hex(v) for v in seen]}"
            )
            dut._log.info("fec=%d, start offset %4d chips: first byte %d of %d, "
                          "then %d in order", fec, offset, start + 1,
                          len(payload), len(seen))


# ---------------------------------------------------------------------------
# 17. Sync word error tolerance
# ---------------------------------------------------------------------------

@cocotb.test()
async def test_sync_word_tolerance(dut):
    """One bit error in the sync word is tolerated, two lose the frame.

    The receiver is compared against the model's FrameReceiver bit for bit,
    including what it does with the payload of a frame whose sync it missed.
    A clean frame afterwards must always get through.
    """
    code_id = 27
    start_clock(dut)

    for flips in ([], [3], [0, 5]):
        uio = await reset(dut, tx_code=code_id, framed=True, disp_page=1)
        uio.set(loopback=0, jam_en=0, disp_sel=0)
        await ClockCycles(dut.clk, 3)

        bits = _flip_bits(frame_bits(0x5A, framed=True), flips)
        bits += frame_bits(0xC6, framed=True)
        model = FrameReceiver(framed=True)
        for b in bits:
            model.push(b)

        await drive_chips(dut, uio, spread_bits(bits, code_id))
        await ClockCycles(dut.clk, 6)
        st = page1_fields(await read_status(dut, uio))
        got = int(dut.uo_out.value)

        assert st["frames"] == len(model.out) % 4, (
            f"sync errors {flips}: {st['frames']} frames, model {len(model.out)}"
        )
        assert got == model.out[-1][0] == 0xC6, f"last byte {got:#04x}"
        if len(flips) <= 1:
            assert len(model.out) == 2, "one sync error must be tolerated"
        dut._log.info("sync word with %d bit error(s): %d of 2 frames delivered",
                      len(flips), len(model.out))


# ---------------------------------------------------------------------------
# 18. Hop sequence
# ---------------------------------------------------------------------------

@cocotb.test()
async def test_fhss_hop_sequence(dut):
    """The displayed hop channel must follow the model, chip by chip."""
    code_id = 21
    start_clock(dut)

    for fhss, fast in ((True, False), (True, True), (False, False)):
        uio = await reset(dut, tx_code=code_id, fhss=fhss, hop_fast=fast,
                          disp_page=2, page1=True)
        uio.set(disp_sel=1)
        await ClockCycles(dut.clk, 3)

        dut.ui_in.value = 0x96
        uio.set(tx_start=1)
        await FallingEdge(dut.clk)
        await FallingEdge(dut.clk)
        uio.set(tx_start=0)
        for _ in range(32):
            await FallingEdge(dut.clk)
            if tx_epoch(dut):
                break
        else:
            raise AssertionError("tx_epoch_out never asserted")

        chans = []
        for i in range(8 * L):
            if i:
                await FallingEdge(dut.clk)
            v = int(dut.uo_out.value)
            assert v >> 7 == 1, "tx_busy must read 1 during the frame"
            chans.append(v & 0x3F)
        await ClockCycles(dut.clk, 4)
        assert int(dut.uo_out.value) >> 7 == 0, "tx_busy must clear after"

        expected = hop_channels(code_id, 8, fast=fast, fhss=fhss)
        assert chans == expected, (
            f"fhss={fhss} fast={fast}: hop mismatch at chip "
            f"{next(i for i, (a, b) in enumerate(zip(chans, expected)) if a != b)}"
        )
        dut._log.info("fhss=%d fast=%d: %d distinct channels over one frame",
                      fhss, fast, len(set(chans)))


# ---------------------------------------------------------------------------
# 19. Partial-band jammer against a fixed and a hopping transmitter
# ---------------------------------------------------------------------------

@cocotb.test()
async def test_partial_band_jammer(dut):
    """A narrowband jammer kills a parked link; hopping escapes it.

    The jammer covers channels 1..7 at 50% chip flips, its strongest
    setting.  A transmitter parked on channel 1 is under it all the time and
    the link breaks.  Hopping once per chip, only about 7 of every 63 chips
    are exposed, the effective flip rate falls to about 6%, and the 31-chip
    majority vote absorbs it completely.  Hopping once per bit is in between:
    whole bits land in the band and are lost, which is where FEC comes in.
    """
    code_id = 5
    rng = random.Random(77)
    payload = [rng.randrange(256) for _ in range(16)]
    start_clock(dut)

    # The hop sequence carries on across frames, so the exposure over the
    # whole payload follows from the model.
    expected_exposure = {}
    for fast in (False, True):
        for fec in (False, True):
            chans = hop_channels(code_id, frame_len(fec) * len(payload),
                                 fast=fast)
            expected_exposure[fast, fec] = (
                sum(in_band(c, 0) for c in chans) / len(chans))

    results = {}
    for label, fhss, fast, fec in (
        ("parked", False, False, False),
        ("hop/bit", True, False, False),
        ("hop/bit + FEC", True, False, True),
        ("hop/chip", True, True, False),
    ):
        uio = await reset(dut, tx_code=code_id, jam_level=3, acq=False,
                          fhss=fhss, hop_fast=fast, pbj=True, pbj_band=0,
                          fec=fec)
        uio.set(loopback=1, jam_en=1, disp_sel=0)
        await ClockCycles(dut.clk, 3)
        bit_errs = byte_errs = 0
        for value in payload:
            await send_byte(dut, uio, value, nbits=frame_len(fec))
            got = int(dut.uo_out.value)
            bit_errs += bin(got ^ value).count("1")
            byte_errs += got != value
        results[label] = (bit_errs / (8 * len(payload)), byte_errs)
        dut._log.info("%-14s BER %.3f, %2d/%d bytes wrong", label,
                      results[label][0], byte_errs, len(payload))

    dut._log.info("chips inside the jammed band: per-bit hop %.1f%%, "
                  "per-bit hop with FEC %.1f%%, per-chip hop %.1f%%",
                  100 * expected_exposure[False, False],
                  100 * expected_exposure[False, True],
                  100 * expected_exposure[True, False])
    assert results["parked"][0] > 0.2, f"parked link should break: {results}"
    assert expected_exposure[False, False] > 0, "slow hopping never met the jammer"
    assert results["hop/chip"][1] == 0, f"fast hopping must be clean: {results}"
    assert results["hop/bit"][0] < results["parked"][0], results
    assert results["hop/bit + FEC"][1] <= results["hop/bit"][1], results


# ---------------------------------------------------------------------------
# 20. A reset that only ever selects page 1
# ---------------------------------------------------------------------------

@cocotb.test()
async def test_reset_page1_only(dut):
    """A page-1-only reset must still leave the part fully defined.

    uio_in[5] is the page select during reset and DISP_SEL in run mode. An
    operator looking at a status page has it high, so pressing reset never
    executes the page 0 branch. cfg_a and cfg_b are written only in that
    branch and have no reset of their own.

    This is not theoretical. Every one of the 168 registers in the hardened
    netlist is a plain dfxtp_2 with no reset pin, so reset is entirely
    synchronous and the power up state really is undefined. If page 0 never
    loads, the code id, the jammer level and acq_en are whatever the silicon
    came up as, and that propagates into the code generators.
    """
    start_clock(dut)
    dut.ena.value = 1
    dut.rst_n.value = 0
    dut.ui_in.value = 0x55
    dut.uio_in.value = 1 << 5          # page 1 selected for the whole reset
    await ClockCycles(dut.clk, 6)
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    await ClockCycles(dut.clk, 4)
    await FallingEdge(dut.clk)

    uio = Uio(dut)
    uio.set(loopback=1, jam_en=0, disp_sel=0)
    await ClockCycles(dut.clk, 3)
    await send_byte(dut, uio, 0xA5)

    def undefined(sig, name):
        s = str(sig.value).lower()
        bad = [c for c in s if c not in "01"]
        assert not bad, f"{name} is not fully defined after a page-1-only reset: {s}"

    undefined(dut.uo_out, "uo_out")
    undefined(dut.uio_out, "uio_out")
    undefined(dut.uio_oe, "uio_oe")
    dut._log.info("page-1-only reset leaves every output defined")


# ---------------------------------------------------------------------------
# 21. Streaming transmit, tx_start held high
# ---------------------------------------------------------------------------

@cocotb.test()
async def test_tx_streaming(dut):
    """Holding tx_start high must emit frames back to back.

    The header and the datasheet both document this, and demo 6 tells the
    operator to use it, but every other test releases tx_start a couple of
    clocks after asserting it and therefore never reaches the reload branch
    at the end of a frame. That line was dead code in simulation.

    Streaming plus framing is also the only configuration that puts two sync
    words on the air back to back, which the software model covers and
    nothing in RTL did.
    """
    code_id = 9
    data = 0x3C
    nframes = 3
    start_clock(dut)

    for fec, framed in ((False, True), (True, True)):
        uio = await reset(dut, tx_code=code_id, fec=fec, framed=framed)
        nbits = frame_len(fec, framed)

        dut.ui_in.value = data
        uio.set(tx_start=1)                 # held high for the whole run
        for _ in range(32):
            await FallingEdge(dut.clk)
            if tx_epoch(dut):
                break
        else:
            raise AssertionError("tx_epoch_out never asserted")

        total = nframes * nbits * L
        chips, epochs = [tx_chip(dut)], [0]
        for i in range(1, total):
            await FallingEdge(dut.clk)
            chips.append(tx_chip(dut))
            if tx_epoch(dut):
                epochs.append(i)
        uio.set(tx_start=0)

        one = spread_frame(data, code_id, fec=fec, framed=framed)
        expected = one * nframes
        assert chips == expected, (
            f"fec={fec}: streaming chip mismatch at "
            f"{next(i for i, (a, b) in enumerate(zip(chips, expected)) if a != b)}"
            f" of {total}"
        )
        # Epochs must stay on the grid with no gap between frames.
        assert epochs == [i * L for i in range(nframes * nbits)], (
            f"fec={fec}: epoch grid broke at frame boundary: {epochs[:40]}"
        )
        dut._log.info("fec=%d: %d frames streamed back to back, %d chips, "
                      "epoch grid unbroken", fec, nframes, total)


# ---------------------------------------------------------------------------
# 22. The 127-chip code family
# ---------------------------------------------------------------------------

@cocotb.test()
async def test_long_code_chip_sequence(dut):
    """The 127-chip transmitter must emit the degree-7 Gold code exactly.

    This is the test that would catch a wrong tap on the long polynomial,
    which no amount of loopback testing would: loopback uses the same
    generator on both sides, so a wrong polynomial still round trips.
    """
    code_id = 37        # needs both the page 0 and page 2 halves of the seed
    data = 0x9C
    big_l = LONG.L

    start_clock(dut)
    uio = await reset(dut, tx_code=code_id, len127=True)

    dut.ui_in.value = data
    uio.set(tx_start=1)
    await FallingEdge(dut.clk)
    await FallingEdge(dut.clk)
    uio.set(tx_start=0)

    for _ in range(32):
        await FallingEdge(dut.clk)
        if tx_epoch(dut):
            break
    else:
        raise AssertionError("tx_epoch_out never asserted after tx_start")

    chips, epochs = [tx_chip(dut)], [0]
    for i in range(1, 8 * big_l):
        await FallingEdge(dut.clk)
        chips.append(tx_chip(dut))
        if tx_epoch(dut):
            epochs.append(i)

    expected = spread_byte(data, code_id, fam=LONG)
    assert chips == expected, (
        "long chip stream mismatch at chip "
        f"{next(i for i, (a, b) in enumerate(zip(chips, expected)) if a != b)}"
    )
    assert epochs == [i * big_l for i in range(8)], f"epochs at {epochs}"
    dut._log.info("%d chips and 8 epoch strobes match the degree-7 model",
                  8 * big_l)


@cocotb.test()
async def test_long_code_loopback(dut):
    """Every byte must survive a 127-chip round trip, and beat 31 chips.

    The second half is the measurement that justifies the long family
    existing at all: both lengths are run through the same jammer at the same
    strength and the bit errors are counted.  At 34.4% of chips flipped the
    predicted bit error rate is 3.6e-2 at L = 31 and 1.5e-4 at L = 127, so
    256 bits should show around nine errors on the short code and none on the
    long one.
    """
    code_id = 70
    payload = [(0x5A + 7 * i) & 0xFF for i in range(32)]

    start_clock(dut)

    # Clean channel: both lengths must be exact and must hold lock.
    for use_long, fam in ((False, SHORT), (True, LONG)):
        uio = await reset(dut, tx_code=code_id, jam_level=0, len127=use_long)
        uio.set(loopback=1, jam_en=0, disp_sel=0)
        await ClockCycles(dut.clk, 3)
        for value in payload[:6]:
            await send_byte(dut, uio, value, big_l=fam.L)
            assert int(dut.uo_out.value) == value, (
                f"clean {fam.name} round trip returned "
                f"{int(dut.uo_out.value):#04x}, expected {value:#04x}")
        st = await read_status(dut, uio)
        assert status_fields(st)["locked"] == 1, (
            f"{fam.name} must hold lock on a clean channel")

    # Jammed channel, same jammer, both lengths.
    errors = {}
    for use_long, fam in ((False, SHORT), (True, LONG)):
        uio = await reset(dut, tx_code=code_id, jam_level=2, len127=use_long)
        uio.set(loopback=1, jam_en=1, disp_sel=0)
        await ClockCycles(dut.clk, 3)
        bad = 0
        for value in payload:
            await send_byte(dut, uio, value, big_l=fam.L)
            bad += bin(int(dut.uo_out.value) ^ value).count("1")
        errors[fam.name] = bad
        dut._log.info("%s at 34.4%% chips flipped: %d bit errors in %d",
                      fam.name, bad, 8 * len(payload))

    assert errors["L127"] == 0, (
        f"L=127 should be error free at this jam level, got "
        f"{errors['L127']} bit errors")
    assert errors["L31"] > 0, (
        "L=31 should show errors at this jam level, so the comparison with "
        "L=127 means something")
    dut._log.info("6.1 dB of extra processing gain: %d errors -> %d",
                  errors["L31"], errors["L127"])


@cocotb.test()
async def test_long_code_lock_band(dut):
    """Between 30.7% and 49.6% of chips corrupted, L=127 decodes but cannot
    acquire.

    Lock needs at most 39 of 127 chips wrong and the majority vote needs
    fewer than 64, so there is a band of channel quality in which an
    epoch-slaved receiver keeps delivering perfect bytes while a receiver
    hunting for the phase can never confirm one.  That is a property of the
    thresholds, not a defect, and it is the reason the datasheet tells you to
    acquire on a quiet channel and only then turn the jammer up.
    """
    code_id = 70
    payload = [0xA5, 0x3C, 0x81, 0x00, 0xFF, 0x5A]

    start_clock(dut)
    uio = await reset(dut, tx_code=code_id, jam_level=2, len127=True)
    uio.set(loopback=1, jam_en=1, disp_sel=0)
    await ClockCycles(dut.clk, 3)

    bad = 0
    for value in payload:
        await send_byte(dut, uio, value, big_l=LONG.L)
        bad += bin(int(dut.uo_out.value) ^ value).count("1")

    st = status_fields(await read_status(dut, uio))
    assert bad == 0, f"the majority vote should still decode, got {bad} errors"
    assert st["locked"] == 0, (
        "34.4% of chips flipped is past the 30.7% lock tolerance, so lock "
        "must NOT be held here")
    dut._log.info("decoded %d bytes with 0 errors while unlocked, exactly the "
                  "predicted band", len(payload))


@cocotb.test()
async def test_long_code_rejects_foreign(dut):
    """A 127-chip receiver must not lock onto a different long code.

    The transmitter and receiver are given different members of the degree-7
    family and the receiver is left free running, which is the configuration
    a false lock would actually happen in.
    """
    big_l = LONG.L
    start_clock(dut)
    uio = await reset(dut, tx_code=19, rx_code=100, acq=True, len127=True)
    uio.set(loopback=0, jam_en=0, disp_sel=1)
    await ClockCycles(dut.clk, 3)

    chips = spread_bits([1, 0, 1, 1, 0, 0, 1, 0] * 3, 19, fam=LONG)
    worst = 0
    locked_any = 0

    def watch(_i, byte_val):
        nonlocal worst, locked_any
        st = status_fields(byte_val)
        locked_any |= st["locked"]

    await drive_chips(dut, uio, chips, epoch=False, watch=watch, big_l=big_l)

    assert locked_any == 0, (
        "receiver locked onto a foreign 127-chip code, which the exhaustive "
        "search in jamguard_model.py says is impossible")
    dut._log.info("no false lock over %d chips of a foreign long code",
                  len(chips))


def page_last_fields(byte_val):
    """Unpack the last status page: {ovf, fifo_cnt[1:0], trk[2:0], len, prbs}."""
    return {
        "fifo_ovf": (byte_val >> 7) & 1,
        "fifo_cnt": (byte_val >> 5) & 3,
        "trk_cnt": (byte_val >> 2) & 7,
        "len127": (byte_val >> 1) & 1,
        "prbs_en": byte_val & 1,
    }


# ---------------------------------------------------------------------------
# 25. Chip-phase tracking
# ---------------------------------------------------------------------------

def _drift(chips, period, insert):
    """Add or remove one chip every `period` chips, simulating clock drift.

    Removing a chip models a transmitter whose clock runs fast relative to
    the receiver, so the stream arrives quicker than the local code advances.
    Adding one models the opposite.  Real crystals at the same nominal
    frequency differ by tens of parts per million and produce exactly this,
    just far more slowly than a simulation can afford to wait for.
    """
    out = []
    for i, c in enumerate(chips):
        if period and i and i % period == 0:
            if insert:
                out.append(c)
            else:
                continue
        out.append(c)
    return out


@cocotb.test()
async def test_chip_phase_tracking(dut):
    """Tracking must keep a drifting link alive that would otherwise die.

    This is the feature that decides whether two boards with their own
    crystals can talk at all.  Without tracking, a one-chip slip costs the
    receiver its lock, and recovery is not symmetric: the serial search can
    only ever retard the local phase, so a slip in the advancing direction
    needs up to L-1 further slips to walk all the way around.  With tracking
    the advanced correlator sees the peak immediately and the next window is
    shortened by one chip, which fixes the error inside one code period.
    """
    code_id = 11
    payload = [(0x31 * (i + 1)) & 0xFF for i in range(16)]
    base = []
    for v in payload:
        base.extend(spread_frame(v, code_id, framed=True))

    frame_chips = 16 * L         # a framed byte is 8 sync plus 8 payload bits
    start_clock(dut)

    results = {}
    for insert in (False, True):
        for trk in (False, True):
            uio = await reset(dut, tx_code=code_id, acq=True, framed=True,
                              trk=trk, disp_page=15)
            uio.set(loopback=0, jam_en=0, disp_sel=0)
            await ClockCycles(dut.clk, 3)

            seen = []

            def watch(_i, byte_val, seen=seen):
                if not seen or seen[-1] != byte_val:
                    seen.append(byte_val)

            await drive_chips(dut, uio,
                              _drift(base, 4 * frame_chips, insert),
                              epoch=False, watch=watch)
            delivered = sum(1 for v in payload if v in seen)
            trk_cnt = page_last_fields(
                await read_status(dut, uio))["trk_cnt"]
            results[(insert, trk)] = (delivered, trk_cnt)
            dut._log.info(
                "%s chips, tracking %s: %2d of %d bytes delivered, "
                "%d corrections",
                "added  " if insert else "dropped",
                "on " if trk else "off", delivered, len(payload), trk_cnt)

    for insert in (False, True):
        assert results[(insert, True)][1] > 0, (
            "tracking was enabled and the stream drifted, so the hardware "
            "must have made at least one phase correction")
        assert results[(insert, False)][1] == 0, (
            "tracking was disabled, so no correction may be reported")
        assert results[(insert, True)][0] >= results[(insert, False)][0], (
            "tracking must never deliver fewer bytes than no tracking")

    # Dropped chips are the hard direction.  A dropped chip leaves the local
    # replica one chip behind the signal, and the serial search can only
    # retard, so unaided it has to walk L-1 slips the long way round.  Added
    # chips leave it one chip ahead, which one slip happens to fix, so the
    # search gets that direction right by luck.  Tracking must show a clear
    # win on the hard direction.
    assert results[(False, True)][0] > results[(False, False)][0], (
        "on dropped chips tracking must deliver strictly more: got "
        f"{results[(False, True)][0]} with tracking against "
        f"{results[(False, False)][0]} without")
    dut._log.info(
        "dropped-chip drift: %d of %d bytes without tracking, %d of %d with",
        results[(False, False)][0], len(payload),
        results[(False, True)][0], len(payload))


# ---------------------------------------------------------------------------
# 26. Cross-correlation monitor
# ---------------------------------------------------------------------------

@cocotb.test()
async def test_cross_correlation_monitor(dut):
    """The monitor must measure the Gold bound in silicon, and never lock.

    A fourth correlator runs the next code in the family over the receiver's
    own windows.  With the receiver aligned, the monitor sees one member of
    the family against another at zero relative shift, which for this
    construction is exactly -1: the two codes differ by a shift of LFSR-B,
    and summing one full m-sequence period gives -1.  So |R| = 1 and the
    reported magnitude is 0.  Any aligned window at all is bounded by
    t(5) = 9, which is magnitude 4.
    """
    code_id = 9
    payload = [0xA5, 0x5A, 0x00, 0xFF, 0x3C]

    start_clock(dut)
    uio = await reset(dut, tx_code=code_id, mon=True, disp_page=6)
    uio.set(loopback=1, jam_en=0, disp_sel=0)
    await ClockCycles(dut.clk, 3)

    for value in payload:
        await send_byte(dut, uio, value)
        assert int(dut.uo_out.value) == value, "loopback broke during the test"

    st = await read_status(dut, uio)
    false_lock = (st >> 7) & 1
    peak = st & 0x7F

    assert false_lock == 0, (
        "a foreign code reached the lock threshold, which the exhaustive "
        "search in jamguard_model.py says cannot happen")
    assert peak <= 4, (
        f"aligned cross-correlation magnitude {peak} exceeds the t(5) = 9 "
        "bound, which is magnitude 4")
    dut._log.info("monitor peak magnitude %d (|R| = %d), bound is 4 "
                  "(|R| = 9), false lock %d", peak, 2 * peak + 1, false_lock)


# ---------------------------------------------------------------------------
# 27. Built-in bit error rate tester
# ---------------------------------------------------------------------------

@cocotb.test()
async def test_prbs_bert(dut):
    """The pattern generator, the checker and both counters, bit exact.

    The transmitted bytes must be the PRBS-9 sequence the model predicts, and
    the error counter must equal what the model's checker reports over those
    very bytes, including the startup transient from an empty history.
    """
    code_id = 23
    nbytes = 10
    want = prbs_bytes(nbytes)

    # The model's checker over the same bytes, from the same empty history.
    hist, expect_err = 0, 0
    for b in want:
        hist, e = prbs_check(hist, b)
        expect_err += e

    start_clock(dut)

    async def run(page, jam_level=0):
        uio = await reset(dut, tx_code=code_id, prbs=True, disp_page=page,
                          jam_level=jam_level)
        uio.set(loopback=1, jam_en=1 if jam_level else 0, disp_sel=0)
        await ClockCycles(dut.clk, 3)
        seq = []
        for _ in range(nbytes):
            await send_byte(dut, uio, 0x00)   # ui_in is ignored with prbs_en
            seq.append(int(dut.uo_out.value))
        return seq, await read_status(dut, uio)

    got, err_lo = await run(7)
    assert got == want, (
        "PRBS-9 sequence mismatch\n"
        f"  got    {[hex(v) for v in got]}\n"
        f"  expect {[hex(v) for v in want]}")
    assert err_lo == expect_err, (
        f"error counter reads {err_lo}, model predicts {expect_err}. The "
        "first two bytes are unpredictable from an empty history, which is "
        "where a clean run's count comes from.")

    _, bytes_lo = await run(9)
    assert bytes_lo == nbytes, (
        f"byte counter reads {bytes_lo}, expected {nbytes}")
    dut._log.info("PRBS-9: %d bytes bit exact, %d prediction failures, model "
                  "predicted %d", nbytes, err_lo, expect_err)

    _, jammed = await run(7, jam_level=3)
    assert jammed > err_lo, (
        "half the chips were flipped, so the error counter must exceed the "
        f"clean reading: got {jammed} against {err_lo}")
    dut._log.info("jammed at 50 percent chip flip: %d failures against %d "
                  "clean", jammed, err_lo)


# ---------------------------------------------------------------------------
# 28. Receive buffer
# ---------------------------------------------------------------------------

@cocotb.test()
async def test_rx_buffer(dut):
    """Two bytes must queue, pop in order, and set the overflow flag.

    rx_pop_en re-purposes uio[0] from TX_START to RX_POP, so this is a
    receive-only board and the chips have to arrive from outside, which is
    exactly how the feature is meant to be used.

    Nothing here assumes WHICH byte reaches the head of the buffer.  The
    serial search consumes however many early frames it needs before the
    first one is delivered, so the identity of the first delivered byte is
    not a property of the buffer.  What is a property of the buffer is its
    depth, that a full buffer drops rather than overwrites, and that bytes
    come out in the order they went in, and those are what is checked.
    """
    code_id = 17
    payload = [0xC3, 0x96, 0x5A, 0x69, 0x3C, 0xA5]
    chips = []
    for v in payload:
        chips.extend(spread_frame(v, code_id, framed=True))

    start_clock(dut)
    uio = await reset(dut, tx_code=code_id, acq=True, framed=True,
                      rx_pop=True, disp_page=15)
    uio.set(loopback=0, jam_en=0, disp_sel=0)
    await ClockCycles(dut.clk, 3)

    await drive_chips(dut, uio, chips, epoch=False)
    await ClockCycles(dut.clk, 4)

    st = page_last_fields(await read_status(dut, uio))
    head = int(dut.uo_out.value)
    dut._log.info("after %d frames: count=%d overflow=%d head=%#04x",
                  len(payload), st["fifo_cnt"], st["fifo_ovf"], head)

    assert st["fifo_cnt"] == 2, (
        "the buffer is two deep and more than two frames arrived, so it must "
        f"hold 2, not {st['fifo_cnt']}")
    assert st["fifo_ovf"] == 1, (
        "more frames arrived than the buffer holds and nothing was popped, "
        "so the overflow flag must be set")
    assert head in payload[:-1], (
        f"the head {head:#04x} is not one of the transmitted bytes, so the "
        "buffer is not holding what it received")

    # Popping must yield the byte that followed it in transmission order.
    expect_next = payload[payload.index(head) + 1]
    uio.set(tx_start=1)
    await ClockCycles(dut.clk, 4)
    uio.set(tx_start=0)
    await ClockCycles(dut.clk, 4)

    got_next = int(dut.uo_out.value)
    assert got_next == expect_next, (
        f"after popping {head:#04x} the head must be the next byte sent, "
        f"{expect_next:#04x}, got {got_next:#04x}")
    st = page_last_fields(await read_status(dut, uio))
    assert st["fifo_cnt"] == 1, f"count must fall to 1, got {st['fifo_cnt']}"
    dut._log.info("queued %#04x then %#04x and popped them in that order, "
                  "count fell to %d", head, got_next, st["fifo_cnt"])


# ---------------------------------------------------------------------------
# 29. The extended jammer kinds
# ---------------------------------------------------------------------------

@cocotb.test()
async def test_jammer_kinds(dut):
    """Each jammer kind must produce the density the model predicts.

    The broadband, pulsed and periodic kinds depend only on the jammer's own
    state, so their densities are predicted exactly.  The follower kind
    depends on the chip stream it is replaying, and its point is not its
    density but that a jammer using the victim's own code gains nothing from
    doing so, which is checked by requiring the link to survive it.
    """
    code_id = 29
    nclk = 4000
    level = 6                      # 31.25 percent of chips

    start_clock(dut)
    for kind, name, duty in ((JAM_BROADBAND, "broadband", 0),
                             (JAM_PULSED, "pulsed", 2),
                             (JAM_PERIODIC, "periodic", 0)):
        uio = await reset(dut, tx_code=code_id, jam_level=level,
                          jam_kind=kind, pulse_duty=duty, disp_page=2)
        uio.set(loopback=1, jam_en=1, disp_sel=1)
        await ClockCycles(dut.clk, 3)

        hits = 0
        for _ in range(nclk):
            await FallingEdge(dut.clk)
            hits += (int(dut.uo_out.value) >> 6) & 1

        ref = Jammer(jam_level=level, kind=kind, pulse_duty=duty)
        predicted = sum(ref.step() for _ in range(nclk))
        dut._log.info("%-9s density measured %.4f, model %.4f",
                      name, hits / nclk, predicted / nclk)
        assert abs(hits - predicted) <= max(8, 0.1 * predicted), (
            f"{name} jammer density {hits}/{nclk} does not match the model's "
            f"{predicted}/{nclk}")

    # The follower replays the chip stream back after a delay.  It is the
    # matched attack, and it must fail: a Gold code correlated against a
    # shifted copy of itself gives at most t(n), never the code length.
    for delay in (0, 3):
        uio = await reset(dut, tx_code=code_id, jam_level=7,
                          jam_kind=JAM_FOLLOWER, fol_delay=delay)
        uio.set(loopback=1, jam_en=1, disp_sel=0)
        await ClockCycles(dut.clk, 3)
        bad = 0
        for value in (0xA5, 0x5A, 0x3C, 0xC3):
            await send_byte(dut, uio, value)
            bad += bin(int(dut.uo_out.value) ^ value).count("1")
        dut._log.info("follower jammer, replay delay %d chips: %d bit "
                      "errors in 32", delay + 1, bad)
        assert bad == 0, (
            "the matched attack must gain nothing, got "
            f"{bad} bit errors at a replay delay of {delay + 1} chips")


# ---------------------------------------------------------------------------
# 30. A frame arriving on the same clock as a pop
# ---------------------------------------------------------------------------

@cocotb.test()
async def test_rx_buffer_push_pop_same_clock(dut):
    """A byte arriving on the very clock the host pops must not corrupt the
    queue.

    This is the one ordering the two-deep buffer has to get right and the one
    a casual test never reaches. A push needs the count below two and a pop
    needs it above zero, so the two can only coincide at a count of exactly
    one, and at a count of one only the head holds a valid byte. Sliding the
    tail down in that case would put stale data at the head and leave the
    arriving byte in a slot the count says is empty, which shows up as a
    duplicate byte in the delivered stream rather than as a lost one.

    Finding the coincidence is deterministic rather than lucky. The receiver
    is fed a fixed stream, so the clock on which the first byte is delivered
    is fixed too, and every later frame lands one frame period after it. The
    test measures that cycle, then re-runs and places the pop edge so it
    arrives at the next frame boundary, sweeping the few cycles of
    synchroniser delay to be sure of landing on it.
    """
    code_id = 17
    payload = [0xC3, 0x96, 0x5A, 0x69, 0x3C, 0xA5, 0x4B, 0xB4]
    chips = []
    for v in payload:
        chips.extend(spread_frame(v, code_id, framed=True))
    frame_chips = 16 * L          # 8 sync bits plus 8 payload bits

    start_clock(dut)

    # Pass one: no pops at all, just to learn when the first byte lands.
    uio = await reset(dut, tx_code=code_id, acq=True, framed=True,
                      rx_pop=True)
    uio.set(loopback=0, jam_en=0, disp_sel=0)
    await ClockCycles(dut.clk, 3)

    seen_at = []

    def watch_first(i, byte_val, seen_at=seen_at):
        if not seen_at and byte_val in payload:
            seen_at.append(i)

    await drive_chips(dut, uio, chips, epoch=False, watch=watch_first)
    assert seen_at, "no byte was delivered, so the test cannot position a pop"
    first_cycle = seen_at[0]
    dut._log.info("first byte delivered at stream cycle %d, frame period %d",
                  first_cycle, frame_chips)

    # Pass two: aim a pop edge at the next frame boundary. The pin passes
    # through two synchroniser flops and an edge detector, so sweep the lead
    # to be certain one of the runs lands exactly on frame_done.
    worst = None
    for lead in range(1, 7):
        target = first_cycle + frame_chips - lead
        uio = await reset(dut, tx_code=code_id, acq=True, framed=True,
                          rx_pop=True)
        uio.set(loopback=0, jam_en=0, disp_sel=0)
        await ClockCycles(dut.clk, 3)


        seq = []

        def watch_pop(i, byte_val, seq=seq, target=target):
            # Hold the pop pin high for two cycles from the target, which
            # produces exactly one rising edge at the intended clock.
            uio.set(tx_start=1 if target <= i < target + 2 else 0)
            # Record EVERY transition. An earlier version of this test
            # filtered to payload values only, which hid the very defect it
            # was written to find, because a corrupted head reads as the
            # reset value.
            if not seq or seq[-1] != byte_val:
                seq.append(byte_val)

        await drive_chips(dut, uio, chips, epoch=False, watch=watch_pop)
        uio.set(tx_start=0)

        # The invariant: once a real byte has reached the head, the head must
        # never afterwards show a value that was never transmitted. A stale
        # slot promoted to the head violates that, and it shows up as the
        # reset value reappearing.
        delivered = [v for v in seq if v in payload]
        bad = None
        if delivered:
            for v in seq[seq.index(delivered[0]):]:
                if v not in payload:
                    bad = v
                    break
        order = [payload.index(v) for v in delivered]
        ordered = all(b > a for a, b in zip(order, order[1:]))

        dut._log.info("lead %d: head sequence %s%s", lead,
                      [hex(v) for v in seq],
                      "" if (bad is None and ordered) else "   CORRUPT")
        if worst is None and (bad is not None or not ordered):
            worst = (lead, [hex(v) for v in seq], bad)

    assert worst is None, (
        "a byte arriving on the same clock as a pop corrupted the queue: at a "
        f"pop lead of {worst[0]} cycles the head sequence was {worst[1]}"
        + (f", which promoted the never-transmitted value {worst[2]:#04x} to "
           "the head" if worst[2] is not None
           else ", which reorders the payload"))
    dut._log.info("push and pop on the same clock preserved the queue at "
                  "every lead tested")
