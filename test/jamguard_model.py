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

The core supports two code lengths.  Everything that depends on the length
takes a `fam` argument defaulting to SHORT, the 31-chip family, so the
default behaviour of every function here is the behaviour of the hardware
with len127 clear.
"""

import math

# ---------------------------------------------------------------------------
# Gold code families
# ---------------------------------------------------------------------------

SEED_A = 0x01         # fixed seed of LFSR-A, both lengths


def _mseq(n, taps, seed, length):
    """Fibonacci LFSR in the exact form the RTL uses.

    RTL:  out = s[0];  s <= {s[n-2:0], s[n-1] ^ s[t] ^ ...}

    So the output tap is the *low* bit, the register shifts toward higher
    indices, and the feedback lands in bit 0.  A polynomial term x^k is the
    tap at index k-1, which is why x^5 + x^2 + 1 becomes taps (4, 1).
    """
    reg = [(seed >> i) & 1 for i in range(n)]
    out = []
    for _ in range(length):
        out.append(reg[0])
        fb = 0
        for t in taps:
            fb ^= reg[t]
        reg = [fb] + reg[0:n - 1]
    return out


class Family:
    """One Gold code family: a preferred pair of LFSRs and its thresholds.

    half is the midpoint of the code length rounded up, 2^(n-1), and because
    L = 2^n - 1 is odd the signed correlation R = 2M - L is odd too.  The
    distance of M from the midpoint,

        magn(M) = M - half      when M >= half
                = half - 1 - M  when M <  half

    satisfies |R| = 2 * magn + 1, so magn is monotone in |R| and every
    strength comparison can be made on it.  One tunable per family, lock_mag,
    then fixes both lock thresholds.
    """

    def __init__(self, n, taps_a, taps_b, lock_mag, name):
        self.n = n
        self.L = 2 ** n - 1
        self.taps_a = taps_a
        self.taps_b = taps_b
        self.lock_mag = lock_mag
        self.name = name
        self.half = 2 ** (n - 1)
        self.seq_a = _mseq(n, taps_a, SEED_A, self.L)
        self._codes = {}

    # -- thresholds ---------------------------------------------------------
    @property
    def lock_hi(self):
        """Smallest match count that locks on the high side."""
        return self.half + self.lock_mag

    @property
    def lock_lo(self):
        """Largest match count that locks on the low side."""
        return self.half - 1 - self.lock_mag

    @property
    def lock_r(self):
        """Smallest |R| that locks."""
        return 2 * self.lock_mag + 1

    @property
    def bound(self):
        """The preferred-pair bound t(n) = 1 + 2^((n+1)/2)."""
        return 1 + 2 ** ((self.n + 1) // 2)

    @property
    def gain_db(self):
        return 10 * math.log10(self.L)

    # -- codes --------------------------------------------------------------
    def code(self, code_id):
        """The Gold code selected by code_id, which is the seed of LFSR-B.

        code_id 0 is a dead LFSR state and the hardware forces it to 1, so
        this does the same.  The hardware also masks the seed to n bits, so
        bits above the code length are ignored rather than misinterpreted.
        """
        code_id &= self.L           # the seed mask in the RTL
        if code_id == 0:
            code_id = 1
        if code_id not in self._codes:
            b = _mseq(self.n, self.taps_b, code_id, self.L)
            self._codes[code_id] = [self.seq_a[k] ^ b[k]
                                    for k in range(self.L)]
        return self._codes[code_id]

    def magn(self, m):
        """Distance of a match count from the midpoint.

        Written the way the RTL computes it, with masking and no subtractor,
        so that the identity is tested rather than assumed.
        """
        folded = m if (m & self.half) else ~m
        return folded & (self.half - 1)

    def magn_ref(self, m):
        """The same quantity by the obvious arithmetic, for cross-checking."""
        return (m - self.half) if m >= self.half else (self.half - 1 - m)

    def decide(self, m):
        """The hard decision: 1 when M is below the midpoint."""
        return 0 if (m & self.half) else 1

    def locks(self, m):
        return self.magn(m) >= self.lock_mag

    def mon_seed(self, rx_code_id):
        """The code the cross-correlation monitor runs, as the RTL derives it."""
        s = (rx_code_id + 1) & self.L
        return s if s else 1


# The shipping short family: x^5 + x^2 + 1 and x^5 + x^4 + x^3 + x^2 + 1.
SHORT = Family(5, (4, 1), (4, 3, 2, 1), 9, "L31")

# The long family: x^7 + x^3 + 1 and x^7 + x^3 + x^2 + x + 1, octal 211 and
# 217.  lock_mag 24 puts the lock threshold at |R| >= 49, which clears the
# measured misaligned worst case of 41 by 8 chips, the same 6 percent of the
# code length that the short family's margin of 2 of 31 gives.
LONG = Family(7, (6, 2), (6, 2, 1, 0), 24, "L127")

FAMILIES = (SHORT, LONG)

# ---------------------------------------------------------------------------
# Backward-compatible module-level names, all referring to the short family.
# ---------------------------------------------------------------------------

N = SHORT.n
L = SHORT.L
TAPS_A = SHORT.taps_a
TAPS_B = SHORT.taps_b
LOCK_HI = SHORT.lock_hi          # 25
LOCK_LO = SHORT.lock_lo          # 6
M_SEQ_A = SHORT.seq_a

# Jammer thresholds indexed by {jam_fine, jam_level}, flip probability is
# thresh/32.  The coarse ladder (0..3) lands in the four distinct regimes:
#   0,1 -> lock holds and data is clean
#   2   -> lock is lost but the majority vote still mostly decodes
#   3   -> half the chips flip and the link breaks
# The fine ladder (4..7) is spaced across the decode cliff, where FEC matters.
JAM_THRESH = (2, 4, 11, 16, 8, 9, 10, 12)

# Jammer kinds, matching jam_kind on configuration page 3.
JAM_BROADBAND, JAM_PULSED, JAM_PERIODIC, JAM_FOLLOWER = 0, 1, 2, 3
PULSE_DUTY_TOP = (1, 2, 4, 8)     # of 16

# Frame sync word: 7-bit Barker 1110010 followed by a 0.
SYNC = 0xE4

# Frequency hopping
HOP_TAPS = (5, 4)        # x^6 + x^5 + 1
PARK_CH = 1              # channel a non-hopping transmitter sits on
PBJ_BAND_TOP = (1, 2, 4, 6)   # jammed when channel[5:3] < this

# PRBS-9 bit error rate tester: x^9 + x^5 + 1, period 511.
PRBS_TAPS = (8, 4)
PRBS_RESET = 0x1FF


def lfsr_period(taps, seed=1, n=None):
    """Period of an LFSR, used to prove a polynomial is primitive."""
    if n is None:
        n = max(taps) + 1
    reg = tuple((seed >> i) & 1 for i in range(n))
    seen, k = set(), 0
    while reg not in seen:
        seen.add(reg)
        k += 1
        fb = 0
        for t in taps:
            fb ^= reg[t]
        reg = (fb,) + reg[0:n - 1]
    return k


def gold_code(code_id, fam=SHORT):
    """The Gold code selected by code_id."""
    return fam.code(code_id)


# ---------------------------------------------------------------------------
# Correlation helpers (the +-1 domain)
# ---------------------------------------------------------------------------

def bpsk(bits):
    """Map the logic domain to the signal domain: 0 -> +1, 1 -> -1."""
    return [1 - 2 * b for b in bits]


def correlate(rx_chips, local_code):
    """Signed correlation R = sum of products, over one code period."""
    x, y = bpsk(rx_chips), bpsk(local_code)
    return sum(a * b for a, b in zip(x, y))


def match_count(rx_chips, local_code):
    """Number of agreeing chips, M.  Related to R by R = 2M - L."""
    return sum(1 for a, b in zip(rx_chips, local_code) if a == b)


def cross_correlate(c1, c2, shift):
    """Correlation of c1 against c2 cyclically shifted by `shift` chips."""
    n = len(c2)
    x, y = bpsk(c1), bpsk(c2)
    return sum(x[k] * y[(k + shift) % n] for k in range(n))


def _bits_to_int(bits):
    v = 0
    for i, b in enumerate(bits):
        v |= b << i
    return v


def worst_misaligned_correlation(same_code=False, fam=SHORT):
    """Worst |R| a free-running receiver can see, searched exhaustively.

    The classic Gold bound t(n) applies only to windows aligned with a code
    period.  A receiver that has not yet acquired slides its window
    arbitrarily, so it also sees windows straddling two data bits.  Those are
    sums of two *partial* correlations and the bound does not cover them.
    They reach |R| = 17 of 31 and |R| = 41 of 127, which is why the lock
    thresholds are 19 and 49 and not something just above t(n).

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

    Implemented on integer bitmasks because at L = 127 the enumeration is
    127 * 127 * 127 * 4 windows, and a per-chip Python loop over that is
    thousands of times slower than one popcount per window.
    """
    big_l = fam.L
    mask = (1 << big_l) - 1
    codes = {c: _bits_to_int(fam.code(c)) for c in range(1, big_l + 1)}
    worst = 0
    locking = 0
    for tx_id in range(1, big_l + 1):
        gtx = codes[tx_id]
        rx_ids = ([tx_id] if same_code
                  else [r for r in range(1, big_l + 1) if r != tx_id])
        offsets = range(1, big_l) if same_code else range(big_l)
        for b0 in (0, 1):
            for b1 in (0, 1):
                # Two periods is enough: a window of L chips touches at most
                # two of them.  The first L-o chips carry b0, the rest b1.
                stream = (gtx ^ (mask if b0 else 0)) | \
                         ((gtx ^ (mask if b1 else 0)) << big_l)
                for off in offsets:
                    window = (stream >> off) & mask
                    for rx_id in rx_ids:
                        m = big_l - (window ^ codes[rx_id]).bit_count()
                        if fam.locks(m):
                            locking += 1
                        r = abs(2 * m - big_l)
                        if r > worst:
                            worst = r
    return worst, locking


# ---------------------------------------------------------------------------
# Transmitter model
# ---------------------------------------------------------------------------

def spread_bit(data_bit, code_id, fam=SHORT):
    """One data bit becomes L chips: the code, inverted when the bit is 1."""
    return [c ^ data_bit for c in fam.code(code_id)]


def spread_byte(byte_val, code_id, fam=SHORT):
    """8*L chips, MSB of the byte transmitted first."""
    chips = []
    for i in range(7, -1, -1):
        chips.extend(spread_bit((byte_val >> i) & 1, code_id, fam))
    return chips


# ---------------------------------------------------------------------------
# Receiver model
# ---------------------------------------------------------------------------

def despread_window(rx_chips, code_id, fam=SHORT):
    """Despread one code period.

    Returns (bit, M, locked_this_window) using the same rules as the RTL:
        bit    = 1 when M is below the midpoint   (RTL: one bit of M)
        locked = magn(M) >= lock_mag
    """
    m = match_count(rx_chips, fam.code(code_id))
    return fam.decide(m), m, fam.locks(m)


def despread_byte(rx_chips, code_id, fam=SHORT):
    """Despread 8*L chips back into a byte, MSB first."""
    assert len(rx_chips) == 8 * fam.L
    val = 0
    for i in range(8):
        bit, _, _ = despread_window(rx_chips[i * fam.L:(i + 1) * fam.L],
                                    code_id, fam)
        val = (val << 1) | bit
    return val


def early_late(rx_chips, code_id, offset, fam=SHORT):
    """The three correlator magnitudes the RTL computes over one window.

    `offset` is the alignment error e = local index minus signal index, so
    e = 0 is perfect alignment, e = +1 means the local replica runs one chip
    ahead of the signal, and e = -1 means it runs one chip behind.

    Returns (mag_on, mag_adv, mag_ret).  The advanced replica is the code one
    chip ahead of the local index and the retarded replica is the code one
    chip behind, which is exactly what the hardware's three accumulators see.
    """
    big_l = fam.L
    code = fam.code(code_id)
    mags = []
    for shift in (0, 1, -1):          # on-time, advanced, retarded
        m = 0
        for k in range(big_l):
            local = code[(k + offset + shift) % big_l]
            if rx_chips[k] == local:
                m += 1
        mags.append(fam.magn(m))
    return tuple(mags)


def track_decision(mag_on, mag_adv, mag_ret, fam=SHORT):
    """The RTL's chip-phase discriminator.

    Returns +1 to shrink the next window, which advances the local phase by
    one chip, -1 to hold for one chip, which retards it, and 0 for no
    correction.  Expressed exactly as the RTL's two comparisons.

    The test is "would a neighbour have locked when the on-time correlator
    did not", not a comparison between the three magnitudes.  At alignment
    the on-time correlator locks and nothing fires.  Under heavy jamming all
    three collapse below the threshold and nothing fires either, which is the
    behaviour that stops the loop chasing noise.
    """
    on_locks = mag_on >= fam.lock_mag
    if (mag_adv >= fam.lock_mag) and not on_locks:
        return +1
    if (mag_ret >= fam.lock_mag) and not on_locks:
        return -1
    return 0


# ---------------------------------------------------------------------------
# PRBS-9 bit error rate tester, bit-exact with the RTL
# ---------------------------------------------------------------------------

def prbs_step(s):
    """One step of x^9 + x^5 + 1, RTL form {s[7:0], s[8] ^ s[4]}."""
    fb = ((s >> 8) ^ (s >> 4)) & 1
    return ((s << 1) | fb) & 0x1FF


def prbs_out8(s):
    """The next eight output bits, first bit out in the MSB."""
    v = 0
    t = s
    for i in range(7, -1, -1):
        v |= (t & 1) << i
        t = prbs_step(t)
    return v


def prbs_step8(s):
    for _ in range(8):
        s = prbs_step(s)
    return s


def prbs_check(hist, byte_val):
    """Check one received byte against the prediction from the history.

    Returns (new_history, failures).  The predictor's state is the received
    history itself, so it needs no synchronisation machinery at all.  The
    price is error multiplication: one bit error in the stream shows up as
    three prediction failures, once as itself and twice more when it reaches
    the two feedback taps.
    """
    t = hist
    e = 0
    for i in range(7, -1, -1):
        d = (byte_val >> i) & 1
        p = ((t >> 8) ^ (t >> 4)) & 1
        e += p ^ d
        t = ((t << 1) | d) & 0x1FF
    return t, e


def prbs_bytes(count, state=PRBS_RESET):
    """The byte sequence the transmitter sends with prbs_en set."""
    out = []
    for _ in range(count):
        out.append(prbs_out8(state))
        state = prbs_step8(state)
    return out


# ---------------------------------------------------------------------------
# Jammer model (bit-exact with the RTL)
# ---------------------------------------------------------------------------

class Jammer:
    """x^17 + x^14 + 1, reset state 0x1ACE1, advanced five steps per clock.

    Five steps per clock means the five low bits compared against the
    threshold are all fresh on every chip, so chip flips are independent
    with probability thresh/32: a binary symmetric channel.

    The pulsed and periodic timebases are free-running counters that start at
    zero when reset releases, and the follower delay line is fed with the
    clean chip source, all exactly as in the RTL.
    """

    RESET = 0x1ACE1
    BITS = 17

    def __init__(self, jam_level=0, enabled=True, kind=JAM_BROADBAND,
                 pulse_duty=0, fol_delay=0):
        self.state = self.RESET
        self.thresh = JAM_THRESH[jam_level]
        self.enabled = enabled
        self.kind = kind
        self.duty_top = PULSE_DUTY_TOP[pulse_duty]
        self.fol_delay = fol_delay
        self.tone_cnt = 0
        self.fol_sr = 0

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

    def step(self, chip_in=0):
        """Return this clock's flip decision, then advance every timebase.

        chip_in is the clean chip source, which only matters for the follower
        kind.  The counters advance whether or not the jammer is enabled,
        matching the RTL.
        """
        draw = self.state & 0x1F
        bsc = draw < self.thresh
        if self.kind == JAM_BROADBAND:
            core = bsc
        elif self.kind == JAM_PULSED:
            core = ((self.tone_cnt & 0xF) < self.duty_top) \
                and bool(draw & 1)
        elif self.kind == JAM_PERIODIC:
            core = (self.tone_cnt >> 1) < self.thresh
        else:
            core = bool((self.fol_sr >> self.fol_delay) & 1) and bsc

        self.state = self.advance(self.state)
        # One counter serves both timed kinds in the RTL: its low four
        # bits are the pulsed duty cycle and the whole six are the
        # periodic cycle.
        self.tone_cnt = (self.tone_cnt + 1) & 0x3F
        self.fol_sr = ((self.fol_sr << 1) | (chip_in & 1)) & 0xFF
        return 1 if (core and self.enabled) else 0

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


def spread_bits(bits, code_id, fam=SHORT):
    chips = []
    for b in bits:
        chips.extend(spread_bit(b, code_id, fam))
    return chips


def spread_frame(byte_val, code_id, fec=False, framed=False, fam=SHORT):
    return spread_bits(frame_bits(byte_val, fec, framed), code_id, fam)


def despread_bits(rx_chips, code_id, fam=SHORT):
    """Aligned hard decisions, one per code period."""
    big_l = fam.L
    return [despread_window(rx_chips[i:i + big_l], code_id, fam)[0]
            for i in range(0, len(rx_chips) - big_l + 1, big_l)]


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
    return (((code_id & 0x1F) << 1) | 1) & 0x3F


def hop_channels(code_id, n_bits, fast=False, fhss=True, fam=SHORT):
    """Channel of every chip of the first n_bits bits sent after reset.

    The hop state is seeded at reset and carries on across frames, so this
    covers any number of back-to-back frames too.
    """
    if not fhss:
        return [PARK_CH] * (n_bits * fam.L)
    s, out = hop_seed(code_id), []
    for _ in range(n_bits):
        for k in range(fam.L):
            out.append(s)
            if fast or k == fam.L - 1:
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

def _check_family(fam, check, deep=True):
    """Every claim that depends on the code length, for one family."""
    tag = f" [{fam.name}]"
    big_l = fam.L

    pa = lfsr_period(fam.taps_a, n=fam.n)
    pb = lfsr_period(fam.taps_b, n=fam.n)
    check(f"LFSR-A is maximal length{tag}", pa == big_l,
          f"  (period {pa}, want {big_l})")
    check(f"LFSR-B is maximal length{tag}", pb == big_l,
          f"  (period {pb}, want {big_l})")

    codes = {cid: fam.code(cid) for cid in range(1, big_l + 1)}
    check(f"{big_l} seeds give {big_l} distinct codes{tag}",
          len({tuple(c) for c in codes.values()}) == big_l)

    # The subtractor-free magnitude in the RTL must equal the arithmetic one
    # for every reachable match count.  This is the identity that removed
    # eight 7-bit subtractors from the correlator path.
    check(f"magn() masking identity holds for every M{tag}",
          all(fam.magn(m) == fam.magn_ref(m) for m in range(big_l + 1)))
    check(f"|R| = 2*magn + 1 for every M{tag}",
          all(abs(2 * m - big_l) == 2 * fam.magn(m) + 1
              for m in range(big_l + 1)))

    # Autocorrelation sidelobes and cross-correlations must both stay inside
    # the preferred-pair bound t(n) = 1 + 2^((n+1)/2).  Done on bitmasks
    # because at L = 127 this is a million correlations and a per-chip Python
    # loop over that takes minutes rather than a second.
    mask = (1 << big_l) - 1
    ints = {cid: _bits_to_int(c) for cid, c in codes.items()}
    side, cross = set(), set()
    for cid, ci in ints.items():
        dbl = ci | (ci << big_l)
        for s in range(1, big_l):
            m = big_l - (((dbl >> s) & mask) ^ ci).bit_count()
            side.add(2 * m - big_l)
    ids = sorted(ints)
    for i_idx, i in enumerate(ids):
        dbl = ints[i] | (ints[i] << big_l)
        for j in ids[i_idx + 1:]:
            cj = ints[j]
            for s in range(big_l):
                m = big_l - (((dbl >> s) & mask) ^ cj).bit_count()
                cross.add(2 * m - big_l)

    worst = max(max(abs(v) for v in side), max(abs(v) for v in cross))
    check(f"correlation bound t(n) = {fam.bound} is met and attained{tag}",
          worst == fam.bound, f"  (worst |R| = {worst})")
    expect = sorted({-1, -fam.bound, fam.bound - 2})
    check(f"correlation value set is {expect}{tag}",
          sorted(side | cross) == expect, f"  (got {sorted(side | cross)})")

    # Every impostor window must land strictly inside the lock thresholds.
    # A data bit of 1 inverts the code, so R and -R are both reachable and the
    # match-count set is closed under M -> L - M.
    impostor_r = (side | cross) | {-r for r in (side | cross)}
    impostor_m = sorted({(r + big_l) // 2 for r in impostor_r})
    check(f"impostor match counts never reach lock{tag}",
          all(fam.lock_lo < m < fam.lock_hi for m in impostor_m),
          f"  (impostor M = {impostor_m}, lock needs M<={fam.lock_lo} "
          f"or M>={fam.lock_hi})")

    # The follower jammer replays the chip stream after a delay, so what it
    # presents to the correlator is the right code at the wrong phase.  That
    # is a sidelobe, bounded by t(n), which is the whole reason the matched
    # attack fails.
    check(f"a replayed chip stream cannot reach lock{tag}",
          max(abs(v) for v in side) < fam.lock_r,
          f"  (worst replay |R| = {max(abs(v) for v in side)}, "
          f"lock needs {fam.lock_r})")

    # Round trip over every byte, clean channel.
    probe = 7 if big_l >= 7 else 1
    bad = [v for v in range(256)
           if despread_byte(spread_byte(v, probe, fam), probe, fam) != v]
    check(f"all 256 bytes survive a clean round trip{tag}", not bad)

    # Worst-case jam margin: the decision is a majority vote over L chips.
    first_fail = None
    for j in range(big_l + 1):
        chips = spread_bit(0, probe, fam)
        for k in range(j):
            chips[k] ^= 1
        bit, _, _ = despread_window(chips, probe, fam)
        if bit != 0 and first_fail is None:
            first_fail = j
    check(f"tolerates up to {fam.half - 1} corrupted chips{tag}",
          first_fail == fam.half,
          f"  (first failure at {first_fail} of {big_l} chips)")

    # The chip-phase discriminator must point toward alignment, not away.
    # At e = 0 it must make no correction, and one chip off in either
    # direction it must correct back.  Checked for every code and both data
    # polarities, because a window off the peak straddles two data bits.
    trk_ok = True
    trk_detail = ""
    for cid in range(1, min(big_l, 24) + 1):
        for bit in (0, 1):
            chips = spread_bit(bit, cid, fam)
            for e, want in ((0, 0), (1, -1), (-1, +1)):
                mags = early_late(chips, cid, e, fam)
                got = track_decision(*mags, fam=fam)
                if got != want:
                    trk_ok = False
                    trk_detail = (f"  (code {cid} bit {bit} e={e}: "
                                  f"wanted {want}, got {got}, mags {mags})")
    check(f"chip-phase discriminator corrects toward alignment{tag}",
          trk_ok, trk_detail)

    if not deep:
        return None, None

    # The acquisition threshold must clear the *misaligned* worst case too,
    # not merely the aligned Gold bound.  Both searches below are exhaustive.
    worst_mis, lock_mis = worst_misaligned_correlation(False, fam)
    check(f"lock threshold clears misaligned foreign windows{tag}",
          worst_mis < fam.lock_r and lock_mis == 0,
          f"  (worst |R| = {worst_mis}, {lock_mis} would lock, "
          f"lock needs |R| >= {fam.lock_r})")

    # The case acquisition really depends on: the right code at the wrong
    # phase.  Once locked the slip search stops, so a false lock here would
    # deliver wrong bytes forever with no self recovery.
    worst_self, lock_self = worst_misaligned_correlation(True, fam)
    check(f"lock threshold clears the right code at a wrong phase{tag}",
          worst_self < fam.lock_r and lock_self == 0,
          f"  (worst |R| = {worst_self}, {lock_self} would lock, "
          f"lock needs |R| >= {fam.lock_r})")

    return max(worst_mis, worst_self), worst


def _self_check(verbose=True, deep=True):
    ok = True

    def check(label, cond, detail=""):
        nonlocal ok
        ok = ok and bool(cond)
        if verbose:
            print(f"  [{'PASS' if cond else 'FAIL'}] {label}{detail}")

    if verbose:
        print("JAMGUARD model self-check")
        print("-" * 70)

    stats = {}
    for fam in FAMILIES:
        stats[fam.name] = _check_family(fam, check, deep)

    # --- jammer ------------------------------------------------------------
    jp = Jammer.period()
    check("jammer LFSR is maximal length", jp == 2 ** 17 - 1,
          f"  (period {jp})")
    for fam in FAMILIES:
        check(f"jam pattern never aligns with the code [{fam.name}]",
              math.gcd(jp, fam.L) == 1)

    # Five steps per clock must make consecutive draws independent, which is
    # what makes the broadband mode a genuine binary symmetric channel.  One
    # step per clock would share four of five bits with the previous draw.
    j = Jammer(jam_level=3)
    draws = []
    for _ in range(4096):
        draws.append(j.state & 0x1F)
        j.state = Jammer.advance(j.state)
    shared = sum(1 for a, b in zip(draws, draws[1:])
                 if (a >> 1) == (b & 0xF))
    check("consecutive jammer draws do not share bits", shared < 300,
          f"  ({shared} of 4095 coincidences, chance alone gives ~256)")

    # The periodic kind must have the same average density as broadband at
    # the same level, so the comparison between them isolates correlation in
    # time from average power.
    dens = []
    for kind in (JAM_BROADBAND, JAM_PERIODIC):
        jj = Jammer(jam_level=6, kind=kind)
        dens.append(sum(jj.step() for _ in range(6400)) / 6400)
    check("periodic jammer matches broadband average density",
          abs(dens[0] - dens[1]) < 0.02,
          f"  (broadband {dens[0]:.3f}, periodic {dens[1]:.3f}, "
          f"nominal {JAM_THRESH[6] / 32:.3f})")

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

    # --- PRBS-9 bit error rate tester --------------------------------------
    pp = lfsr_period(PRBS_TAPS, n=9)
    check("PRBS-9 is maximal length", pp == 511, f"  (period {pp})")

    # A clean stream must produce no failures once the history has filled.
    # The history starts at zero, so the first two bytes are not predictable
    # and are excluded, which is exactly what the datasheet tells the user.
    seq = prbs_bytes(80)
    hist, errs = 0, 0
    for i, b in enumerate(seq):
        hist, e = prbs_check(hist, b)
        if i >= 2:
            errs += e
    check("PRBS checker finds no errors in a clean stream", errs == 0,
          f"  ({errs} failures over {8 * (len(seq) - 2)} bits)")

    # One injected bit error must produce exactly three failures, which is
    # the calibration the BER readout depends on.
    mult = set()
    for pos in range(8):
        hist = 0
        for b in seq[:3]:
            hist, _ = prbs_check(hist, b)
        total = 0
        for i, b in enumerate(seq[3:12]):
            corrupt = b ^ (1 << pos) if i == 0 else b
            hist, e = prbs_check(hist, corrupt)
            total += e
        mult.add(total)
    check("one bit error gives exactly three prediction failures",
          mult == {3}, f"  (saw {sorted(mult)})")

    # --- derived figures ---------------------------------------------------
    if verbose:
        print("-" * 70)
        print(f"  {'':22s} {'L31':>12s} {'L127':>12s}")
        rows = [
            ("code length, chips", lambda f: f"{f.L}"),
            ("processing gain, dB", lambda f: f"{f.gain_db:.2f}"),
            ("codes in the family", lambda f: f"{f.L}"),
            ("bound t(n)", lambda f: f"{f.bound}"),
            ("lock threshold |R|", lambda f: f"{f.lock_r}"),
            ("lock M high / low", lambda f: f"{f.lock_hi} / {f.lock_lo}"),
            ("decode tolerance", lambda f: f"{f.half - 1}/{f.L} "
                                           f"({100 * (f.half - 1) / f.L:.1f}%)"),
            ("lock tolerance", lambda f: f"{f.lock_lo}/{f.L} "
                                         f"({100 * f.lock_lo / f.L:.1f}%)"),
        ]
        for label, fn in rows:
            print(f"  {label:22s} {fn(SHORT):>12s} {fn(LONG):>12s}")
        if deep:
            for fam in FAMILIES:
                mis, imp = stats[fam.name]
                print(f"  worst misaligned |R|, {fam.name:4s}  {mis}/{fam.L} "
                      f"(lock needs >= {fam.lock_r}), worst impostor "
                      f"{imp}/{fam.L}")
        print("-" * 70)

        print("\n  Broadband jammer, per level, at L = 31 and L = 127")
        print("  level  P(flip)   BER(31)   BER(127)  byte(31)  "
              "byte+FEC(31)")
        for lvl, thr in enumerate(JAM_THRESH):
            p = thr / 32
            def ber_for(f):
                return sum(math.comb(f.L, k) * p ** k * (1 - p) ** (f.L - k)
                           for k in range(f.half, f.L + 1))
            b31, b127 = ber_for(SHORT), ber_for(LONG)
            cw_fail = 1 - ((1 - b31) ** 8 + 8 * b31 * (1 - b31) ** 7)
            print(f"  {lvl}{' fine' if lvl >= 4 else '     '}  {p:6.1%}  "
                  f"{b31:9.2e} {b127:9.2e}  {1 - (1 - b31) ** 8:8.2e}  "
                  f"{max(0.0, 1 - (1 - cw_fail) ** 2):8.2e}")
        print()
        print("RESULT:", "ALL CHECKS PASSED" if ok else "FAILURES PRESENT")

    return ok


if __name__ == "__main__":
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="JAMGUARD model self-check")
    ap.add_argument("--quick", action="store_true",
                    help="skip the exhaustive misaligned-window searches, "
                         "which dominate the runtime at L = 127")
    args = ap.parse_args()
    sys.exit(0 if _self_check(deep=not args.quick) else 1)
