# SPDX-FileCopyrightText: (c) 2026 Marc Boudames
# SPDX-License-Identifier: Apache-2.0
"""
Bit-exact software model of the JAMGUARD DSSS core.

This file is the *reference*: the cocotb tests compare the RTL against it,
and it is also runnable on its own to prove the coding-theory claims that the
hardware's margins are derived from:

    python jamguard_model.py

Every convention here mirrors src/project.v exactly -- the LFSR shift
direction, which bit is the output tap, the MSB-first bit order, and the
decision thresholds.  If you change one, change the other.
"""

# ---------------------------------------------------------------------------
# Gold code generator
# ---------------------------------------------------------------------------

N = 5                 # LFSR length
L = 2 ** N - 1        # code length in chips = 31
SEED_A = 0x01         # fixed seed of LFSR-A

TAPS_A = (4, 1)          # x^5 + x^2 + 1
TAPS_B = (4, 3, 2, 1)    # x^5 + x^4 + x^3 + x^2 + 1

# Decision thresholds, identical to the RTL localparams.
#
# The lock window is |R| >= 19, i.e. M >= 25 or M <= 6.  That is deliberately
# far above the aligned cross-correlation bound of 9: a free-running receiver
# also sees *misaligned* windows, whose partial sums are not covered by the
# Gold bound and reach |R| = 17.  See worst_misaligned_correlation().
LOCK_HI = 25
LOCK_LO = 6

# Jammer thresholds indexed by jam_level; flip probability is thresh/32.
# Chosen so the four levels land in the four distinct regimes:
#   0,1 -> lock holds and data is clean
#   2   -> lock is lost but the majority vote still decodes
#   3   -> half the chips flip and the link breaks
JAM_THRESH = (2, 4, 11, 16)


def _mseq(taps, seed, length):
    """Fibonacci LFSR in the exact form the RTL uses.

    RTL:  out = s[0];  s <= {s[3:0], s[4] ^ s[1] ^ ...}

    So the output tap is the *low* bit, the register shifts toward higher
    indices, and the feedback lands in bit 0.
    """
    reg = [(seed >> i) & 1 for i in range(N)]   # reg[i] is s[i]
    out = []
    for _ in range(length):
        out.append(reg[0])
        fb = 0
        for t in taps:
            fb ^= reg[t]
        reg = [fb] + reg[0:N - 1]               # {s[3:0], fb}
    return out


def lfsr_period(taps, seed=1):
    """Period of an LFSR, used to prove both polynomials are primitive."""
    reg = tuple((seed >> i) & 1 for i in range(N))
    seen, n = set(), 0
    while reg not in seen:
        seen.add(reg)
        n += 1
        fb = 0
        for t in taps:
            fb ^= reg[t]
        reg = (fb,) + reg[0:N - 1]
    return n


M_SEQ_A = _mseq(TAPS_A, SEED_A, L)


def gold_code(code_id):
    """The 31-chip Gold code selected by code_id (the seed of LFSR-B).

    code_id 0 is a dead LFSR state and the hardware forces it to 1, so this
    does the same.
    """
    if code_id == 0:
        code_id = 1
    b = _mseq(TAPS_B, code_id, L)
    return [M_SEQ_A[k] ^ b[k] for k in range(L)]


# ---------------------------------------------------------------------------
# Correlation helpers (the +-1 domain)
# ---------------------------------------------------------------------------

def bpsk(bits):
    """Map the logic domain to the signal domain: 0 -> +1, 1 -> -1."""
    return [1 - 2 * b for b in bits]


def correlate(rx_chips, local_code):
    """Signed correlation R = sum of products, over one 31-chip window."""
    x, y = bpsk(rx_chips), bpsk(local_code)
    return sum(a * b for a, b in zip(x, y))


def match_count(rx_chips, local_code):
    """Number of agreeing chips, M.  Related to R by R = 2M - L."""
    return sum(1 for a, b in zip(rx_chips, local_code) if a == b)


def cross_correlate(c1, c2, shift):
    """Correlation of c1 against c2 cyclically shifted by `shift` chips."""
    x, y = bpsk(c1), bpsk(c2)
    return sum(x[k] * y[(k + shift) % L] for k in range(L))


def worst_misaligned_correlation(tx_ids=None, rx_ids=None, data_vals=None):
    """Worst |R| a free-running receiver can see from a foreign transmitter.

    The classic Gold bound of t(n) = 9 applies only to windows aligned with a
    code period.  A receiver that has not yet acquired slides its window
    arbitrarily, so it also sees windows straddling two data bits.  Those are
    sums of two *partial* correlations and the bound does not cover them --
    they reach |R| = 17, which is why the lock threshold is set at 19 rather
    than at something just above 9.
    """
    tx_ids = tx_ids or range(1, L + 1, 3)
    rx_ids = rx_ids or range(1, L + 1, 3)
    data_vals = data_vals or (0x00, 0xA5, 0x5A, 0xC3, 0x6B, 0xF0)

    worst = 0
    for tx_id in tx_ids:
        for rx_id in rx_ids:
            if rx_id == tx_id:
                continue
            rxcode = gold_code(rx_id)
            for data in data_vals:
                stream = spread_byte(data, tx_id) * 2
                for off in range(8 * L):
                    win = stream[off:off + L]
                    m = sum(1 for a, b in zip(win, rxcode) if a == b)
                    worst = max(worst, abs(2 * m - L))
    return worst


# ---------------------------------------------------------------------------
# Transmitter model
# ---------------------------------------------------------------------------

def spread_bit(data_bit, code_id):
    """One data bit becomes L chips: the code, inverted when the bit is 1."""
    return [c ^ data_bit for c in gold_code(code_id)]


def spread_byte(byte_val, code_id):
    """8*31 = 248 chips, MSB of the byte transmitted first."""
    chips = []
    for i in range(7, -1, -1):
        chips.extend(spread_bit((byte_val >> i) & 1, code_id))
    return chips


# ---------------------------------------------------------------------------
# Receiver model
# ---------------------------------------------------------------------------

def despread_window(rx_chips, code_id):
    """Despread one 31-chip window.

    Returns (bit, M, locked_this_window) using the same rules as the RTL:
        bit    = 1 when M < 16            (RTL: ~m_final[4])
        locked = M >= LOCK_HI or M <= LOCK_LO
    """
    m = match_count(rx_chips, gold_code(code_id))
    return (1 if m < 16 else 0), m, (m >= LOCK_HI or m <= LOCK_LO)


def despread_byte(rx_chips, code_id):
    """Despread 248 chips back into a byte, MSB first."""
    assert len(rx_chips) == 8 * L
    val = 0
    for i in range(8):
        bit, _, _ = despread_window(rx_chips[i * L:(i + 1) * L], code_id)
        val = (val << 1) | bit
    return val


# ---------------------------------------------------------------------------
# Jammer model (bit-exact with the RTL's 8-bit LFSR)
# ---------------------------------------------------------------------------

class Jammer:
    """x^8 + x^6 + x^5 + x^4 + 1, reset state 0xA5, advanced every clock."""

    RESET = 0xA5

    def __init__(self, jam_level=0, enabled=True):
        self.state = self.RESET
        self.thresh = JAM_THRESH[jam_level]
        self.enabled = enabled

    def step(self):
        """Return this clock's flip decision, then advance the state."""
        hit = self.enabled and (self.state & 0x1F) < self.thresh
        fb = ((self.state >> 7) ^ (self.state >> 5) ^
              (self.state >> 4) ^ (self.state >> 3)) & 1
        self.state = ((self.state << 1) | fb) & 0xFF
        return 1 if hit else 0

    @classmethod
    def period(cls):
        s, start, n = cls.RESET, cls.RESET, 0
        while True:
            fb = ((s >> 7) ^ (s >> 5) ^ (s >> 4) ^ (s >> 3)) & 1
            s = ((s << 1) | fb) & 0xFF
            n += 1
            if s == start:
                return n


# ---------------------------------------------------------------------------
# Self-check: the coding theory the hardware's margins rest on
# ---------------------------------------------------------------------------

def _self_check(verbose=True):
    import math

    ok = True

    def check(label, cond, detail=""):
        nonlocal ok
        ok = ok and bool(cond)
        if verbose:
            print(f"  [{'PASS' if cond else 'FAIL'}] {label}{detail}")

    if verbose:
        print("JAMGUARD model self-check")
        print("-" * 64)

    pa, pb = lfsr_period(TAPS_A), lfsr_period(TAPS_B)
    check("LFSR-A is maximal length", pa == L, f"  (period {pa}, want {L})")
    check("LFSR-B is maximal length", pb == L, f"  (period {pb}, want {L})")

    codes = {cid: gold_code(cid) for cid in range(1, L + 1)}
    check("31 seeds give 31 distinct codes",
          len({tuple(c) for c in codes.values()}) == L)

    # Autocorrelation sidelobes and cross-correlations must both stay inside
    # the preferred-pair bound t(n) = 1 + 2^((n+1)/2).
    t_n = 1 + 2 ** ((N + 1) // 2)
    side, cross = set(), set()
    for cid, c in codes.items():
        for s in range(1, L):
            side.add(cross_correlate(c, c, s))
    ids = sorted(codes)
    for i_idx, i in enumerate(ids):
        for j in ids[i_idx + 1:]:
            for s in range(L):
                cross.add(cross_correlate(codes[i], codes[j], s))

    worst = max(max(abs(v) for v in side), max(abs(v) for v in cross))
    check("correlation bound t(n) = 9 is met", worst == t_n,
          f"  (worst |R| = {worst})")
    check("correlation value set is {-9,-1,7}",
          sorted(side | cross) == [-9, -1, 7],
          f"  (got {sorted(side | cross)})")

    # Every impostor window must land strictly inside the lock thresholds.
    # A data bit of 1 inverts the code, so R and -R are both reachable and the
    # match-count set is closed under M -> L - M.
    impostor_R = (side | cross) | {-r for r in (side | cross)}
    impostor_M = sorted({(r + L) // 2 for r in impostor_R})
    check("impostor match counts never reach lock",
          all(LOCK_LO < m < LOCK_HI for m in impostor_M),
          f"  (impostor M = {impostor_M}, lock needs M<={LOCK_LO} or M>={LOCK_HI})")

    # Round trip over every byte, clean channel.
    bad = [v for v in range(256) if despread_byte(spread_byte(v, 7), 7) != v]
    check("all 256 bytes survive a clean round trip", not bad)

    # Worst-case jam margin: the decision is a majority vote over 31 chips.
    margins = []
    for j in range(L + 1):
        chips = spread_bit(0, 7)
        for k in range(j):
            chips[k] ^= 1
        bit, m, _ = despread_window(chips, 7)
        margins.append((j, m, bit))
    first_fail = next(j for j, _, bit in margins if bit != 0)
    check("tolerates up to 15 corrupted chips", first_fail == 16,
          f"  (first failure at {first_fail} of {L} chips)")

    # The acquisition threshold must clear the *misaligned* worst case too,
    # not merely the aligned Gold bound.
    worst_mis = worst_misaligned_correlation()
    lock_R = L - 2 * LOCK_LO
    check("lock threshold clears misaligned foreign windows",
          worst_mis < lock_R,
          f"  (worst misaligned |R| = {worst_mis}, lock needs |R| >= {lock_R})")

    check("jammer LFSR is maximal length", Jammer.period() == 255,
          f"  (period {Jammer.period()})")
    check("jam pattern never aligns with the code", math.gcd(255, L) == 1)

    if verbose:
        print("-" * 64)
        print(f"  code length L          = {L} chips")
        print(f"  processing gain        = 10*log10({L}) = "
              f"{10 * math.log10(L):.2f} dB")
        print(f"  jam tolerance (decode) = {first_fail - 1}/{L} chips = "
              f"{100 * (first_fail - 1) / L:.1f}%")
        print(f"  jam tolerance (lock)   = {LOCK_LO}/{L} chips")
        print(f"  worst misaligned |R|   = {worst_mis}/{L} "
              f"(lock needs >= {lock_R})")
        print(f"  worst impostor |R|     = {worst}/{L}")
        print("-" * 64)

        print("\n  jam_level   P(flip)   E[flips]/31   expected M   outcome")
        for lvl, thr in enumerate(JAM_THRESH):
            p = thr / 32
            ej = p * L
            em = L - ej
            if em >= LOCK_HI:
                outcome = "locks and decodes"
            elif em >= 16:
                outcome = "decodes, loses lock"
            else:
                outcome = "breaks down"
            print(f"      {lvl}      {p:6.1%}      {ej:5.1f}       "
                  f"{em:5.1f}      {outcome}")
        print()
        print("RESULT:", "ALL CHECKS PASSED" if ok else "FAILURES PRESENT")

    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if _self_check() else 1)
