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

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge

from jamguard_model import (
    L,
    JAM_THRESH,
    LOCK_HI,
    LOCK_LO,
    despread_byte,
    gold_code,
    spread_byte,
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


async def reset(dut, tx_code, jam_level=0, acq=False, rx_code=0):
    """Reset the DUT and latch a configuration off the pins.

    The configuration flops track ui_in / uio_in[5:0] the whole time rst_n is
    low, so the value present when reset releases is what gets kept.
    """
    uio = Uio(dut)
    dut.ena.value = 1
    dut.rst_n.value = 0
    dut.ui_in.value = (
        ((1 if acq else 0) << 7) | ((jam_level & 3) << 5) | (tx_code & 0x1F)
    )
    uio.val = rx_code & 0x1F
    dut.uio_in.value = uio.val

    await ClockCycles(dut.clk, 6)
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


async def send_byte(dut, uio, value, slack=12):
    """Pulse tx_start, then wait out the whole 8 x 31 chip frame."""
    dut.ui_in.value = value
    uio.set(tx_start=1)
    await FallingEdge(dut.clk)
    await FallingEdge(dut.clk)
    uio.set(tx_start=0)
    await ClockCycles(dut.clk, 8 * L + slack)
    await FallingEdge(dut.clk)


async def drive_chips(dut, uio, chips, epoch=True, watch=None):
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
        uio.set(rx_chip=c, rx_epoch=1 if (epoch and i % L == 0) else 0)
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
