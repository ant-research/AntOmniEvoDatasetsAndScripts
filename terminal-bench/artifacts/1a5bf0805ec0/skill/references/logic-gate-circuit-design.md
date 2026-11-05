# Logic-Gate Circuit Design Reference

## Table of Contents
1. [Gate-Level Arithmetic](#gate-level-arithmetic)
2. [32-Bit Adder](#32-bit-adder)
3. [Comparator](#comparator)
4. [Iterative Circuits with Delay](#iterative-circuits-with-delay)
5. [Signal Allocation Strategy](#signal-allocation-strategy)
6. [Complete Design Pattern](#complete-design-pattern)

## Gate-Level Arithmetic

Logic-gate simulators that support AND, OR, XOR, NOT, and pass-through can implement
any boolean function. Key identities:
- Half adder: sum = a XOR b, carry = a AND b
- Full adder: uses two half adders + OR for carry
- Multiplexer: out = (sel AND a) OR (NOT sel AND b) — selects between a and b
- Constants: `outX = 0` for constant 0, `outY = ~outX` where outX is 0 for constant 1

## 32-Bit Adder

Build from 32 full adders chained by carry:
```
For bit i (i=0..31):
  sum_i = a_i XOR b_i XOR carry_i
  carry_{i+1} = (a_i AND b_i) OR (carry_i AND (a_i XOR b_i))
```
Gate count per bit: ~9 gates (2 XOR, 2 AND, 1 OR for carry; plus the signal definitions)
Total for 32-bit adder: ~32 × 9 = 288 gates + 2 for carry in/out = ~290 gates

Implementation pattern in a gate generator:
```python
def add32(a_bits, b_bits, carry_in=None):
    """Add two 32-bit numbers. Returns (sum_bits, carry_out)."""
    carry = carry_in if carry_in else const_0()
    sums = []
    for i in range(32):
        axb = xor(a_bits[i], b_bits[i])
        s = xor(axb, carry)
        c1 = and_(a_bits[i], b_bits[i])
        c2 = and_(axb, carry)
        carry = or_(c1, c2)
        sums.append(s)
    return sums, carry
```

## Comparator

To compare A >= B, compute A - B using the adder and check if the result is
non-negative (no borrow). With two's complement:
- Compute A + NOT(B) + 1 (i.e., A - B = A + (~B + 1))
- If carry_out = 1, then A >= B
- If carry_out = 0, then A < B

NOT(B) is free — just invert each bit. The +1 is the carry_in to the adder.
Gate count: same as 32-bit adder (~290 gates) + 32 NOT gates for inversion.

```python
def gte(a_bits, b_bits):
    """Return a single signal that is 1 iff A >= B."""
    b_inv = [not_(b) for b in b_bits]
    _, carry_out = add32(a_bits, b_inv, carry_in=const_1())
    return carry_out  # 1 if A >= B, 0 if A < B
```

## Iterative Circuits with Delay

The simulator runs 32,000 steps with event-driven propagation. Each step,
signals update based on their gate definitions. A signal that depends on
another takes one step to see the new value.

**Key insight**: Use chains of pass-through gates as 1-step delay registers:
```
reg_new = computed_value   # combinational logic
outX = reg_new             # delay: available next step
```

For iterative algorithms (Newton's method, Fibonacci iteration):
1. Allocate register groups for current state (e.g., fib_a, fib_b, counter)
2. Build combinational logic for the next state (e.g., fib_a_new = fib_b, fib_b_new = fib_a + fib_b)
3. Connect registers: `outR = outR_new` (pass-through creates 1-step delay)
4. The 32,000 steps provide the clock — each step performs one iteration
5. Use the input-initialized signals (0-31) to compute the initial counter value
   or the number of iterations needed

**CRITICAL: Register initialization pitfall**
All signals >= 32 start at value 0 on step 0. This means fa=0, fb=0, k=0
at the start. For Fibonacci, fb MUST be 1 initially (fib(1)=1). Fix this with
an INIT signal that detects k==0:
```python
# INIT = NOT(OR of all k bits) -> 1 when k==0, 0 otherwise
k_any = const_0()
for bit in k_reg:
    k_any = or_(k_any, bit)
INIT = not_(k_any)

# On INIT step, force correct initial values via MUX:
# fa_next = MUX(INIT, fib(1)_value, MUX(cont, fb[i], fa[i]))
#   When INIT=1: fa_next = 1 (fib(1)), regardless of cont
#   When INIT=0: fa_next = fb[i] if cont, else fa[i] (normal iteration)
# fb_next = MUX(INIT, fib(2)_value, MUX(cont, fa+fb[i], fb[i]))
#   When INIT=1: fb_next = 1 (fib(2)=1)
#   When INIT=0: fb_next = fa+fb[i] if cont, else fb[i]
#   Note: fib(1)=1 means bit 0 = c1, bits 1-31 = c0
```
Without INIT logic, the circuit will permanently output 0 because fb never
becomes 1, so fa_new = fb = 0 every step. This is the #1 cause of circuit
failure in logic-gate tasks.

**Register pattern**:
```python
def make_reg(next_value_signal):
    """Create a 1-step delay register."""
    r = new_sig()
    lines.append(f"out{r} = out{next_value_signal}")
    return r
```

## Signal Allocation Strategy

- Signals 0-31 carry the input N (bit i = (N >> i) & 1).
- **CRITICAL: You MUST add identity gates for signals 0-31** (`out0 = out0`,
  `out1 = out1`, ..., `out31 = out31`). The simulator's default gate type for
  undefined signals is OP_CONST_0, which overwrites stdin-initialized input
  values to 0 on step 0. Without identity gates, the circuit always sees N=0
  regardless of what the user passes on the command line. This is the #1 cause
  of circuits that produce constant wrong output (e.g., always output 1).
- Start your signal allocation at 32 or higher (after the 32 input identity gates)
- Build all combinational logic FIRST, then place the 32 output signals LAST
- The simulator outputs the LAST 32 signals in the file
- Final 32 signals must be your result bits (LSB first)
- Track signal allocation with a counter to avoid collisions

```python
class CircuitBuilder:
    def __init__(self):
        self.next_sig = 32  # Start after input signals
        self.lines = []
    
    def new_sig(self):
        s = self.next_sig
        self.next_sig += 1
        return s
    
    def const_0(self):
        s = self.new_sig()
        self.lines.append(f"out{s} = 0")
        return s
    
    def const_1(self):
        z = self.const_0()
        s = self.new_sig()
        self.lines.append(f"out{s} = ~out{z}")
        return s
```

## Complete Design Pattern

For tasks like fib(isqrt(N)):

1. **Unroll isqrt** as a lookup table or implement iteratively:
   - For N up to ~2^16 (65536), isqrt ranges are bounded — can use range detection:
     For each k, is N >= k*k? Set bit k of isqrt result if (k+1)^2 > N >= k^2.
   - Chain comparisons: detect the largest k where k*k <= N.
   - Gate count: ~290 per comparison × ~8 key thresholds = ~2,320 gates

2. **Iterate Fibonacci** using the step-delay register pattern:
   - Two 32-bit register groups: fib_a and fib_b
   - Each step: fib_a_new = fib_b, fib_b_new = fib_a + fib_b (mod 2^32)
   - A counter decrements each step; when it reaches 0, output stabilizes
   - Gate count: 2 × 32 delay registers + 1 adder = ~290 + 64 = ~354 gates per step

3. **Gate budget check**: With 32,000 line limit, budget allows ~8-10 comparisons
   for isqrt detection + 1 Fibonacci iteration circuit + output MUX.
   Total: ~3,000-5,000 gates — well within budget.

4. **Alternative: precompute Fibonacci values** for each isqrt range and output
   the appropriate constant via MUX selection based on range detection.
   This uses ~32 × (number of ranges) gates for constants + MUX logic.
   For isqrt up to 220 (max in typical test), that's ~221 ranges,
   each needing 32 constant bits = ~221 × 32 = 7,072 constant gates + MUX overhead.
   Still under 32,000 but more gates than iterative approach.
   Best for small input ranges where the number of distinct Fibonacci outputs is manageable.