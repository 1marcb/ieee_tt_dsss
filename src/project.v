/*
 * JAMGUARD -- a DSSS (direct-sequence spread-spectrum) anti-jam link core
 *
 * Copyright (c) 2026 Marc Boudames (github: 1marcb)
 * SPDX-License-Identifier: Apache-2.0
 *
 * ---------------------------------------------------------------------------
 * WHAT IT IS
 *   A complete spread-spectrum modem on one Tiny Tapeout 1x1 tile:
 *     - a length-31 Gold-code generator (two 5-bit LFSRs, a preferred pair),
 *     - a transmitter that spreads each data bit over 31 chips,
 *     - a correlating receiver that despreads and majority-decides,
 *     - a built-in pseudo-random chip jammer so the anti-jam margin can be
 *       measured on the bench with no external equipment,
 *     - a serial-search acquisition mode so the receiver can find the chip
 *       phase on its own.
 *
 *   Processing gain = 10*log10(31) = 14.9 dB.  The receiver decodes correctly
 *   while fewer than 16 of the 31 chips are corrupted, and refuses to lock
 *   onto a transmitter using a different code_id (Gold cross-correlation is
 *   bounded at 9 of 31, verified exhaustively in test/jamguard_model.py).
 *
 * ---------------------------------------------------------------------------
 * CONFIGURATION  (latched continuously while rst_n == 0)
 *   ui_in [4:0]  tx_code_id   seed of LFSR-B, selects 1 of 31 Gold codes
 *                             (0 is illegal and is forced to 1)
 *   ui_in [6:5]  jam_level    jammer strength 0..3
 *   ui_in [7]    acq_en       1 = self-acquiring receiver (gated on lock)
 *                             0 = receiver slaved to rx_epoch_in (ungated)
 *   uio_in[4:0]  rx_code_id   receiver code_id; 0 means "same as TX".  Set it
 *                             different from tx_code_id to watch the receiver
 *                             reject a foreign transmitter.
 *   uio_in[5]    reserved, tie low
 *
 * RUN MODE
 *   ui_in [7:0]  tx_data      byte to transmit, sampled when a frame starts
 *   uo_out[7:0]  rx_data      last fully received byte      (disp_sel = 0)
 *                status       {valid, locked, hunting, m_last[4:0]}
 *                                                           (disp_sel = 1)
 *   uio[0] in    tx_start     rising edge sends one byte; hold high to stream
 *   uio[1] in    rx_chip_in   received chip stream, one chip per clock
 *   uio[2] in    rx_epoch_in  pulses on the first chip of each 31-chip code
 *   uio[3] in    loopback_en  1 = wire the transmitter into the receiver
 *   uio[4] in    jam_en       1 = enable the on-chip jammer
 *   uio[5] in    disp_sel     0 = show rx_data, 1 = show status
 *   uio[6] out   tx_chip_out  transmitted chip stream
 *   uio[7] out   tx_epoch_out code-period strobe, aligned to chip 0
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
  // straddling two data bits; those are sums of two partial correlations, the
  // bound does not apply, and they reach |R| = 17.  A threshold of 13 lets a
  // free-running receiver false-lock onto a foreign transmitter about 17% of
  // the time.  19 clears the measured worst case of 17 with margin and costs
  // nothing in acquisition time.  See worst_misaligned_correlation() in
  // test/jamguard_model.py, which is checked on every CI run.
  localparam [4:0] LOCK_HI = 5'd25;
  localparam [4:0] LOCK_LO = 5'd6;

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
  // Held in load-enable flops that track the pins the whole time reset is
  // asserted, so the value present on the pins at the moment reset releases
  // is the configuration.  Hold rst_n low for at least a couple of clocks.
  // ==========================================================================

  reg [7:0] cfg_a;   // from ui_in
  reg [5:0] cfg_b;   // from uio_in[5:0]

  always @(posedge clk) begin
    if (!rst_n) begin
      cfg_a <= ui_in;
      cfg_b <= uio_in[5:0];
    end
  end

  // A zero LFSR seed is a dead state, so force it to 1.
  wire [4:0] tx_code_id = (cfg_a[4:0] == 5'd0) ? 5'd1 : cfg_a[4:0];
  wire [1:0] jam_level  = cfg_a[6:5];
  wire       acq_en     = cfg_a[7];
  // rx_code_id == 0 means "use the same code as the transmitter".
  wire [4:0] rx_code_id = (cfg_b[4:0] == 5'd0) ? tx_code_id : cfg_b[4:0];

  // ==========================================================================
  // Gold code generator
  //
  // Two maximal-length 5-bit LFSRs forming a preferred pair:
  //     A:  x^5 + x^2 + 1                  taps a[4], a[1]
  //     B:  x^5 + x^4 + x^3 + x^2 + 1      taps b[4], b[3], b[2], b[1]
  // The chip is the XOR of the two outputs.  Shifting B relative to A selects
  // one of the 31 Gold codes, and shifting B is the same thing as changing its
  // seed -- which is why code_id is simply the seed of B.
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
  // Transmitter
  // ==========================================================================

  reg [4:0] tx_a, tx_b;      // code generator state for the current chip
  reg [7:0] tx_shift;        // data byte, transmitted MSB first
  reg [2:0] tx_bit;          // which bit of the byte is being spread
  reg [4:0] tx_idx;          // chip index within the code period
  reg       tx_busy;

  wire tx_code  = tx_a[0] ^ tx_b[0];
  wire tx_chip  = tx_busy & (tx_code ^ tx_shift[7]);
  wire tx_epoch = tx_busy & (tx_idx == 5'd0);

  always @(posedge clk) begin
    if (!rst_n) begin
      tx_shift <= 8'h00;
      tx_bit   <= 3'd0;
      tx_idx   <= 5'd0;
      tx_busy  <= 1'b0;
    end else if (!tx_busy) begin
      // Idle: park at chip 0 and wait for a start edge.
      tx_idx <= 5'd0;
      tx_bit <= 3'd0;
      if (tx_start_rise) begin
        tx_busy  <= 1'b1;
        tx_shift <= ui_in;
      end
    end else if (tx_idx == CHIP_LAST) begin
      tx_idx <= 5'd0;
      if (tx_bit == 3'd7) begin
        tx_bit <= 3'd0;
        if (tx_start_s) tx_shift <= ui_in;   // held high -> stream the next byte
        else            tx_busy  <= 1'b0;
      end else begin
        tx_bit   <= tx_bit + 3'd1;
        tx_shift <= {tx_shift[6:0], 1'b0};   // next data bit into the MSB slot
      end
    end else begin
      tx_idx <= tx_idx + 5'd1;
    end
  end

  // Code generator: reload at the end of every period, otherwise step.
  always @(posedge clk) begin
    if (!rst_n || !tx_busy) begin
      tx_a <= SEED_A;
      tx_b <= tx_code_id;
    end else if (tx_idx == CHIP_LAST) begin
      tx_a <= SEED_A;
      tx_b <= tx_code_id;
    end else begin
      tx_a <= step_a(tx_a);
      tx_b <= step_b(tx_b);
    end
  end

  // ==========================================================================
  // Jammer
  //
  // An 8-bit maximal LFSR (x^8 + x^6 + x^5 + x^4 + 1, period 255) runs on
  // every clock, independently of the code periods.  gcd(255, 31) = 1, so the
  // jam pattern never locks to the code and the errors it injects are spread
  // uniformly across chip positions.  A chip is flipped when the low 5 bits of
  // the noise state fall below a threshold, giving flip probability thr/32.
  // ==========================================================================

  reg [7:0] noise;
  always @(posedge clk) begin
    if (!rst_n) noise <= 8'hA5;
    else        noise <= {noise[6:0], noise[7] ^ noise[5] ^ noise[4] ^ noise[3]};
  end

  // The four levels are placed in the four distinct regimes of the link:
  //   0, 1  lock holds and the data is clean
  //   2     lock is lost, but the 31-chip majority vote still decodes
  //   3     half the chips flip and the link finally breaks
  reg [4:0] jam_thresh;
  always @(*) begin
    case (jam_level)
      2'd0:    jam_thresh = 5'd2;   //  6.3% of chips, about  1.9 of 31
      2'd1:    jam_thresh = 5'd4;   // 12.5% of chips, about  3.9 of 31
      2'd2:    jam_thresh = 5'd11;  // 34.4% of chips, about 10.7 of 31
      default: jam_thresh = 5'd16;  // 50.0% of chips, about 15.5 of 31
    endcase
  end

  wire jam_hit = jam_en & (noise[4:0] < jam_thresh);

  // The channel: pick the source, then let the jammer corrupt it.
  wire rx_chip_src = loopback_en ? tx_chip  : rx_chip_s;
  wire rx_epoch    = loopback_en ? tx_epoch : rx_epoch_s;
  wire rx_chip     = rx_chip_src ^ jam_hit;

  // ==========================================================================
  // Receiver
  // ==========================================================================

  reg [4:0] rx_a, rx_b;      // local code generator
  reg [4:0] rx_idx;          // chip index within the local code period
  reg       rx_active;       // a correlation window is in progress
  reg       rx_slip;         // stall one chip to advance the local phase
  reg [4:0] match_cnt;       // running count of agreeing chips
  reg [4:0] m_last;          // match count of the most recent window
  reg [1:0] conf;            // lock confidence, 0..3
  // Only seven bits are needed: the eighth decided bit is concatenated
  // straight into rx_data, so an eight-bit register would carry one flop that
  // is written and never read.  (Verilator's UNUSEDSIGNAL check found that.)
  reg [6:0] rx_shift;
  reg [2:0] rx_bit;
  reg [7:0] rx_data;
  reg       rx_valid;

  wire locked = conf[1];     // conf >= 2

  // In epoch-slaved mode the epoch strobe arrives on the same clock as chip 0,
  // so chip 0 must be processed on that very cycle -- hence win_start_now and
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
  // accumulated -- that is the lab mode used to measure raw bit error rate
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

  // ---- bit and byte assembly ---------------------------------------------
  // Framing is implicit: the transmitter emits exactly 8 epochs per byte, so
  // counting 8 decided bits reproduces the byte boundaries.
  always @(posedge clk) begin
    if (!rst_n) begin
      rx_shift <= 7'h00;
      rx_bit   <= 3'd0;
      rx_data  <= 8'h00;
      rx_valid <= 1'b0;
    end else if (win_end && accumulate) begin
      rx_shift <= {rx_shift[5:0], rx_bit_val};
      if (rx_bit == 3'd7) begin
        rx_bit   <= 3'd0;
        rx_data  <= {rx_shift[6:0], rx_bit_val};
        rx_valid <= 1'b1;
      end else begin
        rx_bit <= rx_bit + 3'd1;
        if (rx_bit == 3'd0) rx_valid <= 1'b0;   // a new byte has started
      end
    end
  end

  // ==========================================================================
  // Outputs
  // ==========================================================================

  wire [7:0] status = {rx_valid, locked, acq_en & ~locked, m_last};

  assign uo_out  = disp_sel ? status : rx_data;
  assign uio_out = {tx_epoch, tx_chip, 6'b0};
  assign uio_oe  = 8'b1100_0000;   // uio[7:6] drive out, uio[5:0] are inputs

  // List all unused inputs to prevent warnings.  uio_in[7:6] are the two pins
  // this design drives as outputs, so their input paths are never read, and
  // cfg_b[5] is the reserved configuration bit.
  wire _unused = &{ena, cfg_b[5], uio_in[7:6], 1'b0};

endmodule
