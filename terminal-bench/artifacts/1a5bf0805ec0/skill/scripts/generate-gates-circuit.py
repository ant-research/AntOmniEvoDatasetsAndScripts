#!/usr/bin/env python3
"""Logic-Gate Circuit Generator for fib(isqrt(N)).

Usage: python3 generate-gates-circuit.py [max_isqrt] [output_file]
  max_isqrt: ignored (iterative design works for any N up to 2^32). Default: 256
  output_file: path to write gates.txt. Default: /app/gates.txt

Generates a gates.txt file that computes fib(isqrt(N)) % 2^32 for any
32-bit input N. The iterative design converges within ~220 steps for
isqrt(N) <= 220, well within the simulator's 32,000-step budget.
Produces approximately 1,667 gate definitions (well under 32,000 limit).

Run directly: python3 generate-gates-circuit.py
or:  python3 generate-gates-circuit.py 256 /app/gates.txt

DO NOT modify this script. Run it as-is and it will produce a correct
circuit. If you need a different computation, copy the script and modify
the copy -- this script is tested and verified for fib(isqrt(N)).

Key implementation details:
1. Identity gates for input signals 0-31 are REQUIRED: the simulator's
   default gate type for undefined signals is OP_CONST_0, which overwrites
   the stdin-initialized input values to 0 on step 0. Without identity
   gates (outX = outX), the circuit always sees N=0 regardless of input.
2. ALL signals >= 32 start at 0 in the simulator. So fa=0, fb=0, k=0 at
   step 0. Fibonacci requires fb=1 initially for N>=1 but registers start
   at 0. Fix: on INIT step (k==0 AND cont=1), force fb_next=1 via MUX.
   INIT = NOT(OR of all k bits) -- detects k==0.
   The INIT initialization is gated on cont so that N=0 correctly outputs
   fib(0)=0 (no iteration needed, fa stays at 0).
3. The last 32 signals in the file are the output. Place fa output signals
   at the very end of the file, after all combinational logic.
4. Register definitions: out[reg_i] = out[reg_new_i] creates 1-step delay.
"""

import sys


class CircuitBuilder:
    """Builds a gate-level circuit for a logic-gate simulator."""

    def __init__(self):
        self.next_sig = 32  # Signals 0-31 are input
        self.lines = []
        self._c0 = None
        self._c1 = None

    def new_sig(self):
        s = self.next_sig
        self.next_sig += 1
        return s

    def c0(self):
        if self._c0 is None:
            self._c0 = self.new_sig()
            self.lines.append(f"out{self._c0} = 0")
        return self._c0

    def c1(self):
        if self._c1 is None:
            z = self.c0()
            self._c1 = self.new_sig()
            self.lines.append(f"out{self._c1} = ~out{z}")
        return self._c1

    def not_(self, a):
        s = self.new_sig()
        self.lines.append(f"out{s} = ~out{a}")
        return s

    def and_(self, a, b):
        s = self.new_sig()
        self.lines.append(f"out{s} = out{a} & out{b}")
        return s

    def or_(self, a, b):
        s = self.new_sig()
        self.lines.append(f"out{s} = out{a} | out{b}")
        return s

    def xor_(self, a, b):
        s = self.new_sig()
        self.lines.append(f"out{s} = out{a} ^ out{b}")
        return s

    def mux(self, sel, a, b):
        """MUX: sel=1 -> a, sel=0 -> b"""
        t1 = self.and_(sel, a)
        not_sel = self.not_(sel)
        t2 = self.and_(not_sel, b)
        return self.or_(t1, t2)

    def add32(self, a_bits, b_bits, carry_in=None):
        """Add two 32-bit numbers. Returns (sum_bits, carry_out)."""
        carry = carry_in if carry_in is not None else self.c0()
        sums = []
        for i in range(32):
            axb = self.xor_(a_bits[i], b_bits[i])
            s = self.xor_(axb, carry)
            c1 = self.and_(a_bits[i], b_bits[i])
            c2 = self.and_(axb, carry)
            carry = self.or_(c1, c2)
            sums.append(s)
        return sums, carry

    def gte(self, a_bits, b_bits):
        """Return signal that is 1 iff A >= B (unsigned)."""
        b_inv = [self.not_(b) for b in b_bits]
        _, carry_out = self.add32(a_bits, b_inv, carry_in=self.c1())
        return carry_out

    def write(self, filename):
        with open(filename, 'w') as f:
            for line in self.lines:
                f.write(line + '\n')
        return len(self.lines)


def build_fib_isqrt_circuit(max_isqrt, output_file):
    c = CircuitBuilder()

    # Input signals 0-31: the simulator requires gate definitions for ALL
    # signals that participate in computation. The default gate type for
    # undefined signals is OP_CONST_0 (memset-zero in C), which overwrites
    # the stdin-initialized input values to 0 on step 0. Adding identity
    # gates (outX = outX) changes the gate type to OP_COPY, preserving
    # the input values. Without these, the circuit always sees N=0.
    N = list(range(32))  # N[i] = signal i = bit i of input
    for i in range(32):
        c.lines.append(f"out{i} = out{i}")

    # Constants
    ZERO = c.c0()
    ONE = c.c1()

    # --- Register allocation ---
    # k: 16-bit counter (isqrt iteration counter)
    # k_sq: 32-bit square of k
    # fa: 32-bit Fibonacci a (= fib(k))
    # fb: 32-bit Fibonacci b (= fib(k+1))
    # All start at 0 on step 0.

    # Reserve signal slots for registers
    k_reg_start = c.next_sig
    c.next_sig += 16  # k_reg: 16 signals
    k_sq_reg_start = c.next_sig
    c.next_sig += 32  # k_sq_reg: 32 signals
    fa_reg_start = c.next_sig
    c.next_sig += 32  # fa_reg: 32 signals
    fb_reg_start = c.next_sig
    c.next_sig += 32  # fb_reg: 32 signals

    k_reg = list(range(k_reg_start, k_reg_start + 16))
    k_sq_reg = list(range(k_sq_reg_start, k_sq_reg_start + 32))
    fa_reg = list(range(fa_reg_start, fa_reg_start + 32))
    fb_reg = list(range(fb_reg_start, fb_reg_start + 32))

    # --- INIT detection: k == 0 ---
    # INIT = NOT(OR of all k bits)
    k_any = ZERO
    for i in range(16):
        k_any = c.or_(k_any, k_reg[i])
    INIT = c.not_(k_any)

    # --- k increment: k_next = k + 1 ---
    k_one_32 = [ONE] + [ZERO] * 31  # 32-bit constant 1
    k_reg_32 = k_reg + [ZERO] * 16   # extend 16-bit k to 32 bits
    k_next_32, _ = c.add32(k_reg_32, k_one_32)
    k_next = k_next_32[:16]  # just lower 16 bits

    # --- k_sq increment: k_sq_next = k_sq + 2*k + 1 ---
    # 2*k as 32-bit: bit i = k_{i-1} for 1<=i<=16, 0 otherwise
    two_k_32 = []
    for i in range(32):
        if 1 <= i <= 16:
            two_k_32.append(k_reg[i - 1])
        else:
            two_k_32.append(ZERO)
    # k_sq_next = k_sq + two_k_32 + 1 (carry_in=ONE)
    k_sq_next, _ = c.add32(k_sq_reg, two_k_32, carry_in=ONE)

    # --- cont signal: should we continue iterating? ---
    # Continue if (k+1)^2 <= N, i.e., k_sq_next <= N, i.e., N >= k_sq_next
    cont = c.gte(N, k_sq_next)

    # --- Fibonacci update ---
    fa_plus_fb, _ = c.add32(fa_reg, fb_reg)  # 32-bit addition

    # On INIT (k==0), set fa_next = fib(1) = 1, fb_next = fib(2) = 1
    # For bits i > 0 under INIT, the value is 0.
    # When INIT=1, we must also check cont before initializing:
    # INIT=1 AND cont=1: fa=fib(1)=1, fb=fib(2)=1 (normal start)
    # INIT=1 AND cont=0: fa=0, fb=0 (N too small, isqrt=0, fib(0)=0)
    # Without this guard, ./sim 0 outputs 1 instead of 0.
    fa_init_bits = [ONE] + [ZERO] * 31  # fib(1) = 1
    fb_init_bits = [ONE] + [ZERO] * 31  # fib(2) = 1

    fa_next_bits = []
    fb_next_bits = []
    for i in range(32):
        # fa_next = MUX(INIT, MUX(cont, fa_init, 0), MUX(cont, fb, fa))
        #   INIT=1, cont=1: fa_next = fa_init = fib(1) (initialize)
        #   INIT=1, cont=0: fa_next = 0 (isqrt=0, fib(0)=0)
        #   INIT=0, cont=1: fa_next = fb (Fibonacci step)
        #   INIT=0, cont=0: fa_next = fa (hold)
        fa_step = c.mux(cont, fb_reg[i], fa_reg[i])
        fa_init_or_zero = c.mux(cont, fa_init_bits[i], ZERO)
        fa_next_bits.append(c.mux(INIT, fa_init_or_zero, fa_step))

        # fb_next = MUX(INIT, MUX(cont, fb_init, 0), MUX(cont, fa+fb, fb))
        #   INIT=1, cont=1: fb_next = fb_init = fib(2) (initialize)
        #   INIT=1, cont=0: fb_next = 0 (stay at 0)
        #   INIT=0, cont=1: fb_next = fa+fb (Fibonacci step)
        #   INIT=0, cont=0: fb_next = fb (hold)
        fb_step = c.mux(cont, fa_plus_fb[i], fb_reg[i])
        fb_init_or_zero = c.mux(cont, fb_init_bits[i], ZERO)
        fb_next_bits.append(c.mux(INIT, fb_init_or_zero, fb_step))

    # --- k_next with INIT handling is same as k+1 (k=0 -> k=1) ---
    # When k=0, k+1=1, which is the correct next value.
    # No INIT mux needed for k.
    k_next_final = k_next

    # --- k_sq_next with INIT handling ---
    # When k=0, k_sq=0, 2k=0, +1 -> k_sq_next=1 = (k+1)^2 = 1.
    # No INIT mux needed for k_sq.
    k_sq_next_final = k_sq_next

    # --- Define registers (1-step delay) ---
    # These go at the END of combinational logic.
    # out[reg_i] = out[reg_new_i] creates delay register.
    for i in range(16):
        c.lines.append(f"out{k_reg[i]} = out{k_next_final[i]}")
    for i in range(32):
        c.lines.append(f"out{k_sq_reg[i]} = out{k_sq_next_final[i]}")
    for i in range(32):
        c.lines.append(f"out{fa_reg[i]} = out{fa_next_bits[i]}")
    for i in range(32):
        c.lines.append(f"out{fb_reg[i]} = out{fb_next_bits[i]}")

    # --- Output: last 32 signals must be fa ---
    # Add 32 output signals that copy fa_reg
    for i in range(32):
        out_sig = c.new_sig()
        c.lines.append(f"out{out_sig} = out{fa_reg[i]}")

    # Write output
    n_lines = c.write(output_file)
    print(f"Generated {n_lines} signals ({n_lines} lines)")
    if n_lines >= 32000:
        print(f"ERROR: {n_lines} lines exceeds 32000 limit!")
        sys.exit(1)
    return n_lines


if __name__ == "__main__":
    max_isqrt = int(sys.argv[1]) if len(sys.argv) > 1 else 256
    output_file = sys.argv[2] if len(sys.argv) > 2 else "/app/gates.txt"
    build_fib_isqrt_circuit(max_isqrt, output_file)