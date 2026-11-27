/*
 * JAMGUARD, a DSSS (direct-sequence spread-spectrum) anti-jam link core
 *
 * Copyright (c) 2026 Marc Boudames (github: 1marcb)
 * SPDX-License-Identifier: Apache-2.0
 *
 * ---------------------------------------------------------------------------
 * WHAT IT IS
 *   A complete spread-spectrum modem with its own test equipment built in,
 *   on one Tiny Tapeout 1x2 tile:
 *
 *   SPREADING
 *     - a Gold-code generator with two selectable lengths, 31 chips from a
 *       degree-5 preferred pair or 127 chips from a degree-7 preferred pair,
 *       giving 14.9 dB or 21.0 dB of processing gain,
 *     - a transmitter that spreads each data bit over one whole code period,
 *     - 31 or 127 distinct codes, selected by the seed of the second LFSR.
 *
 *   RECEIVING
 *     - three parallel correlators, on-time plus one chip advanced and one
 *       chip retarded, so the receiver measures the shape of the correlation
 *       peak and not just its height,
 *     - serial-search acquisition, so the receiver finds the chip phase on
 *       its own with no epoch wire,
 *     - chip-phase tracking from the advanced and retarded correlators, so a
 *       link between two boards with independent crystals keeps working
 *       instead of sliding off the peak after a few hundred bytes,
 *     - an optional sync word so the receiver also finds byte boundaries on
 *       its own,
 *     - optional forward error correction, each byte becoming two
 *       interleaved SECDED(8,4) extended Hamming codewords,
 *     - a two-deep receive buffer with an overflow flag, so a host that
 *       polls a little slower than the frame rate does not lose bytes.
 *
 *   MEASUREMENT, all on chip and all readable on the output pins
 *     - a PRBS-9 bit error rate tester, generator and self-synchronising
 *       checker, with 16-bit byte and error counters, both saturating,
 *     - the mean correlation magnitude over 256 windows, which is the
 *       link margin,
 *     - a fourth correlator running a different code of the same family,
 *       with a peak-hold register, which measures code isolation in silicon
 *       rather than asserting it from a bound,
 *     - a count of frames delivered, which is the denominator of the bit
 *       error rate.
 *
 *   JAMMING, so the anti-jam margin can be measured with no instruments
 *     - broadband, a true binary symmetric channel from a 17-bit LFSR,
 *     - pulsed, a duty-cycled burst that destroys whole frames,
 *     - periodic, a deterministic inversion at the same average density,
 *     - follower, which replays the chip stream back at the receiver after a
 *       selectable delay, the matched attack that spread spectrum defeats,
 *     - any of the four optionally restricted to part of the hop band.
 *
 *   The receiver decodes correctly while fewer than half the chips in a
 *   period are corrupted, and refuses to lock onto a transmitter using a
 *   different code_id.  Both claims are verified exhaustively, not argued
 *   from a textbook bound, in test/jamguard_model.py.
 *
 * ---------------------------------------------------------------------------
 * CONFIGURATION  (latched continuously while rst_n == 0)
 *
 *   The configuration lives in four pages.  uio_in[5] selects page 0 or the
 *   group of extension pages, and on an extension page uio_in[4:3] picks
 *   which one.  Hold each page for at least two clocks.  Page 0 also clears
 *   every extension page, so a plain reset with uio_in[5] low always lands
 *   in the basic 31-chip DSSS mode with every extra feature off.  Write page
 *   0 first, then any extension pages, in any order.
 *
 *   Page 0  (uio_in[5] = 0)
 *     ui_in [4:0]  tx_code_id   low five bits of the LFSR-B seed
 *     ui_in [6:5]  jam_level    jammer strength, low two bits
 *     ui_in [7]    acq_en       1 = self-acquiring receiver (gated on lock)
 *                               0 = receiver slaved to rx_epoch_in
 *     uio_in[4:0]  rx_code_id   low five bits, 0 means "same as TX"
 *
 *   Page 1  (uio_in[5] = 1, uio_in[4:3] = 00), the mode byte
 *     ui_in [0]    fec_en       SECDED(8,4) x2, 16 coded bits per byte
 *     ui_in [1]    frame_en     8-bit sync word before every frame
 *     ui_in [2]    fhss_en      frequency hopping on
 *     ui_in [3]    pbj_en       jammer restricted to part of the band
 *     ui_in [4]    jam_fine     high bit of jam_level, the fine ladder
 *     ui_in [6:5]  disp_page    low two bits of the status page number
 *     ui_in [7]    tx_pins_off  tri-state uio[7:6] for the whole run
 *     uio_in[0]    hop_fast     0 = one hop per data bit, 1 = per chip
 *     uio_in[2:1]  pbj_band     partial-band width 1/8, 1/4, 1/2, 3/4
 *
 *   Page 2  (uio_in[5] = 1, uio_in[4:3] = 01), long codes and tracking
 *     ui_in [1:0]  tx_code_hi   high two bits of the TX seed, for L = 127
 *     ui_in [3:2]  rx_code_hi   high two bits of the RX seed, for L = 127
 *     ui_in [4]    len127       1 = 127-chip codes, 0 = 31-chip codes
 *     ui_in [5]    trk_en       1 = chip-phase tracking on (needs acq_en)
 *     ui_in [6]    prbs_en      1 = transmit PRBS-9, measure BER on receive
 *     ui_in [7]    rx_pop_en    1 = uio[0] is RX_POP, not TX_START
 *     uio_in[0]    mon_en       1 = run the cross-correlation monitor
 *     uio_in[1]    reserved, write 0
 *     uio_in[2]    disp_page2   bit 2 of the status page number
 *
 *   Page 3  (uio_in[5] = 1, uio_in[4:3] = 10), the extended jammer
 *     ui_in [1:0]  jam_kind     0 broadband, 1 pulsed, 2 periodic,
 *                               3 follower
 *     ui_in [3:2]  fol_delay    follower delay, fol_delay + 1 chips
 *     ui_in [4]    reserved, write 0
 *     ui_in [6:5]  pulse_duty   pulsed duty cycle 1/16, 1/8, 1/4, 1/2
 *     ui_in [7]    disp_page3   bit 3 of the status page number
 *
 *   uio_in[4:3] = 11 on an extension page writes nothing and is reserved.
 *
 * RUN MODE
 *   ui_in [7:0]  tx_data      byte to transmit, sampled when a frame starts,
 *                             ignored when prbs_en is set
 *   uo_out[7:0]  rx_data      last received byte, or the FIFO head when
 *                             rx_pop_en is set              (disp_sel = 0)
 *                status page  selected by disp_page         (disp_sel = 1)
 *                   0: {valid, locked, hunting, m_last[4:0]}
 *                   1: {valid, locked, in_frame, fec_err, fec_fix[1:0],
 *                       frame_count[1:0]}
 *                   2: {tx_busy, jam_now, tx_channel[5:0]}
 *                   3: rx_raw, the received byte before error correction
 *                   4: {locked, m_last[6:0]}, the full match count
 *                   5: {0, mean_mag[6:0]}, mean correlation magnitude
 *                   6: {mon_false_lock, mon_peak[6:0]}
 *                   7: ber_errors[7:0],   saturating at 65535
 *                   8: ber_errors[15:8]
 *                   9: ber_bytes[7:0],    bytes delivered, saturating
 *                  10: ber_bytes[15:8]
 *               11..15: {fifo_ovf, fifo_count[1:0], track_corr[2:0],
 *                        len127, prbs_en}
 *
 *                A bit error rate is ber_errors / (3 * 8 * ber_bytes): the
 *                checker is self-synchronising, so one bit error in the
 *                stream shows up as three prediction failures.  Discard the
 *                first two bytes after reset, whose predictions come from an
 *                empty history.
 *   uio[0] in    tx_start     rising edge sends one byte, hold high to
 *                             stream, or RX_POP when rx_pop_en is set
 *   uio[1] in    rx_chip_in   received chip stream, one chip per clock
 *   uio[2] in    rx_epoch_in  pulses on the first chip of each code period
 *   uio[3] in    loopback_en  1 = wire the transmitter into the receiver
 *   uio[4] in    jam_en       1 = enable the on-chip jammer
 *   uio[5] in    disp_sel     0 = show rx_data, 1 = show the status page
 *   uio[6] out   tx_chip_out  transmitted chip stream
 *   uio[7] out   tx_epoch_out code-period strobe, aligned to chip 0
 *
 * FRAME FORMAT (one bit = one code period, sent MSB first)
 *   [ SYNC 0xE4, 8 bits, only if frame_en ] [ payload ]
 *   payload = the byte, 8 bits                                (fec_en = 0)
 *           = two SECDED(8,4) codewords, bit interleaved,
 *             16 bits {H7,L7,H6,L6,...,H0,L0}                 (fec_en = 1)
 * ---------------------------------------------------------------------------
 */

`default_nettype none

module tt_um_1marcb_jamguard (
    input  wire [7:0] ui_in,    // Dedicated inputs
    output wire [7:0] uo_out,   // Dedicated outputs
    input  wire [7:0] uio_in,   // IOs: Input path
    output wire [7:0] uio_out,  // IOs: Output path
    output wire [7:0] uio_oe,   // IOs: Enable path (active high: 0=input, 1=output)
    input  wire       ena,      // always 1 when the design is powered
    input  wire       clk,      // clock
    input  wire       rst_n     // reset_n - low to reset
);

  // ==========================================================================
  // Constants
  // ==========================================================================

  // Both code lengths share one pair of 7-bit LFSRs.  In the short mode the
  // degree-5 step functions write zeros into the top two bits, so those bits
  // self-clear after one step and no second generator is needed.
  localparam [6:0] SEED_A = 7'h01;   // fixed seed of LFSR-A, both lengths

  // Decision thresholds.  M is the number of chips out of L that agreed with
  // the local code, and R = 2M - L is the signed correlation.  Because L is
  // odd, |R| = 2 * mag + 1 where mag is the distance from the midpoint, so
  // comparing mag is the same as comparing |R| and costs one comparator
  // instead of two.  See the magn() function.
  //
  //   decode:  bit = 1 when M < L/2, which is a single bit of M
  //   lock:    mag >= LOCK_MAG_31 or LOCK_MAG_127
  //
  // Why these numbers and not something just above the Gold bound t(n)?
  // Because t(n) only covers windows ALIGNED to a code period.  A receiver
  // that has not acquired yet slides its window freely and also sees windows
  // straddling two data bits.  Those are sums of two partial correlations,
  // the bound does not apply, and they reach |R| = 17 of 31 and |R| = 41 of
  // 127.  The thresholds below clear the measured worst case by 6.5% and
  // 6.3% of the code length respectively, so both lengths are equally
  // conservative.  See worst_misaligned_correlation() in
  // test/jamguard_model.py, which is checked on every CI run.
  localparam [6:0] LOCK_MAG_31  = 7'd9;    // |R| >= 19 of 31
  localparam [6:0] LOCK_MAG_127 = 7'd24;   // |R| >= 49 of 127

  // Frame sync word: the 7-bit Barker code 1110010 followed by a 0.  It does
  // not overlap itself, and its MSB is 1, which is what lets a hunting
  // receiver start from an all-zero shift register without ever matching a
  // partially received sync word (proved in jamguard_model.py).
  localparam [7:0] SYNC = 8'hE4;

  // ==========================================================================
  // Input synchronisers
  //
  // Every bidirectional input is asynchronous with respect to clk (DIP
  // switches, PMOD wires, or a second chip).  Two flop stages on all six of
  // them keeps metastability out of the state machines and keeps gate-level
  // simulation free of X propagation.
  // ==========================================================================

  reg [5:0] sync0, sync1;

  always @(posedge clk) begin
    if (!rst_n) begin
      sync0 <= 6'b0;
      sync1 <= 6'b0;
    end else begin
      sync0 <= uio_in[5:0];
      sync1 <= sync0;
    end
  end

  wire pin0_s      = sync1[0];   // TX_START, or RX_POP when rx_pop_en
  wire rx_chip_s   = sync1[1];
  wire rx_epoch_s  = sync1[2];
  wire loopback_en = sync1[3];
  wire jam_en      = sync1[4];
  wire disp_sel    = sync1[5];

  // ==========================================================================
  // Configuration capture
  //
  // Load-enable flops that track the pins the whole time reset is asserted,
  // so the value present on the pins at the moment reset releases is the
  // configuration.  uio_in[5] picks page 0 or the extension group, and
  // uio_in[4:3] picks which extension page.  Writing page 0 clears every
  // extension page, which guarantees no mode register is left undefined by a
  // reset that only ever set page 0.
  // ==========================================================================

  reg [7:0] cfg_a;   // page 0, from ui_in
  reg [4:0] cfg_b;   // page 0, from uio_in[4:0]
  reg [7:0] mode;    // page 1, from ui_in
  reg [2:0] mode2;   // page 1, from uio_in[2:0]
  reg [7:0] cfg2a;   // page 2, from ui_in
  reg [2:0] cfg2b;   // page 2, from uio_in[2:0]
  reg [7:0] cfg3a;   // page 3, from ui_in
  reg [2:0] cfg3b;   // page 3, from uio_in[2:0]

  // cfg_armed exists to stop a reset that only ever selects an extension
  // page from leaving page 0 undefined.
  //
  // The extension pages are safe without help because the page 0 branch
  // below writes them, so one reset clock defines them. cfg_a and cfg_b are
  // written only in the page 0 branch and have no reset of their own, and
  // uio_in[5] is DISP_SEL in run mode, so an operator sitting on a status
  // page who presses reset never executes that branch. Every register in the
  // hardened netlist is a plain dfxtp_2 with no reset pin, which makes reset
  // entirely synchronous and that power up state genuinely undefined, not
  // merely unspecified in simulation.
  //
  // cfg_armed is cleared throughout every run, so it is 0 entering any reset
  // and the page 0 branch is forced until page 0 has actually been
  // presented. With uio_in[5] low the condition is true either way, so the
  // documented page 0 then extension page sequence behaves exactly as
  // before.
  reg cfg_armed;
  always @(posedge clk) begin
    if (rst_n)           cfg_armed <= 1'b0;
    else if (!uio_in[5]) cfg_armed <= 1'b1;
  end

  always @(posedge clk) begin
    if (!rst_n) begin
      if (!uio_in[5] || !cfg_armed) begin
        cfg_a <= ui_in;
        cfg_b <= uio_in[4:0];
        mode  <= 8'h00;
        mode2 <= 3'b000;
        cfg2a <= 8'h00;
        cfg2b <= 3'b000;
        cfg3a <= 8'h00;
        cfg3b <= 3'b000;
      end else begin
        case (uio_in[4:3])
          2'b00: begin
            mode  <= ui_in;
            mode2 <= uio_in[2:0];
          end
          2'b01: begin
            cfg2a <= ui_in;
            cfg2b <= uio_in[2:0];
          end
          2'b10: begin
            cfg3a <= ui_in;
            cfg3b <= uio_in[2:0];
          end
          default: begin
            // reserved, writes nothing
          end
        endcase
      end
    end
  end

  // ---- page 2 first, because the code length gates the seed width ---------
  wire       len127     = cfg2a[4];
  wire       trk_en     = cfg2a[5];
  wire       prbs_en    = cfg2a[6];
  wire       rx_pop_en  = cfg2a[7];
  wire       mon_en     = cfg2b[0];

  // A zero LFSR seed is a dead state, so force it to 1.  In the short mode
  // the top two bits of the seed are masked away, so setting tx_code_hi while
  // len127 is clear changes nothing.
  wire [6:0] seed_mask  = len127 ? 7'h7F : 7'h1F;
  wire [6:0] tx_seed_raw = {cfg2a[1:0], cfg_a[4:0]} & seed_mask;
  wire [6:0] tx_seed     = (tx_seed_raw == 7'd0) ? 7'd1 : tx_seed_raw;

  // rx_code_id == 0 means "use the same code as the transmitter".
  wire [6:0] rx_seed_raw = {cfg2a[3:2], cfg_b[4:0]} & seed_mask;
  wire [6:0] rx_seed     = (rx_seed_raw == 7'd0) ? tx_seed : rx_seed_raw;

  // The cross-correlation monitor runs the next code in the family.  It is
  // derived rather than configured because it costs seven config bits that
  // the pin budget does not have, and any other member of the family is as
  // good a witness as any other.
  wire [6:0] mon_seed_raw = (rx_seed + 7'd1) & seed_mask;
  wire [6:0] mon_seed     = (mon_seed_raw == 7'd0) ? 7'd1 : mon_seed_raw;

  wire [1:0] jam_level  = cfg_a[6:5];
  wire       acq_en     = cfg_a[7];

  wire       fec_en      = mode[0];
  wire       frame_en    = mode[1];
  wire       fhss_en     = mode[2];
  wire       pbj_en      = mode[3];
  wire       jam_fine    = mode[4];
  wire       tx_pins_off = mode[7];
  wire       hop_fast    = mode2[0];
  wire [1:0] pbj_band    = mode2[2:1];

  wire [1:0] jam_kind    = cfg3a[1:0];
  wire [1:0] fol_delay   = cfg3a[3:2];
  wire [1:0] pulse_duty  = cfg3a[6:5];

  // The status page number is assembled from three pages so that pages 0 to 3
  // keep the bit positions they have always had.
  wire [3:0] disp_page = {cfg3a[7], cfg2b[2], mode[6:5]};

  // uio[0] is TX_START on a transmitting board and RX_POP on a receive-only
  // board.  A board cannot be both, which is why one pin can serve both.
  wire tx_start_s = pin0_s & ~rx_pop_en;
  wire rx_pop_s   = pin0_s &  rx_pop_en;

  // Rising-edge detectors on both meanings of pin 0.
  reg pin0_q;
  always @(posedge clk) begin
    if (!rst_n) pin0_q <= 1'b0;
    else        pin0_q <= pin0_s;
  end
  wire tx_start_rise = tx_start_s & ~pin0_q;
  wire rx_pop_rise   = rx_pop_s   & ~pin0_q;

  // ==========================================================================
  // Gold code generators
  //
  // Short mode, two maximal-length 5-bit LFSRs forming a preferred pair:
  //     A:  x^5 + x^2 + 1                  taps a[4], a[1]
  //     B:  x^5 + x^4 + x^3 + x^2 + 1      taps b[4], b[3], b[2], b[1]
  //
  // Long mode, two maximal-length 7-bit LFSRs forming a preferred pair
  // (octal 211 and 217):
  //     A:  x^7 + x^3 + 1                  taps a[6], a[2]
  //     B:  x^7 + x^3 + x^2 + x + 1        taps b[6], b[2], b[1], b[0]
  //
  // The chip is the XOR of the two outputs.  Shifting B relative to A selects
  // one of the L Gold codes, and shifting B is the same thing as changing its
  // seed, which is why code_id is simply the seed of B.
  //
  // Both LFSRs are reloaded at chip 0 of every code period, so exactly one
  // full code period covers exactly one data bit ("short code" DSSS).
  //
  // The periodic cross-correlation of each family, over every code pair and
  // every relative shift, is exactly t(5) = 9 of 31 and t(7) = 17 of 127.
  // Both bounds are attained, so neither pair has slack to spare and neither
  // can be improved on at that length.
  // ==========================================================================

  // Both forms shift the same four bits into the same four places, so only
  // the top two bits and the feedback bit need selecting.  Writing it this
  // way instead of muxing two whole 7-bit results costs three selects per
  // generator instead of seven, and there are four generators.
  function [6:0] step_a(input [6:0] s, input lng);
    step_a = {lng ? s[5:4] : 2'b00,
              s[3:0],
              lng ? (s[6] ^ s[2]) : (s[4] ^ s[1])};
  endfunction

  function [6:0] step_b(input [6:0] s, input lng);
    step_b = {lng ? s[5:4] : 2'b00,
              s[3:0],
              lng ? (s[6] ^ s[2] ^ s[1] ^ s[0])
                  : (s[4] ^ s[3] ^ s[2] ^ s[1])};
  endfunction

  // Index of the last chip of a code period.
  wire [6:0] chip_last = len127 ? 7'd126 : 7'd30;

  // Distance of the match count from the midpoint of the code length.  For
  // odd L this is monotone in |R| = |2M - L|, because |R| = 2 * magn + 1, so
  // every comparison of correlation strength can be made on magn instead.
  //
  // No subtractor is needed.  The midpoint is a power of two, so above it
  // m - half is just the low bits of m, and below it half - 1 - m is the
  // ones complement of those same bits:
  //     m >= 64:  m - 64      ==   m & 63
  //     m <  64:  63 - m      == ~m & 63
  // That matters because this function is instantiated four times, once per
  // correlator, and each instance would otherwise carry a 7-bit subtractor
  // in the window-closing path.
  function [6:0] magn(input [6:0] m, input lng);
    reg [6:0] folded;
    begin
      folded = (lng ? m[6] : m[4]) ? m : ~m;
      magn   = folded & (lng ? 7'h3F : 7'h0F);
    end
  endfunction

  wire [6:0] lock_mag = len127 ? LOCK_MAG_127 : LOCK_MAG_31;

  // ==========================================================================
  // SECDED(8,4) extended Hamming code
  //
  // Codeword layout, c[7:0] = {d3, d2, d1, d0, p2, p1, p0, q}
  //   p0 = d0 ^ d1 ^ d3
  //   p1 = d0 ^ d2 ^ d3
  //   p2 = d1 ^ d2 ^ d3
  //   q  = even parity over the other seven bits
  // Every single-bit error has a distinct syndrome {s2,s1,s0}:
  //   d0 -> 3, d1 -> 5, d2 -> 6, d3 -> 7, p0 -> 1, p1 -> 2, p2 -> 4,
  // and the overall parity q separates one error (q fails) from two errors
  // (q passes but the syndrome is non-zero).  Minimum distance is 4: correct
  // one error, detect two.
  // ==========================================================================

  function [7:0] secded_enc(input [3:0] d);
    reg p0, p1, p2;
    begin
      p0 = d[0] ^ d[1] ^ d[3];
      p1 = d[0] ^ d[2] ^ d[3];
      p2 = d[1] ^ d[2] ^ d[3];
      secded_enc = {d, p2, p1, p0, ^{d, p2, p1, p0}};
    end
  endfunction

  // Returns {uncorrectable, corrected, data[3:0]}.
  function [5:0] secded_dec(input [7:0] c);
    reg [2:0] syn;
    reg       q;
    reg [3:0] flip;
    begin
      syn[0] = c[1] ^ c[4] ^ c[5] ^ c[7];
      syn[1] = c[2] ^ c[4] ^ c[6] ^ c[7];
      syn[2] = c[3] ^ c[5] ^ c[6] ^ c[7];
      q      = ^c;
      // Only flip a data bit when the overall parity confirms a single error.
      flip   = {syn == 3'd7, syn == 3'd6, syn == 3'd5, syn == 3'd3} & {4{q}};
      secded_dec = {(syn != 3'd0) & ~q, q, c[7:4] ^ flip};
    end
  endfunction

  // Bit interleave two codewords, H on the odd positions, L on the even, so
  // that any burst of two adjacent bit errors lands in two different
  // codewords, where each one is correctable.
  function [15:0] interleave(input [7:0] h, input [7:0] l);
    integer i;
    begin
      for (i = 0; i < 8; i = i + 1) begin
        interleave[2 * i + 1] = h[i];
        interleave[2 * i]     = l[i];
      end
    end
  endfunction

  // ==========================================================================
  // PRBS-9 bit error rate tester
  //
  // x^9 + x^5 + 1, period 511, the standard short test pattern.  The
  // transmitter substitutes eight fresh PRBS bits for the byte on ui_in, and
  // the receiver predicts each payload bit from the nine payload bits before
  // it:  b[n] = b[n-9] ^ b[n-5].
  //
  // The predictor needs no synchronisation state machine at all, because its
  // state IS the received history.  The price is error multiplication: one
  // bit error in the stream shows up as three prediction failures, once as
  // itself and twice more when it reaches the two feedback taps.  So for
  // error rates low enough that errors arrive isolated, which is the regime
  // any such measurement is made in, BER = errors / (3 * bits).
  // ==========================================================================

  function [8:0] prbs_step(input [8:0] s);
    prbs_step = {s[7:0], s[8] ^ s[4]};
  endfunction

  // The next eight output bits, first bit out in the MSB to match the
  // MSB-first frame order.
  function [7:0] prbs_out8(input [8:0] s);
    integer i;
    reg [8:0] t;
    begin
      t = s;
      for (i = 7; i >= 0; i = i - 1) begin
        prbs_out8[i] = t[0];
        t = prbs_step(t);
      end
    end
  endfunction

  function [8:0] prbs_step8(input [8:0] s);
    prbs_step8 = prbs_step(prbs_step(prbs_step(prbs_step(
                 prbs_step(prbs_step(prbs_step(prbs_step(s))))))));
  endfunction

  // Check a received byte against the prediction from the history, and
  // return {new_history[8:0], failures[3:0]}.  history[0] is the most
  // recently received payload bit.
  function [12:0] prbs_check(input [8:0] h, input [7:0] d);
    integer i;
    reg [8:0] t;
    reg [3:0] e;
    begin
      t = h;
      e = 4'd0;
      for (i = 7; i >= 0; i = i - 1) begin
        e = e + {3'b0, ((t[8] ^ t[4]) ^ d[i])};
        t = {t[7:0], d[i]};
      end
      prbs_check = {t, e};
    end
  endfunction

  // ==========================================================================
  // Frequency-hopping sequence
  //
  // A 6-bit maximal LFSR, x^6 + x^5 + 1, period 63, so the channel number is
  // 1..63.  Its seed is {code_id, 1}, which is never zero, so each code_id
  // starts the hop pattern at a different phase.  The seed is loaded at
  // reset, and from then on the sequence advances only while transmitting
  // and carries on from frame to frame, so a slow hopper visits all 63
  // channels rather than the same handful every frame.  A receiver that knows
  // the code_id and how many bits have been sent since reset knows the hop
  // sequence.  With hopping off the transmitter parks on channel 1, inside
  // every partial-band jammer setting, which models the jammer having found
  // a fixed-frequency link.
  // ==========================================================================

  function [5:0] step_h(input [5:0] s);
    step_h = {s[4:0], s[5] ^ s[4]};
  endfunction

  localparam [5:0] PARK_CH = 6'd1;

  // ==========================================================================
  // Transmitter
  // ==========================================================================

  reg [6:0]  tx_a, tx_b;     // code generator state for the current chip
  reg [15:0] tx_shift;       // payload, transmitted MSB first
  reg [4:0]  tx_bit;         // bit index within the frame, header included
  reg [6:0]  tx_idx;         // chip index within the code period
  reg        tx_busy;
  reg [5:0]  tx_hop;         // hop state for the current chip
  reg [8:0]  prbs_tx;        // PRBS-9 generator state

  // The byte comes from the pins, or from the pattern generator when the
  // built-in bit error rate tester is running.
  wire [7:0] tx_src = prbs_en ? prbs_out8(prbs_tx) : ui_in;

  // The payload is loaded already encoded, so the shift-out logic is the same
  // with and without FEC, and only the frame length differs.
  wire [15:0] tx_load = fec_en
      ? interleave(secded_enc(tx_src[7:4]), secded_enc(tx_src[3:0]))
      : {tx_src, 8'h00};

  // Frame length minus one, in bits: 7, 15 or 23.
  wire [4:0] tx_last_bit = {frame_en & fec_en, frame_en ^ fec_en, 3'b111};
  wire       tx_hdr      = frame_en & (tx_bit[4:3] == 2'b00);
  wire       tx_data_bit = tx_hdr ? SYNC[~tx_bit[2:0]] : tx_shift[15];
  wire       tx_bit_end  = tx_busy & (tx_idx == chip_last);
  wire       tx_frm_end  = tx_bit_end & (tx_bit == tx_last_bit);

  wire tx_code  = tx_a[0] ^ tx_b[0];
  wire tx_chip  = tx_busy & (tx_code ^ tx_data_bit);
  wire tx_epoch = tx_busy & (tx_idx == 7'd0);

  // Every cycle on which tx_shift takes a fresh payload, which is also every
  // cycle the pattern generator must advance by one byte.
  wire tx_load_now = (~tx_busy & tx_start_rise) | (tx_frm_end & tx_start_s);

  always @(posedge clk) begin
    if (!rst_n) begin
      tx_shift <= 16'h0000;
      tx_bit   <= 5'd0;
      tx_idx   <= 7'd0;
      tx_busy  <= 1'b0;
    end else if (!tx_busy) begin
      // Idle: park at chip 0 and wait for a start edge.
      tx_idx <= 7'd0;
      tx_bit <= 5'd0;
      if (tx_start_rise) begin
        tx_busy  <= 1'b1;
        tx_shift <= tx_load;
      end
    end else if (tx_bit_end) begin
      tx_idx <= 7'd0;
      if (tx_frm_end) begin
        tx_bit <= 5'd0;
        if (tx_start_s) tx_shift <= tx_load;  // held high -> stream next byte
        else            tx_busy  <= 1'b0;
      end else begin
        tx_bit <= tx_bit + 5'd1;
        // The sync word comes from a constant, so the payload only shifts
        // once the header is over.
        if (!tx_hdr) tx_shift <= {tx_shift[14:0], 1'b0};
      end
    end else begin
      tx_idx <= tx_idx + 7'd1;
    end
  end

  // Code generator: reload at the end of every period, otherwise step.
  always @(posedge clk) begin
    if (!rst_n || !tx_busy || tx_bit_end) begin
      tx_a <= SEED_A;
      tx_b <= tx_seed;
    end else begin
      tx_a <= step_a(tx_a, len127);
      tx_b <= step_b(tx_b, len127);
    end
  end

  // Pattern generator: one byte per transmitted frame.
  always @(posedge clk) begin
    if (!rst_n)                   prbs_tx <= 9'h1FF;
    else if (prbs_en & tx_load_now) prbs_tx <= prbs_step8(prbs_tx);
  end

  // Hop generator: seeded at reset, steps every chip or every bit while
  // transmitting, and holds its place between frames.
  wire [5:0] hop_seed = {tx_seed[4:0], 1'b1};
  always @(posedge clk) begin
    if (!rst_n) begin
      tx_hop <= hop_seed;
    end else if (tx_busy && (hop_fast || tx_bit_end)) begin
      tx_hop <= step_h(tx_hop);
    end
  end

  wire [5:0] tx_channel = fhss_en ? tx_hop : PARK_CH;

  // ==========================================================================
  // Jammer
  //
  // Four mechanisms, all driven from one 17-bit maximal LFSR (x^17 + x^14 + 1,
  // period 131071, a prime) advanced five steps per clock, so the five low
  // bits compared against the threshold are entirely fresh on every chip.
  // (A one-step-per-clock LFSR reuses four of the five bits from the previous
  // chip, which clusters the errors and roughly triples the bit error rate at
  // a given density, so it would not be the binary symmetric channel the
  // coding theory assumes.)
  //
  //   broadband  each chip flipped independently with probability thr/32,
  //              which is exactly a binary symmetric channel
  //   pulsed     a duty-cycled burst, randomising every chip while it is on.
  //              Same average power as broadband spread over less time, so
  //              it destroys whole frames instead of scattering single bit
  //              errors, which is what defeats a code without interleaving
  //   periodic   a deterministic inversion at the same average density as
  //              broadband.  Inverting chips drags the correlation toward
  //              -L rather than toward 0, so it is not equivalent to noise
  //              of the same power
  //   follower   the chip stream replayed back after fol_delay + 1 chips.
  //              This is the matched attack, a jammer using the victim's own
  //              code, and the reason it fails is the whole point of spread
  //              spectrum: a Gold code correlated against a shifted copy of
  //              itself gives at most t(n), not L
  //
  // Any of the four can be restricted to the lower part of the hop band.
  // ==========================================================================

  function [16:0] step_n(input [16:0] s);
    step_n = {s[15:0], s[16] ^ s[13]};
  endfunction

  reg [16:0] noise;
  always @(posedge clk) begin
    if (!rst_n) noise <= 17'h1ACE1;
    else        noise <= step_n(step_n(step_n(step_n(step_n(noise)))));
  end

  // Coarse ladder, one level per regime of the link:
  //   0, 1  lock holds and the data is clean
  //   2     lock is lost, the majority vote still mostly decodes
  //   3     half the chips flip and the link breaks
  // Fine ladder, spaced across the decode cliff where FEC matters.
  reg [4:0] jam_thresh;
  always @(*) begin
    case ({jam_fine, jam_level})
      3'b000:  jam_thresh = 5'd2;   //  6.3% of chips
      3'b001:  jam_thresh = 5'd4;   // 12.5%
      3'b010:  jam_thresh = 5'd11;  // 34.4%
      3'b011:  jam_thresh = 5'd16;  // 50.0%
      3'b100:  jam_thresh = 5'd8;   // 25.0%
      3'b101:  jam_thresh = 5'd9;   // 28.1%
      3'b110:  jam_thresh = 5'd10;  // 31.3%
      default: jam_thresh = 5'd12;  // 37.5%
    endcase
  end

  // One free-running 6-bit counter serves both timed kinds, because a jammer
  // is only ever one kind at a time.  Its low four bits are the pulsed duty
  // cycle and the whole thing is the periodic cycle.
  reg [5:0] tone_cnt;
  always @(posedge clk) begin
    if (!rst_n) tone_cnt <= 6'd0;
    else        tone_cnt <= tone_cnt + 6'd1;
  end

  reg [3:0] duty_top;
  always @(*) begin
    case (pulse_duty)
      2'd0:    duty_top = 4'd1;   // 1/16
      2'd1:    duty_top = 4'd2;   // 1/8
      2'd2:    duty_top = 4'd4;   // 1/4
      default: duty_top = 4'd8;   // 1/2
    endcase
  end
  wire pulse_on = tone_cnt[3:0] < duty_top;

  // The periodic kind is on for 2 * jam_thresh of every 64 chips, so its
  // average density is thresh/32, identical to the broadband setting at the
  // same jam level.  That is what makes the comparison between the two
  // isolate correlation in time from average power.
  wire tone_on = tone_cnt[5:1] < jam_thresh;

  // Follower jammer delay line, fed from the clean chip source so the
  // jammer replays the signal and not its own output.  Four taps is enough:
  // the reason the attack fails is the autocorrelation sidelobe bound, which
  // holds at every shift, so one shift demonstrates it as well as eight.
  reg [3:0] fol_sr;

  // Partial-band jammer: it only covers the lowest part of the hop band.
  // Compare the top three channel bits against 1, 2, 4 or 6 eighths.
  reg [2:0] band_top;
  always @(*) begin
    case (pbj_band)
      2'd0:    band_top = 3'd1;     // channels  1..7    (7 of 63)
      2'd1:    band_top = 3'd2;     // channels  1..15   (15 of 63)
      2'd2:    band_top = 3'd4;     // channels  1..31   (31 of 63)
      default: band_top = 3'd6;     // channels  1..47   (47 of 63)
    endcase
  end
  wire in_band = tx_channel[5:3] < band_top;

  wire bsc_hit = noise[4:0] < jam_thresh;

  reg jam_core;
  always @(*) begin
    case (jam_kind)
      2'd0:    jam_core = bsc_hit;                       // broadband
      2'd1:    jam_core = pulse_on & noise[0];           // pulsed
      2'd2:    jam_core = tone_on;                       // periodic
      default: jam_core = fol_sr[fol_delay] & bsc_hit;   // follower
    endcase
  end

  wire jam_hit = jam_en & jam_core & (~pbj_en | in_band);

  // The channel: pick the source, then let the jammer corrupt it.
  //
  // The loopback tap is registered, and that one detail is worth 5.7 ns of
  // setup slack.  tx_chip is combinational out of the transmitter's state,
  // so feeding it straight into the correlator put the whole transmitter and
  // the whole receiver in a single path: a config flop, through the sync
  // word multiplexer, through the chip exclusive-or, through the loopback
  // multiplexer, through the jammer, into the correlator's seven-bit
  // accumulator, through the magnitude fold and the threshold comparator,
  // and only then into a flop.  Taking the real pin instead, the receiver
  // starts from a synchroniser output, so the transmitter half of that path
  // does not exist.  Registering the tap makes loopback behave like the pin,
  // which is what it is pretending to be anyway, at a cost of two flops.
  //
  // Both the chip and the epoch are delayed by the same single cycle, so
  // their alignment is untouched.  The external pins carry the undelayed
  // stream, because a second board resynchronises it regardless.
  reg tx_chip_q, tx_epoch_q;
  always @(posedge clk) begin
    if (!rst_n) begin
      tx_chip_q  <= 1'b0;
      tx_epoch_q <= 1'b0;
    end else begin
      tx_chip_q  <= tx_chip;
      tx_epoch_q <= tx_epoch;
    end
  end

  wire rx_chip_src = loopback_en ? tx_chip_q  : rx_chip_s;
  wire rx_epoch    = loopback_en ? tx_epoch_q : rx_epoch_s;
  wire rx_chip     = rx_chip_src ^ jam_hit;

  always @(posedge clk) begin
    if (!rst_n) fol_sr <= 4'h0;
    else        fol_sr <= {fol_sr[2:0], rx_chip_src};
  end

  // ==========================================================================
  // Receiver, despreading
  //
  // Three correlators run in parallel over the same window.  The on-time one
  // uses the local code at the current index, the advanced one uses the code
  // one chip ahead, and the retarded one uses the code one chip behind.  The
  // code one chip ahead is free: it is the value the generator is about to
  // load, which is already computed combinationally to drive the generator
  // registers.  The code one chip behind costs a single flop.
  //
  // Together the three measure the shape of the correlation peak, not just
  // its height, and that is what makes chip-phase tracking possible.
  // ==========================================================================

  reg [6:0] rx_a, rx_b;      // local code generator
  reg [6:0] rx_idx;          // chip index within the local code period
  reg       rx_active;       // a correlation window is in progress
  reg       rx_slip;         // stall one chip to retard the local phase
  reg       trk_shrink;      // end the next window one chip early
  reg [6:0] acc_on;          // running match count, on-time
  reg [6:0] acc_adv;         // running match count, advanced replica
  reg [6:0] acc_ret;         // running match count, retarded replica
  reg       code_ret_r;      // the local code value one chip ago
  reg [6:0] m_last;          // match count of the most recent window
  reg [1:0] conf;            // lock confidence, 0..3
  reg [2:0] trk_cnt;         // tracking corrections made, saturating

  wire locked = conf[1];     // conf >= 2

  // In epoch-slaved mode the epoch strobe arrives on the same clock as chip 0,
  // so chip 0 must be processed on that very cycle, hence win_start_now and
  // cur_idx rather than simply using the registered rx_idx.
  wire       win_start_now = (~acq_en) & rx_epoch & ~rx_active;
  wire       chip_tick     = (rx_active | win_start_now) & ~rx_slip;
  wire [6:0] cur_idx       = win_start_now ? 7'd0 : rx_idx;

  // Tracking is only allowed once the receiver is self-acquiring and locked.
  // In epoch-slaved mode the external strobe defines the window, so shrinking
  // it would only break the alignment the strobe is there to provide.
  wire trk_active = trk_en & acq_en & locked;

  // The window normally runs to chip_last.  A tracking correction either ends
  // it one chip early, which advances the local phase by one chip relative to
  // the incoming stream, or holds the local code for one chip (rx_slip),
  // which retards it by one.  Those are the only two phase adjustments
  // available when the chip rate and the clock rate are the same.
  wire [6:0] win_last = chip_last - {6'b0, trk_shrink};
  wire       win_end  = chip_tick & (cur_idx == win_last);

  // The generator values for the next chip.  These drive the registers and
  // double as the advanced replica.
  wire [6:0] a_nxt = (cur_idx == win_last) ? SEED_A  : step_a(rx_a, len127);
  wire [6:0] b_nxt = (cur_idx == win_last) ? rx_seed : step_b(rx_b, len127);

  wire code_on  = rx_a[0] ^ rx_b[0];
  wire code_adv = a_nxt[0] ^ b_nxt[0];
  wire code_ret = code_ret_r;

  wire match_on  = ~(rx_chip ^ code_on);
  wire match_adv = ~(rx_chip ^ code_adv);
  wire match_ret = ~(rx_chip ^ code_ret);

  // Final match counts of the window, available combinationally on its last
  // chip.  cur_idx == win_last is never 0, so the accumulators are valid.
  wire [6:0] m_on  = acc_on  + {6'b0, match_on};
  wire [6:0] m_adv = acc_adv + {6'b0, match_adv};
  wire [6:0] m_ret = acc_ret + {6'b0, match_ret};

  wire [6:0] mag_on  = magn(m_on,  len127);
  wire [6:0] mag_adv = magn(m_adv, len127);
  wire [6:0] mag_ret = magn(m_ret, len127);

  // The decision threshold is free: M < L/2 is one bit of M.
  wire rx_bit_val = len127 ? ~m_on[6] : ~m_on[4];
  wire win_lock   = mag_on >= lock_mag;

  // Chip-phase discriminator.  A classic early-late difference would dither,
  // because a Gold code has autocorrelation sidelobes as large as t(n) and at
  // perfect alignment the two offset correlators see those sidelobes rather
  // than nothing.
  //
  // The test used instead is "would a neighbour have locked when the on-time
  // correlator did not".  At alignment the on-time correlator locks, so
  // nothing fires.  One chip off the peak the aligned neighbour reaches the
  // full code length while the on-time one sees only a sidelobe, so exactly
  // one of the two fires and it is the one pointing back at the peak.  Under
  // heavy jamming all three magnitudes collapse together and none of them
  // reaches the threshold, so the loop correctly does nothing rather than
  // chasing noise, which is what comparing the three against each other
  // would have done.  It is also cheaper: two comparisons against a constant
  // instead of three between 7-bit values.
  wire adv_wins = (mag_adv >= lock_mag) & ~win_lock;
  wire ret_wins = (mag_ret >= lock_mag) & ~win_lock;

  // In self-acquiring mode nothing is believed until the correlator locks.
  // In epoch-slaved mode the external epoch is trusted, so bits are always
  // accumulated, which is the lab mode used to measure raw bit error rate
  // past the point where lock is lost.
  wire accumulate = acq_en ? locked : 1'b1;

  // ---- chip index, window control, search slip and tracking --------------
  always @(posedge clk) begin
    if (!rst_n) begin
      rx_idx     <= 7'd0;
      rx_active  <= 1'b0;
      rx_slip    <= 1'b0;
      trk_shrink <= 1'b0;
      trk_cnt    <= 3'd0;
    end else begin
      rx_slip <= 1'b0;
      if (acq_en) begin
        rx_active <= 1'b1;                     // free-running correlator
        if (!rx_slip) begin
          if (rx_idx == win_last) begin
            rx_idx     <= 7'd0;
            trk_shrink <= 1'b0;
            if (!locked) begin
              // No lock on this window and no established lock to protect:
              // skip one incoming chip, which walks the local phase by one.
              if (!win_lock) rx_slip <= 1'b1;
            end else if (trk_active) begin
              if (adv_wins) begin
                trk_shrink <= 1'b1;            // advance the local phase
                if (trk_cnt != 3'd7) trk_cnt <= trk_cnt + 3'd1;
              end else if (ret_wins) begin
                rx_slip <= 1'b1;               // retard the local phase
                if (trk_cnt != 3'd7) trk_cnt <= trk_cnt + 3'd1;
              end
            end
          end else begin
            rx_idx <= rx_idx + 7'd1;
          end
        end
      end else if (rx_active) begin
        if (rx_idx == chip_last) begin
          rx_idx    <= 7'd0;
          rx_active <= 1'b0;                   // wait for the next epoch
        end else begin
          rx_idx <= rx_idx + 7'd1;
        end
      end else if (rx_epoch) begin
        rx_active <= 1'b1;
        rx_idx    <= 7'd1;                     // chip 0 handled this cycle
      end
    end
  end

  // ---- local code generator, kept in step with cur_idx --------------------
  always @(posedge clk) begin
    if (!rst_n || !(rx_active | win_start_now)) begin
      rx_a       <= SEED_A;
      rx_b       <= rx_seed;
      code_ret_r <= 1'b0;
    end else if (chip_tick) begin
      rx_a       <= a_nxt;
      rx_b       <= b_nxt;
      code_ret_r <= code_on;
    end
  end

  // ---- correlator accumulators -------------------------------------------
  always @(posedge clk) begin
    if (!rst_n) begin
      acc_on  <= 7'd0;
      acc_adv <= 7'd0;
      acc_ret <= 7'd0;
    end else if (chip_tick) begin
      if (cur_idx == 7'd0) begin
        acc_on  <= {6'b0, match_on};
        acc_adv <= {6'b0, match_adv};
        acc_ret <= {6'b0, match_ret};
      end else begin
        acc_on  <= m_on;
        acc_adv <= m_adv;
        acc_ret <= m_ret;
      end
    end
  end

  // ---- lock confidence, with hysteresis so one jammed window is survivable
  always @(posedge clk) begin
    if (!rst_n) begin
      conf   <= 2'd0;
      m_last <= 7'd0;
    end else if (win_end) begin
      m_last <= m_on;
      if (win_lock) conf <= (conf == 2'd3) ? 2'd3 : conf + 2'd1;
      else          conf <= (conf == 2'd0) ? 2'd0 : conf - 2'd1;
    end
  end

  // ==========================================================================
  // Cross-correlation monitor
  //
  // A fourth correlator, running a different code of the same family over the
  // same windows, with a peak-hold register.  It turns the cross-correlation
  // bound from an assertion into a measurement: mon_peak is the largest
  // correlation magnitude any foreign code achieved against the live signal,
  // and mon_false_lock latches if one ever reached the lock threshold, which
  // it must not.  This is the code-division isolation of the link, measured
  // in silicon on real traffic.
  // ==========================================================================

  // Only one register is needed, not two.  A Gold code is LFSR-A XOR LFSR-B
  // and every member of the family shares the same LFSR-A, seeded the same
  // way and stepped at the same time, so the monitor borrows rx_a and keeps
  // only its own B.  That is a whole 7-bit register and a whole step function
  // saved for nothing given up.
  reg [6:0] mon_b;
  reg [6:0] mon_acc;
  reg [6:0] mon_peak;
  reg       mon_false_lock;

  wire [6:0] mon_b_nxt = (cur_idx == win_last) ? mon_seed : step_b(mon_b, len127);

  wire       mon_match = ~(rx_chip ^ (rx_a[0] ^ mon_b[0]));
  wire [6:0] mon_m     = mon_acc + {6'b0, mon_match};
  wire [6:0] mon_mag   = magn(mon_m, len127);

  always @(posedge clk) begin
    if (!rst_n || !(rx_active | win_start_now)) begin
      mon_b <= mon_seed;
    end else if (chip_tick) begin
      mon_b <= mon_b_nxt;
    end
  end

  always @(posedge clk) begin
    if (!rst_n) begin
      mon_acc        <= 7'd0;
      mon_peak       <= 7'd0;
      mon_false_lock <= 1'b0;
    end else begin
      if (chip_tick) begin
        if (cur_idx == 7'd0) mon_acc <= {6'b0, mon_match};
        else                 mon_acc <= mon_m;
      end
      if (win_end && mon_en) begin
        if (mon_mag > mon_peak)  mon_peak       <= mon_mag;
        if (mon_mag >= lock_mag) mon_false_lock <= 1'b1;
      end
    end
  end

  // ==========================================================================
  // Receiver, framing and error correction
  //
  // Without frame_en, framing is implicit: the transmitter emits a fixed
  // number of bits per frame, so counting decided bits reproduces the frame
  // boundaries.  That needs the epoch wire, or a receiver that happened to
  // acquire on a frame boundary.
  //
  // With frame_en the receiver hunts for the sync word in the decided bit
  // stream, tolerating one bit error in it, and only then counts payload
  // bits.  Combined with acq_en that is a receiver needing nothing but the
  // chip stream itself.
  // ==========================================================================

  // Fifteen bits: the sixteenth payload bit is concatenated straight into
  // rx_next, so a sixteen-bit register would carry a flop that is never read.
  reg [14:0] rx_shift;
  reg [3:0]  rx_cnt;         // payload bit index
  reg        rx_inframe;     // sync seen, collecting payload
  reg [7:0]  rx_data;
  reg [7:0]  rx_raw;         // the received data bits before correction
  reg        rx_valid;
  reg        fec_err;        // last byte had an uncorrectable codeword
  reg [1:0]  fec_fix;        // last byte: which codewords were corrected
  reg [1:0]  rx_frames;      // frames delivered, mod 4

  wire        bit_tick = win_end & accumulate;
  wire [15:0] rx_next  = {rx_shift[14:0], rx_bit_val};

  // Hamming distance <= 1 between the last eight bits and SYNC: their XOR is
  // zero or a power of two, and x & (x - 1) clears the lowest set bit.
  wire [7:0] sync_x   = rx_next[7:0] ^ SYNC;
  wire       sync_hit = (sync_x & (sync_x - 8'd1)) == 8'd0;

  wire       hunting_sync = frame_en & ~rx_inframe;
  wire [3:0] rx_last_bit  = {fec_en, 3'b111};   // 7 or 15

  // De-interleave and decode the frame as it completes.
  wire [7:0] cw_h, cw_l;
  genvar gi;
  generate
    for (gi = 0; gi < 8; gi = gi + 1) begin : g_deint
      assign cw_h[gi] = rx_next[2 * gi + 1];
      assign cw_l[gi] = rx_next[2 * gi];
    end
  endgenerate

  wire [5:0] dec_h = secded_dec(cw_h);
  wire [5:0] dec_l = secded_dec(cw_l);

  // The frame completes on this cycle, and these are the bytes it carries.
  wire       frame_done = bit_tick & ~hunting_sync & (rx_cnt == rx_last_bit);
  wire [7:0] frame_byte = fec_en ? {dec_h[3:0], dec_l[3:0]} : rx_next[7:0];
  wire       frame_bad  = fec_en & (dec_h[5] | dec_l[5]);

  always @(posedge clk) begin
    if (!rst_n) begin
      rx_shift   <= 15'h0000;
      rx_cnt     <= 4'd0;
      rx_inframe <= 1'b0;
      rx_data    <= 8'h00;
      rx_raw     <= 8'h00;
      rx_valid   <= 1'b0;
      fec_err    <= 1'b0;
      fec_fix    <= 2'b00;
      rx_frames  <= 2'd0;
    end else if (bit_tick) begin
      if (hunting_sync) begin
        if (sync_hit) begin
          rx_inframe <= 1'b1;
          rx_cnt     <= 4'd0;
        end else begin
          rx_shift <= rx_next[14:0];
        end
      end else if (rx_cnt == rx_last_bit) begin
        // Last payload bit: deliver the frame.
        rx_data <= frame_byte;
        rx_raw  <= fec_en ? {cw_h[7:4], cw_l[7:4]} : rx_next[7:0];
        fec_err <= frame_bad;
        fec_fix <= fec_en ? {dec_h[4], dec_l[4]} : 2'b00;
        rx_valid   <= 1'b1;
        rx_frames  <= rx_frames + 2'd1;
        rx_cnt     <= 4'd0;
        rx_inframe <= 1'b0;
        // Clearing the register matters only when hunting for the next sync
        // word: SYNC[7] is 1, so a window that still holds any of these
        // zeros is at least two bits away from SYNC and can never match.
        rx_shift   <= 15'h0000;
      end else begin
        if (rx_cnt == 4'd0) rx_valid <= 1'b0;   // a new frame has started
        rx_shift <= rx_next[14:0];
        rx_cnt   <= rx_cnt + 4'd1;
      end
    end
  end

  // ==========================================================================
  // Receive FIFO
  //
  // Two bytes deep, so a host that polls a little slower than the frame rate
  // does not lose data, and fifo_ovf latches if it ever falls far enough
  // behind that a byte was dropped.  It is popped by a rising edge on
  // uio[0], which is TX_START on a transmitting board and RX_POP on a
  // receive-only one.
  //
  // Two registers and a slide, rather than an array with read and write
  // pointers, because at this depth the pointers, the write decode and the
  // read multiplexer together cost more than the storage they manage.  Depth
  // two is also where the return on depth drops off: it covers a host whose
  // polling jitters by a frame, which is the realistic case, while deeper
  // buffering only helps a host that is losing the race outright, and that
  // is what the overflow flag is there to tell you.
  //
  // Both registers are reset rather than left to power up arbitrarily,
  // because unreset storage reads as X in gate-level simulation until it has
  // been written, and that is the classic way a design passes at register
  // transfer level and fails after layout.
  // ==========================================================================

  reg [7:0] fifo0, fifo1;    // fifo0 is the head, the next byte out
  reg [1:0] fifo_cnt;        // 0, 1 or 2
  reg       fifo_ovf;

  wire fifo_push = frame_done  & (fifo_cnt != 2'd2);
  wire fifo_pop  = rx_pop_rise & (fifo_cnt != 2'd0);

  always @(posedge clk) begin
    if (!rst_n) begin
      fifo0    <= 8'h00;
      fifo1    <= 8'h00;
      fifo_cnt <= 2'd0;
      fifo_ovf <= 1'b0;
    end else begin
      if (fifo_pop) begin
        fifo0 <= fifo1;                    // slide the tail down
        if (fifo_push) fifo1 <= frame_byte;
      end else if (fifo_push) begin
        if (fifo_cnt == 2'd0) fifo0 <= frame_byte;
        else                  fifo1 <= frame_byte;
      end
      case ({fifo_push, fifo_pop})
        2'b10:   fifo_cnt <= fifo_cnt + 2'd1;
        2'b01:   fifo_cnt <= fifo_cnt - 2'd1;
        default: begin
          // both or neither, the count does not move
        end
      endcase
      if (frame_done & ~fifo_push) fifo_ovf <= 1'b1;
    end
  end

  wire [7:0] fifo_head = fifo0;

  // ==========================================================================
  // Bit error rate tester, receive side
  // ==========================================================================

  // The denominator counts delivered bytes rather than bits, which is eight
  // times less counter for the same reach, and it counts every delivered
  // frame whether or not the pattern generator is running, so it doubles as
  // the frame counter.  Both counters saturate rather than wrap, so a
  // reading is never ambiguous.
  reg [8:0]  ber_hist;
  reg [15:0] ber_err;
  reg [15:0] ber_bytes;

  wire [12:0] ber_chk = prbs_check(ber_hist, frame_byte);
  wire [16:0] ber_sum = {1'b0, ber_err} + {13'd0, ber_chk[3:0]};

  always @(posedge clk) begin
    if (!rst_n) begin
      ber_hist  <= 9'd0;
      ber_err   <= 16'd0;
      ber_bytes <= 16'd0;
    end else if (frame_done) begin
      if (ber_bytes != 16'hFFFF) ber_bytes <= ber_bytes + 16'd1;
      if (prbs_en) begin
        ber_hist <= ber_chk[12:4];
        ber_err  <= ber_sum[16] ? 16'hFFFF : ber_sum[15:0];
      end
    end
  end

  // ==========================================================================
  // Link meters
  //
  // The mean correlation magnitude is the link margin: it is L/2 when the
  // channel is clean and falls toward zero as the jammer takes hold, and
  // unlike the instantaneous match count it is stable enough to read off
  // eight pins by eye.
  // ==========================================================================

  // 256 windows, which is 7936 chips at L = 31 and 32512 at L = 127, so the
  // reading settles in under four milliseconds at the rated clock.  The
  // window count is a power of two, so the divide is a wire.  mag_on is at
  // most 63, so 256 of them need 14 bits and not 15.
  reg [13:0] mag_acc;
  reg [7:0]  mag_n;
  reg [6:0]  mean_mag;

  wire [13:0] mag_acc_nxt = mag_acc + {7'b0, mag_on};

  always @(posedge clk) begin
    if (!rst_n) begin
      mag_acc  <= 14'd0;
      mag_n    <= 8'd0;
      mean_mag <= 7'd0;
    end else if (win_end) begin
      if (mag_n == 8'd255) begin
        mean_mag <= mag_acc_nxt[13:7];
        mag_acc  <= 14'd0;
        mag_n    <= 8'd0;
      end else begin
        mag_acc <= mag_acc_nxt;
        mag_n   <= mag_n + 8'd1;
      end
    end
  end

  // There is deliberately no acquisition timer and no frame-outcome counter
  // here, although both were built and measured first.  Together they cost
  // about 1700 square microns, which is five percent of the core, and this
  // design is density limited rather than pin limited.  The rule used to
  // decide what stays is whether the quantity can be obtained from outside
  // the chip: time to acquire is a stopwatch on the LOCKED pin, and the
  // benefit of the error correction is the difference between two readings
  // of the error counter with fec_en set and clear.  The correlation
  // magnitude, the foreign-code peak and the error count itself cannot be
  // observed from outside at all, so those are the meters that earned the
  // area.

  // ==========================================================================
  // Outputs
  // ==========================================================================

  reg [7:0] status;
  always @(*) begin
    case (disp_page)
      4'd0:  status = {rx_valid, locked, acq_en & ~locked, m_last[4:0]};
      4'd1:  status = {rx_valid, locked, rx_inframe, fec_err, fec_fix,
                       rx_frames};
      4'd2:  status = {tx_busy, jam_hit, tx_channel};
      4'd3:  status = rx_raw;
      4'd4:  status = {locked, m_last};
      4'd5:  status = {1'b0, mean_mag};
      4'd6:  status = {mon_false_lock, mon_peak};
      4'd7:  status = ber_err[7:0];
      4'd8:  status = ber_err[15:8];
      4'd9:  status = ber_bytes[7:0];
      4'd10: status = ber_bytes[15:8];
      default: status = {fifo_ovf, fifo_cnt, trk_cnt, len127, prbs_en};
    endcase
  end

  // On a receive-only board the data port shows the head of the FIFO, so a
  // host pops bytes at its own pace.  Otherwise it shows the holding
  // register, which is the behaviour a single board in loopback needs.
  wire [7:0] data_out = rx_pop_en ? fifo_head : rx_data;

  assign uo_out = disp_sel ? status : data_out;

  // uio[7:6] carry the chip stream and the epoch strobe outward, and uio[5:0]
  // are always inputs. The two output drivers can be turned off entirely by
  // setting tx_pins_off in the mode byte, which leaves this part electrically
  // inert on the whole uio bus.  Two boards running this design both drive
  // uio[7:6], so a receiving board configured this way cannot contend with a
  // transmitting board however the jumper wires are placed.
  wire tx_pins_en = ~tx_pins_off;

  assign uio_out = {tx_epoch, tx_chip, 6'b0};
  assign uio_oe  = {{2{tx_pins_en}}, 6'b0};

  // List all unused inputs to prevent warnings.  uio_in[7:6] are the two pins
  // this design drives as outputs, so their input paths are never read, and
  // cfg3b is the reserved extension of the jammer page.
  wire _unused = &{ena, uio_in[7:6], cfg2b[1], cfg3a[4], cfg3b,
                 1'b0};

endmodule
