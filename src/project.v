/*
 * JAMGUARD, a DSSS (direct-sequence spread-spectrum) anti-jam link core
 *
 * Copyright (c) 2026 Marc Boudames (github: 1marcb)
 * SPDX-License-Identifier: Apache-2.0
 *
 * ---------------------------------------------------------------------------
 * WHAT IT IS
 *   A complete spread-spectrum modem on one Tiny Tapeout 1x2 tile:
 *     - a length-31 Gold-code generator (two 5-bit LFSRs, a preferred pair),
 *     - a transmitter that spreads each data bit over 31 chips,
 *     - a correlating receiver that despreads and majority-decides,
 *     - a serial-search acquisition mode so the receiver finds the chip
 *       phase on its own,
 *     - an optional sync word so the receiver also finds byte boundaries on
 *       its own, which removes the need for an epoch wire entirely,
 *     - optional forward error correction: each byte becomes two interleaved
 *       SECDED(8,4) extended Hamming codewords,
 *     - an optional frequency-hopping sequence derived from the code ID,
 *     - a built-in pseudo-random chip jammer, broadband or partial-band, so
 *       the anti-jam margin can be measured on the bench with no external
 *       equipment.
 *
 *   Processing gain = 10*log10(31) = 14.9 dB.  The receiver decodes correctly
 *   while fewer than 16 of the 31 chips are corrupted, and refuses to lock
 *   onto a transmitter using a different code_id (Gold cross-correlation is
 *   bounded at 9 of 31, verified exhaustively in test/jamguard_model.py).
 *
 * ---------------------------------------------------------------------------
 * CONFIGURATION  (latched continuously while rst_n == 0)
 *
 *   The configuration lives in two pages.  uio_in[5] selects which page the
 *   pins are writing while reset is held.  Hold each page for at least two
 *   clocks.  Page 0 also clears page 1, so a plain reset with uio_in[5] low
 *   always lands in the basic DSSS mode with every extra feature off.
 *
 *   Page 0  (uio_in[5] = 0)
 *     ui_in [4:0]  tx_code_id   seed of LFSR-B, selects 1 of 31 Gold codes
 *                               (0 is illegal and is forced to 1)
 *     ui_in [6:5]  jam_level    jammer strength 0..3
 *     ui_in [7]    acq_en       1 = self-acquiring receiver (gated on lock)
 *                               0 = receiver slaved to rx_epoch_in
 *     uio_in[4:0]  rx_code_id   receiver code_id, 0 means "same as TX"
 *
 *   Page 1  (uio_in[5] = 1), the mode byte and the mode extension
 *     ui_in [0]    fec_en       SECDED(8,4) x2, 16 coded bits per byte
 *     ui_in [1]    frame_en     8-bit sync word before every frame
 *     ui_in [2]    fhss_en      frequency hopping on
 *     ui_in [3]    pbj_en       jammer is partial-band, not broadband
 *     ui_in [4]    jam_fine     use the fine jam ladder (see jam_thresh)
 *     ui_in [6:5]  disp_page    which status page DISP_SEL shows
 *     ui_in [7]    tx_pins_off  tri-state uio[7:6] for the whole run
 *     uio_in[0]    hop_fast     0 = one hop per data bit, 1 = per chip
 *     uio_in[2:1]  pbj_band     partial-band width 1/8, 1/4, 1/2, 3/4
 *     uio_in[4:3]  reserved, write 0
 *
 * RUN MODE
 *   ui_in [7:0]  tx_data      byte to transmit, sampled when a frame starts
 *   uo_out[7:0]  rx_data      last fully received byte    (disp_sel = 0)
 *                status page  selected by disp_page       (disp_sel = 1)
 *                  0: {valid, locked, hunting, m_last[4:0]}
 *                  1: {valid, locked, in_frame, fec_err, fec_fix[1:0],
 *                      frame_count[1:0]}
 *                  2: {tx_busy, jam_now, tx_channel[5:0]}
 *                  3: rx_raw, the received byte before error correction
 *   uio[0] in    tx_start     rising edge sends one byte, hold high to stream
 *   uio[1] in    rx_chip_in   received chip stream, one chip per clock
 *   uio[2] in    rx_epoch_in  pulses on the first chip of each 31-chip code
 *   uio[3] in    loopback_en  1 = wire the transmitter into the receiver
 *   uio[4] in    jam_en       1 = enable the on-chip jammer
 *   uio[5] in    disp_sel     0 = show rx_data, 1 = show the status page
 *   uio[6] out   tx_chip_out  transmitted chip stream
 *   uio[7] out   tx_epoch_out code-period strobe, aligned to chip 0
 *
 * FRAME FORMAT (one bit = one 31-chip code period, sent MSB first)
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

  // Gold code parameters.  n = 5, so the code length is 2^5 - 1 = 31 chips.
  localparam [4:0] SEED_A    = 5'h01;  // fixed seed of LFSR-A
  localparam [4:0] CHIP_LAST = 5'd30;  // index of the last chip of a code period

  // Decision thresholds on the match count M (number of chips out of 31 that
  // agreed with the local code).  R = 2M - 31 is the signed correlation.
  //   decode:  bit = 1 when M < 16   (that is simply ~M[4], see below)
  //   lock:    |R| >= 19  <=>  M >= 25 or M <= 6
  //
  // Why 19 and not something just above the Gold bound of 9?  Because the
  // t(n) = 9 bound only covers windows ALIGNED to a code period.  A receiver
  // that has not acquired yet slides its window freely and also sees windows
  // straddling two data bits.  Those are sums of two partial correlations,
  // the bound does not apply, and they reach |R| = 17.  19 clears the
  // measured worst case with margin.  See worst_misaligned_correlation() in
  // test/jamguard_model.py, which is checked on every CI run.
  localparam [4:0] LOCK_HI = 5'd25;
  localparam [4:0] LOCK_LO = 5'd6;

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

  wire tx_start_s  = sync1[0];
  wire rx_chip_s   = sync1[1];
  wire rx_epoch_s  = sync1[2];
  wire loopback_en = sync1[3];
  wire jam_en      = sync1[4];
  wire disp_sel    = sync1[5];

  // Rising-edge detector on tx_start.
  reg tx_start_q;
  always @(posedge clk) begin
    if (!rst_n) tx_start_q <= 1'b0;
    else        tx_start_q <= tx_start_s;
  end
  wire tx_start_rise = tx_start_s & ~tx_start_q;

  // ==========================================================================
  // Configuration capture
  //
  // Load-enable flops that track the pins the whole time reset is asserted,
  // so the value present on the pins at the moment reset releases is the
  // configuration.  uio_in[5] picks the page.  Writing page 0 clears page 1,
  // which guarantees the mode registers are never left undefined by a reset
  // that only ever set page 0.
  // ==========================================================================

  reg [7:0] cfg_a;   // page 0, from ui_in
  reg [4:0] cfg_b;   // page 0, from uio_in[4:0]
  reg [7:0] mode;    // page 1, from ui_in
  reg [2:0] mode2;   // page 1, from uio_in[2:0]

  always @(posedge clk) begin
    if (!rst_n) begin
      if (!uio_in[5]) begin
        cfg_a <= ui_in;
        cfg_b <= uio_in[4:0];
        mode  <= 8'h00;
        mode2 <= 3'b000;
      end else begin
        mode  <= ui_in;
        mode2 <= uio_in[2:0];
      end
    end
  end

  // A zero LFSR seed is a dead state, so force it to 1.
  wire [4:0] tx_code_id = (cfg_a[4:0] == 5'd0) ? 5'd1 : cfg_a[4:0];
  wire [1:0] jam_level  = cfg_a[6:5];
  wire       acq_en     = cfg_a[7];
  // rx_code_id == 0 means "use the same code as the transmitter".
  wire [4:0] rx_code_id = (cfg_b[4:0] == 5'd0) ? tx_code_id : cfg_b[4:0];

  wire       fec_en      = mode[0];
  wire       frame_en    = mode[1];
  wire       fhss_en     = mode[2];
  wire       pbj_en      = mode[3];
  wire       jam_fine    = mode[4];
  wire [1:0] disp_page   = mode[6:5];
  wire       tx_pins_off = mode[7];
  wire       hop_fast    = mode2[0];
  wire [1:0] pbj_band    = mode2[2:1];

  // ==========================================================================
  // Gold code generator
  //
  // Two maximal-length 5-bit LFSRs forming a preferred pair:
  //     A:  x^5 + x^2 + 1                  taps a[4], a[1]
  //     B:  x^5 + x^4 + x^3 + x^2 + 1      taps b[4], b[3], b[2], b[1]
  // The chip is the XOR of the two outputs.  Shifting B relative to A selects
  // one of the 31 Gold codes, and shifting B is the same thing as changing its
  // seed, which is why code_id is simply the seed of B.
  //
  // Both LFSRs are reloaded at chip 0 of every code period, so exactly one
  // full code period covers exactly one data bit ("short code" DSSS).
  // ==========================================================================

  function [4:0] step_a(input [4:0] s);
    step_a = {s[3:0], s[4] ^ s[1]};
  endfunction

  function [4:0] step_b(input [4:0] s);
    step_b = {s[3:0], s[4] ^ s[3] ^ s[2] ^ s[1]};
  endfunction

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

  reg [4:0]  tx_a, tx_b;     // code generator state for the current chip
  reg [15:0] tx_shift;       // payload, transmitted MSB first
  reg [4:0]  tx_bit;         // bit index within the frame, header included
  reg [4:0]  tx_idx;         // chip index within the code period
  reg        tx_busy;
  reg [5:0]  tx_hop;         // hop state for the current chip

  // The payload is loaded already encoded, so the shift-out logic is the same
  // with and without FEC; only the frame length differs.
  wire [15:0] tx_load = fec_en
      ? interleave(secded_enc(ui_in[7:4]), secded_enc(ui_in[3:0]))
      : {ui_in, 8'h00};

  // Frame length minus one, in bits: 7, 15 or 23.
  wire [4:0] tx_last_bit = {frame_en & fec_en, frame_en ^ fec_en, 3'b111};
  wire       tx_hdr      = frame_en & (tx_bit[4:3] == 2'b00);
  wire       tx_data_bit = tx_hdr ? SYNC[~tx_bit[2:0]] : tx_shift[15];
  wire       tx_bit_end  = tx_busy & (tx_idx == CHIP_LAST);
  wire       tx_frm_end  = tx_bit_end & (tx_bit == tx_last_bit);

  wire tx_code  = tx_a[0] ^ tx_b[0];
  wire tx_chip  = tx_busy & (tx_code ^ tx_data_bit);
  wire tx_epoch = tx_busy & (tx_idx == 5'd0);

  always @(posedge clk) begin
    if (!rst_n) begin
      tx_shift <= 16'h0000;
      tx_bit   <= 5'd0;
      tx_idx   <= 5'd0;
      tx_busy  <= 1'b0;
    end else if (!tx_busy) begin
      // Idle: park at chip 0 and wait for a start edge.
      tx_idx <= 5'd0;
      tx_bit <= 5'd0;
      if (tx_start_rise) begin
        tx_busy  <= 1'b1;
        tx_shift <= tx_load;
      end
    end else if (tx_bit_end) begin
      tx_idx <= 5'd0;
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
      tx_idx <= tx_idx + 5'd1;
    end
  end

  // Code generator: reload at the end of every period, otherwise step.
  always @(posedge clk) begin
    if (!rst_n || !tx_busy || tx_bit_end) begin
      tx_a <= SEED_A;
      tx_b <= tx_code_id;
    end else begin
      tx_a <= step_a(tx_a);
      tx_b <= step_b(tx_b);
    end
  end

  // Hop generator: seeded at reset, steps every chip or every bit while
  // transmitting, and holds its place between frames.
  wire [5:0] hop_seed = {tx_code_id, 1'b1};
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
  // A 17-bit maximal LFSR (x^17 + x^14 + 1, period 131071, a prime) advanced
  // five steps per clock, so the five low bits compared against the
  // threshold are entirely fresh on every chip.  Each chip is then flipped
  // independently with probability thr/32, which is the binary symmetric
  // channel the coding theory assumes.  (A one-step-per-clock LFSR reuses
  // four of the five bits from the previous chip, which clusters the errors
  // and roughly triples the bit error rate at a given density.)
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

  wire jam_hit = jam_en & (noise[4:0] < jam_thresh) & (~pbj_en | in_band);

  // The channel: pick the source, then let the jammer corrupt it.
  wire rx_chip_src = loopback_en ? tx_chip  : rx_chip_s;
  wire rx_epoch    = loopback_en ? tx_epoch : rx_epoch_s;
  wire rx_chip     = rx_chip_src ^ jam_hit;

  // ==========================================================================
  // Receiver, despreading
  // ==========================================================================

  reg [4:0] rx_a, rx_b;      // local code generator
  reg [4:0] rx_idx;          // chip index within the local code period
  reg       rx_active;       // a correlation window is in progress
  reg       rx_slip;         // stall one chip to advance the local phase
  reg [4:0] match_cnt;       // running count of agreeing chips
  reg [4:0] m_last;          // match count of the most recent window
  reg [1:0] conf;            // lock confidence, 0..3

  wire locked = conf[1];     // conf >= 2

  // In epoch-slaved mode the epoch strobe arrives on the same clock as chip 0,
  // so chip 0 must be processed on that very cycle, hence win_start_now and
  // cur_idx rather than simply using the registered rx_idx.
  wire       win_start_now = (~acq_en) & rx_epoch & ~rx_active;
  wire       chip_tick     = (rx_active | win_start_now) & ~rx_slip;
  wire [4:0] cur_idx       = win_start_now ? 5'd0 : rx_idx;
  wire       win_end       = chip_tick & (cur_idx == CHIP_LAST);

  wire rx_code = rx_a[0] ^ rx_b[0];
  wire match   = ~(rx_chip ^ rx_code);          // 1 when the chip agrees

  // Final match count of the window, available combinationally on its last
  // chip.  cur_idx == CHIP_LAST is never 0, so match_cnt is always valid here.
  wire [4:0] m_final = match_cnt + {4'b0, match};

  // The decision threshold is free: M < 16 is exactly M[4] == 0.
  wire rx_bit_val = ~m_final[4];
  wire win_lock   = (m_final >= LOCK_HI) | (m_final <= LOCK_LO);

  // In self-acquiring mode nothing is believed until the correlator locks.
  // In epoch-slaved mode the external epoch is trusted, so bits are always
  // accumulated, which is the lab mode used to measure raw bit error rate
  // past the point where lock is lost.
  wire accumulate = acq_en ? locked : 1'b1;

  // ---- chip index, window control, and the serial-search slip -------------
  always @(posedge clk) begin
    if (!rst_n) begin
      rx_idx    <= 5'd0;
      rx_active <= 1'b0;
      rx_slip   <= 1'b0;
    end else begin
      rx_slip <= 1'b0;
      if (acq_en) begin
        rx_active <= 1'b1;                     // free-running correlator
        if (!rx_slip) begin
          if (rx_idx == CHIP_LAST) begin
            rx_idx <= 5'd0;
            // No lock on this window and no established lock to protect:
            // skip one incoming chip, which walks the local phase by one.
            if (!win_lock && !locked) rx_slip <= 1'b1;
          end else begin
            rx_idx <= rx_idx + 5'd1;
          end
        end
      end else if (rx_active) begin
        if (rx_idx == CHIP_LAST) begin
          rx_idx    <= 5'd0;
          rx_active <= 1'b0;                   // wait for the next epoch
        end else begin
          rx_idx <= rx_idx + 5'd1;
        end
      end else if (rx_epoch) begin
        rx_active <= 1'b1;
        rx_idx    <= 5'd1;                     // chip 0 handled this cycle
      end
    end
  end

  // ---- local code generator, kept in step with cur_idx --------------------
  always @(posedge clk) begin
    if (!rst_n || !(rx_active | win_start_now)) begin
      rx_a <= SEED_A;
      rx_b <= rx_code_id;
    end else if (chip_tick) begin
      if (cur_idx == CHIP_LAST) begin
        rx_a <= SEED_A;
        rx_b <= rx_code_id;
      end else begin
        rx_a <= step_a(rx_a);
        rx_b <= step_b(rx_b);
      end
    end
  end

  // ---- correlator accumulator --------------------------------------------
  always @(posedge clk) begin
    if (!rst_n) begin
      match_cnt <= 5'd0;
    end else if (chip_tick) begin
      if (cur_idx == 5'd0) match_cnt <= {4'b0, match};
      else                 match_cnt <= match_cnt + {4'b0, match};
    end
  end

  // ---- lock confidence, with hysteresis so one jammed window is survivable
  always @(posedge clk) begin
    if (!rst_n) begin
      conf   <= 2'd0;
      m_last <= 5'd0;
    end else if (win_end) begin
      m_last <= m_final;
      if (win_lock) conf <= (conf == 2'd3) ? 2'd3 : conf + 2'd1;
      else          conf <= (conf == 2'd0) ? 2'd0 : conf - 2'd1;
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
        if (fec_en) begin
          rx_data <= {dec_h[3:0], dec_l[3:0]};
          rx_raw  <= {cw_h[7:4], cw_l[7:4]};
          fec_err <= dec_h[5] | dec_l[5];
          fec_fix <= {dec_h[4], dec_l[4]};
        end else begin
          rx_data <= rx_next[7:0];
          rx_raw  <= rx_next[7:0];
          fec_err <= 1'b0;
          fec_fix <= 2'b00;
        end
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
  // Outputs
  // ==========================================================================

  reg [7:0] status;
  always @(*) begin
    case (disp_page)
      2'd0:    status = {rx_valid, locked, acq_en & ~locked, m_last};
      2'd1:    status = {rx_valid, locked, rx_inframe, fec_err, fec_fix,
                         rx_frames};
      2'd2:    status = {tx_busy, jam_hit, tx_channel};
      default: status = rx_raw;
    endcase
  end

  assign uo_out = disp_sel ? status : rx_data;

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
  // this design drives as outputs, so their input paths are never read.
  wire _unused = &{ena, uio_in[7:6], 1'b0};

endmodule
