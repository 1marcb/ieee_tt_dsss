# SPDX-FileCopyrightText: (c) 2026 Marc Boudames
# SPDX-License-Identifier: Apache-2.0
"""
Bit-exact software model of the JAMGUARD DSSS core.

This file is the *reference*: the cocotb tests compare the RTL against it,
and it is also runnable on its own to prove the coding-theory claims that the
hardware's margins are derived from:

    python jamguard_model.py

Every convention here mirrors src/project.v exactly, including the LFSR shift
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

# Jammer thresholds indexed by {jam_fine, jam_level}, flip probability is
# thresh/32.  The coarse ladder (0..3) lands in the four distinct regimes:
#   0,1 -> lock holds and data is clean
#   2   -> lock is lost but the majority vote still mostly decodes
#   3   -> half the chips flip and the link breaks
# The fine ladder (4..7) is spaced across the decode cliff, where FEC matters.
JAM_THRESH = (2, 4, 11, 16, 8, 9, 10, 12)

# Frame sync word: 7-bit Barker 1110010 followed by a 0.
SYNC = 0xE4

# Frequency hopping
HOP_TAPS = (5, 4)        # x^6 + x^5 + 1
PARK_CH = 1              # channel a non-hopping transmitter sits on
PBJ_BAND_TOP = (1, 2, 4, 6)   # jammed when channel[5:3] < this


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


def worst_misaligned_correlation(same_code=False):
    """Worst |R| a free-running receiver can see, searched exhaustively.

    The classic Gold bound of t(n) = 9 applies only to windows aligned with a
    code period.  A receiver that has not yet acquired slides its window
    arbitrarily, so it also sees windows straddling two data bits.  Those are
    sums of two *partial* correlations and the bound does not cover them --
    they reach |R| = 17, which is why the lock threshold is set at 19 rather
    than at something just above 9.

    Any such window is completely determined by the transmit code, the
    receive code, the chip offset and the two adjacent data bits, because the
    first L-o chips carry one bit and the rest carry the next.  That makes
    the whole space only L*L*L*4 windows, so this enumerates every one of
    them instead of sampling.  An earlier version sampled a third of the
    codes and six of the 256 data values, and skipped same_code entirely.

    same_code=False searches every ordered pair of different codes.
    same_code=True searches each code against itself at every wrong phase,
    which is the case acquisition actually has to survive: once locked, the
    slip search stops, so a false lock at the wrong phase of the right code
    would never self correct.
    """
    codes = {c: gold_code(c) for c in range(1, L + 1)}
    worst = 0
    locking = 0
    for tx_id in range(1, L + 1):
        gtx = codes[tx_id]
        rx_ids = [tx_id] if same_code else [r for r in range(1, L + 1)
                                            if r != tx_id]
        offsets = range(1, L) if same_code else range(L)
        for rx_id in rx_ids:
            grx = codes[rx_id]
            for off in offsets:
                for b0 in (0, 1):
                    for b1 in (0, 1):
                        m = 0
                        for k in range(L):
                            idx = off + k
                            bit = b0 if idx < L else b1
                            if (gtx[idx % L] ^ bit) == grx[k]:
                                m += 1
                        if m >= LOCK_HI or m <= LOCK_LO:
                            locking += 1
                        r = abs(2 * m - L)
                        if r > worst:
                            worst = r
    return worst, locking


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
# Jammer model (bit-exact with the RTL's 17-bit LFSR)
# ---------------------------------------------------------------------------

class Jammer:
    """x^17 + x^14 + 1, reset state 0x1ACE1, advanced five steps per clock.

    Five steps per clock means the five low bits compared against the
    threshold are all fresh on every chip, so chip flips are independent
    with probability thresh/32: a binary symmetric channel.
    """

    RESET = 0x1ACE1
    BITS = 17

    def __init__(self, jam_level=0, enabled=True):
        self.state = self.RESET
        self.thresh = JAM_THRESH[jam_level]
        self.enabled = enabled

    @staticmethod
    def _step1(s):
        fb = ((s >> 16) ^ (s >> 13)) & 1
        return ((s << 1) | fb) & 0x1FFFF

    @classmethod
    def advance(cls, s):
        for _ in range(5):
            s = cls._step1(s)
        return s

    def draw(self):
        """This clock's 5-bit uniform draw, then advance the state."""
        v = self.state & 0x1F
        self.state = self.advance(self.state)
        return v

    def step(self):
        """Return this clock's flip decision, then advance the state."""
        return 1 if (self.draw() < self.thresh and self.enabled) else 0

    @classmethod
    def period(cls):
        """Period of the underlying one-step LFSR (should be 2^17 - 1)."""
        s, n = cls._step1(cls.RESET), 1
        while s != cls.RESET:
            s, n = cls._step1(s), n + 1
        return n


# ---------------------------------------------------------------------------
# SECDED(8,4) extended Hamming code, bit-exact with the RTL
# ---------------------------------------------------------------------------
#
# Codeword c[7:0] = {d3, d2, d1, d0, p2, p1, p0, q}

def secded_enc(nibble):
    d = [(nibble >> i) & 1 for i in range(4)]
    p0 = d[0] ^ d[1] ^ d[3]
    p1 = d[0] ^ d[2] ^ d[3]
    p2 = d[1] ^ d[2] ^ d[3]
    upper = (nibble << 4) | (p2 << 3) | (p1 << 2) | (p0 << 1)
    q = bin(upper).count("1") & 1
    return upper | q


def secded_dec(c):
    """Returns (data_nibble, corrected, uncorrectable), mirroring the RTL."""
    b = [(c >> i) & 1 for i in range(8)]
    syn = ((b[1] ^ b[4] ^ b[5] ^ b[7])
           | (b[2] ^ b[4] ^ b[6] ^ b[7]) << 1
           | (b[3] ^ b[5] ^ b[6] ^ b[7]) << 2)
    q = bin(c).count("1") & 1
    flip = 0
    if q:
        flip = {3: 0b0001, 5: 0b0010, 6: 0b0100, 7: 0b1000}.get(syn, 0)
    return ((c >> 4) ^ flip) & 0xF, bool(q), bool(syn != 0 and not q)


def interleave(h, l):
    """16 bits, H on the odd positions and L on the even (bit 15 sent first)."""
    v = 0
    for i in range(8):
        v |= ((h >> i) & 1) << (2 * i + 1)
        v |= ((l >> i) & 1) << (2 * i)
    return v


def deinterleave(v):
    h = l = 0
    for i in range(8):
        h |= ((v >> (2 * i + 1)) & 1) << i
        l |= ((v >> (2 * i)) & 1) << i
    return h, l


def fec_encode_byte(byte_val):
    """The 16-bit payload the transmitter sends for one byte with FEC on."""
    return interleave(secded_enc(byte_val >> 4), secded_enc(byte_val & 0xF))


def fec_decode_payload(v):
    """Returns (byte, raw_byte, fix_bits, uncorrectable) like the RTL."""
    h, l = deinterleave(v)
    dh, fh, eh = secded_dec(h)
    dl, fl, el = secded_dec(l)
    return ((dh << 4) | dl, ((h >> 4) << 4) | (l >> 4),
            (int(fh) << 1) | int(fl), eh or el)


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------

def frame_bits(byte_val, fec=False, framed=False):
    """The bits of one frame, in transmission order."""
    bits = []
    if framed:
        bits += [(SYNC >> (7 - i)) & 1 for i in range(8)]
    if fec:
        v = fec_encode_byte(byte_val)
        bits += [(v >> (15 - i)) & 1 for i in range(16)]
    else:
        bits += [(byte_val >> (7 - i)) & 1 for i in range(8)]
    return bits


def spread_bits(bits, code_id):
    chips = []
    for b in bits:
        chips.extend(spread_bit(b, code_id))
    return chips


def spread_frame(byte_val, code_id, fec=False, framed=False):
    return spread_bits(frame_bits(byte_val, fec, framed), code_id)


def despread_bits(rx_chips, code_id):
    """Aligned hard decisions, one per 31-chip window."""
    return [despread_window(rx_chips[i:i + L], code_id)[0]
            for i in range(0, len(rx_chips) - L + 1, L)]


def sync_hit(window8):
    """Hamming distance <= 1 from SYNC, exactly as the RTL tests it."""
    x = (window8 ^ SYNC) & 0xFF
    return (x & (x - 1)) & 0xFF == 0


class FrameReceiver:
    """Bit-level model of the receiver's framing and FEC, fed decided bits."""

    def __init__(self, fec=False, framed=False):
        self.fec, self.framed = fec, framed
        self.shift, self.cnt, self.inframe = 0, 0, False
        self.out = []          # (byte, raw, fix, err) per delivered frame

    def push(self, bit):
        nxt = ((self.shift << 1) | bit) & 0xFFFF
        last = 15 if self.fec else 7
        if self.framed and not self.inframe:
            if sync_hit(nxt & 0xFF):
                self.inframe, self.cnt = True, 0
            else:
                self.shift = nxt
        elif self.cnt == last:
            if self.fec:
                self.out.append(fec_decode_payload(nxt))
            else:
                self.out.append((nxt & 0xFF, nxt & 0xFF, 0, False))
            self.cnt, self.inframe, self.shift = 0, False, 0
        else:
            self.shift, self.cnt = nxt, self.cnt + 1


# ---------------------------------------------------------------------------
# Frequency hopping
# ---------------------------------------------------------------------------

def hop_step(s):
    fb = 0
    for t in HOP_TAPS:
        fb ^= (s >> t) & 1
    return ((s << 1) | fb) & 0x3F


def hop_seed(code_id):
    if code_id == 0:
        code_id = 1
    return ((code_id << 1) | 1) & 0x3F


def hop_channels(code_id, n_bits, fast=False, fhss=True):
    """Channel of every chip of the first n_bits bits sent after reset.

    The hop state is seeded at reset and carries on across frames, so this
    covers any number of back-to-back frames too.
    """
    if not fhss:
        return [PARK_CH] * (n_bits * L)
    s, out = hop_seed(code_id), []
    for _ in range(n_bits):
        for k in range(L):
            out.append(s)
            if fast or k == L - 1:
                s = hop_step(s)
    return out


def hop_period():
    s0 = hop_seed(1)
    s, n = hop_step(s0), 1
    while s != s0:
        s, n = hop_step(s), n + 1
    return n


def in_band(channel, band):
    return (channel >> 3) < PBJ_BAND_TOP[band]


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
    # not merely the aligned Gold bound.  Both searches below are exhaustive.
    lock_R = L - 2 * LOCK_LO
    worst_mis, lock_mis = worst_misaligned_correlation(same_code=False)
    check("lock threshold clears misaligned foreign windows",
          worst_mis < lock_R and lock_mis == 0,
          f"  (worst |R| = {worst_mis}, {lock_mis} would lock, "
          f"lock needs |R| >= {lock_R})")

    # The case acquisition really depends on: the right code at the wrong
    # phase.  Once locked the slip search stops, so a false lock here would
    # deliver wrong bytes forever with no self recovery.
    worst_self, lock_self = worst_misaligned_correlation(same_code=True)
    check("lock threshold clears the right code at a wrong phase",
          worst_self < lock_R and lock_self == 0,
          f"  (worst |R| = {worst_self}, {lock_self} would lock, "
          f"lock needs |R| >= {lock_R})")

    jp = Jammer.period()
    check("jammer LFSR is maximal length", jp == 2 ** 17 - 1,
          f"  (period {jp})")
    check("jam pattern never aligns with the code", math.gcd(jp, L) == 1)

    # --- SECDED(8,4) ------------------------------------------------------
    cws = [secded_enc(n) for n in range(16)]
    dmin = min(bin(a ^ b).count("1")
               for i, a in enumerate(cws) for b in cws[i + 1:])
    check("SECDED minimum distance is 4", dmin == 4, f"  (d_min = {dmin})")
    single_ok = all(secded_dec(c ^ (1 << k))[0] == n and
                    secded_dec(c ^ (1 << k))[1] and
                    not secded_dec(c ^ (1 << k))[2]
                    for n, c in enumerate(cws) for k in range(8))
    check("SECDED corrects every single-bit error", single_ok)
    double_ok = all(secded_dec(c ^ (1 << j) ^ (1 << k))[2]
                    for c in cws for j in range(8) for k in range(j + 1, 8))
    check("SECDED flags every double-bit error", double_ok)
    clean_ok = all(secded_dec(c) == (n, False, False)
                   for n, c in enumerate(cws))
    check("SECDED passes clean codewords untouched", clean_ok)
    burst_ok = all(
        fec_decode_payload(fec_encode_byte(v) ^ (0b11 << k))[0] == v
        for v in range(256) for k in range(15))
    check("interleaving corrects every 2-bit burst", burst_ok)

    # --- frame sync word ---------------------------------------------------
    # After a frame the receiver clears its shift register to zero.  While
    # the next sync word is only partly received, the 8-bit window still
    # holds some of those zeros, and it must never be within distance 1 of
    # SYNC.  Neither may any shift of SYNC against its neighbours in a
    # stream of back-to-back frames.
    partial_ok = all(not sync_hit((SYNC >> (8 - k)) & 0xFF)
                     for k in range(1, 8))
    check("a partly received sync word never matches", partial_ok)
    stream_ok = True
    for fec in (False, True):
        for v in range(256):
            rx = FrameReceiver(fec=fec, framed=True)
            for _ in range(3):
                for b in frame_bits(v, fec=fec, framed=True):
                    rx.push(b)
            stream_ok &= [o[0] for o in rx.out] == [v] * 3
    check("framed receiver recovers every byte from a stream", stream_ok)

    # --- frequency hopping -------------------------------------------------
    hp = hop_period()
    check("hop LFSR is maximal length", hp == 63, f"  (period {hp})")

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

        print("\n  level  P(flip)  P(lockable)  raw BER   byte err  "
              "byte err w/ FEC")
        for lvl, thr in enumerate(JAM_THRESH):
            p = thr / 32
            ber = sum(math.comb(L, j) * p ** j * (1 - p) ** (L - j)
                      for j in range(16, L + 1))
            lockable = sum(math.comb(L, j) * p ** j * (1 - p) ** (L - j)
                           for j in range(0, LOCK_LO + 1))
            cw_fail = 1 - ((1 - ber) ** 8 + 8 * ber * (1 - ber) ** 7)
            print(f"  {lvl}{' fine' if lvl >= 4 else '     '}  {p:6.1%}  "
                  f"{lockable:9.1%}   {ber:8.2e}  {1 - (1 - ber) ** 8:8.2e}  "
                  f"{max(0.0, 1 - (1 - cw_fail) ** 2):8.2e}")
        print()
        print("RESULT:", "ALL CHECKS PASSED" if ok else "FAILURES PRESENT")

    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if _self_check() else 1)
