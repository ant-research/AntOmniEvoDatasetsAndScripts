# Differential Cryptanalysis of FEAL-like Ciphers

This reference covers the methodology for performing differential
cryptanalysis on Feistel-network ciphers with modular-addition
based round functions (like FEAL), and how to implement a working
chosen-plaintext attack.

## FEAL Structure Recap

A FEAL-like cipher with N rounds works as:
1. Split 64-bit plaintext into L0 (upper 32) and R0 (lower 32)
2. Initial whitening (optional): L0' = L0 XOR k0, R0' = R0 XOR k1
3. For round i (0 to N-1): Li+1 = Ri', Ri+1 = Li' XOR F(Ri' XOR k_{i+2})
4. Final swap: ciphertext = (R_N || L_N)

The F function takes a 32-bit input and produces a 32-bit output
using G-functions that combine byte-level modular addition with
rotation.

## Key Insight: 16-bit Seed Constraint

When each round key is derived from a 16-bit seed (65536 possible
values), the effective keyspace per round key is small enough to
attack individually but too large for full brute force of all keys
simultaneously.

## Differential Cryptanalysis Step-by-Step

### Step 1: Find a High-Probability Differential Characteristic

For FEAL's G function using modular addition (not XOR):
- The G function: g(a, b, x) = ROL2((a + b + x) mod 256)
- Key property: if input difference delta_a XOR delta_b = 0x80,
  then (a + b + x) XOR ((a XOR delta_a) + (b XOR delta_b) + x) =
  0x80 with probability 1 (due to carry behavior of addition)
- This means: for ANY pair of inputs with difference 0x80,
  the output difference of G is ROL2(0x80) = 0x02 with
  probability 1

For the full F function:
- F uses 4 G-function calls with cascade structure
- An input difference of 0x80800000 to F produces output
  difference 0x02000000 with probability 1 (this is the
  zero-round characteristic)

### Step 2: Build the Distinguisher for the Last Round

For a 4-round FEAL attacking key[5] (last round key):
1. Choose plaintext pairs (P, P*) with input difference chosen
   so that after 3 rounds the difference is predictable
2. From ciphertext pair (C, C*), peel off the last round:
   - R4 = C_right, L4 = C_left
   - R4* = C*_right, L4* = C*_left
   - The last round computes: L4 = R3 XOR F(R4 XOR k5)
   - So: L4 XOR L4* = F(R4 XOR k5) XOR F(R4* XOR k5)
3. For each candidate k5_seed (0-65535):
   - Compute k5 = (seed * multiplier) & 0xFFFFFFFF
   - Compute F(R4 XOR k5) and F(R4* XOR k5)
   - Check if F(R4 XOR k5) XOR F(R4* XOR k5) equals the
     expected differential
4. The correct k5 will satisfy the differential for ALL pairs;
   wrong k5 values will fail for some pairs

### Step 3: Extract the F Function and Test Its Differentials

**Step 3a: Copy the EXACT F and g from /app/feal.py**
Do NOT use pseudocode from this reference. The constant
arguments to g() (0 or 1) vary between FEAL variants. Copy the
g and f_function definitions verbatim from feal.py. Verify your
copy matches: F(0x12345678) from your code must equal
feal.f_function(0x12345678).

**Step 3b: Empirically find F-function deterministic differentials**
Instead of tracing through G-call chains (error-prone), use
this Python procedure to FIND them automatically:
```python
f_diffs = {}
for byte_mask in [0x80, 0x8000, 0x800000, 0x80000000,
                   0x8080, 0x808000, 0x80800000,
                   0x808080, 0x80808000, 0x80808080]:
    diffs = set()
    for _ in range(1000):
        a = random.getrandbits(32)
        d = f_function(a) ^ f_function(a ^ byte_mask)
        diffs.add(d)
        if len(diffs) > 1:
            break  # not deterministic
    if len(diffs) == 1:
        f_diffs[byte_mask] = diffs.pop()
print('Deterministic F differentials:', f_diffs)
```
This runs in seconds and directly tells you which input
differences produce deterministic output differences in F.
For FEAL-like ciphers, byte masks involving 0x80 in certain
positions will be deterministic due to modular addition's
carry property. PICK the simplest one (fewest 0x80 bytes)
for cleaner analysis.

### Step 4: Construct the 3-Round Characteristic

For a 4-round FEAL attacking key[5] (last round), you need a
3-round differential characteristic. The key trick exploits
the PRE-ROUND MIXING step common in FEAL variants:

**The pre-round mixing trick**: If feal.py has a step like
`right ^= left` before the rounds (pre-round mixing), then
choosing plaintext pairs where DIFF_LEFT = DIFF_RIGHT causes
the mixing to CANCEL the difference:
- ΔR_after_mix = ΔR_before ^ ΔL_before = X ^ X = 0
- This gives F-input difference of 0 in round 0 (deterministic!)
- Round 0's F-output diff = 0
- ΔR1 = ΔL0' ^ 0 = X (the original half-difference)
- Round 1 gets F-input diff = X, which you tested in Step 3b

So use plaintext_diff = (X << 32) | X where X is an
F-input difference that produced a deterministic F-output
in Step 3b. For example, if F_diffs[0x80800000] = 0x02000000,
then plaintext_diff = 0x8080000080800000.

After 2 rounds (both deterministic):
- ΔL2 = ΔR1 = X (e.g., 0x80800000)
- ΔR2 = ΔL1 ^ F_output_diff = 0 ^ F_output_diff
  (e.g., 0 ^ 0x02000000 = 0x02000000)

After round 2 (probabilistic — round 2's F-input diff may
not be deterministic):
- ΔL3 = ΔR2 (e.g., 0x02000000) — THIS IS DETERMINISTIC
- ΔR3 = ΔL2 ^ ΔF_round2 — varies per pair

The CRITICAL point: ΔL3 is deterministic even though round 2
is probabilistic. You predict ΔL3 = ΔR2 (known from the
2-round characteristic), regardless of what happens in round 2.

### Step 5: Extract Intermediate Values from Ciphertext

This is the most important and most frequently missed step.
The ciphertext is NOT simply (R_final || L_final). Most FEAL
variants apply post-round mixing (e.g., `left ^= right`) after
the loop but before the final merge. You MUST account for this.

Read feal.py's encrypt function carefully. For a cipher that
does `left ^= right` after the loop and returns `merge(right,
left)`, the ciphertext layout is:
```
C_upper = right_after_loop
C_lower = left_after_loop ^ right_after_loop
```

From this:
```python
R4 = c >> 32                          # right after round 3
L4_xor_R4 = c & 0xFFFFFFFF            # left after post-mix
R3 = R4 ^ L4_xor_R4                   # = R4 ^ (L4 ^ R4) = L4 = R3
# Equivalently: R3 = (c >> 32) ^ (c & 0xFFFFFFFF)
L3 = R4 ^ F(R3 ^ k5)                  # needs candidate k5
```

R3 is directly computable from ciphertext WITHOUT knowing
k5. This is the key insight. For a pair:
```python
R3_1 = (c1 >> 32) ^ (c1 & 0xFFFFFFFF)
R3_2 = (c2 >> 32) ^ (c2 & 0xFFFFFFFF)
R4_1 = c1 >> 32
R4_2 = c2 >> 32
```
If your feal.py does NOT have post-round mixing, the
extraction is simpler: R3 = c & 0xFFFFFFFF, R4 = c >> 32.
Always verify by reading the encrypt function carefully.

### Step 6: Implement the O(65536) Attack

```python
def attack(encrypt_fn):
    # 1. Copy EXACT g and F from /app/feal.py (Step 3a)
    # 2. Find deterministic F differentials (Step 3b)
    # 3. Compute expected_delta_L3 from 2-round characteristic (Step 4)
    # 4. Get key derivation formula from feal.py
    # 5. Read encrypt() to determine ciphertext layout (Step 5)

    X = <F-input diff from Step 3b>  # e.g. 0x80800000
    F_out = <F-output diff from Step 3b>  # e.g. 0x02000000
    expected_delta_L3 = 0 ^ F_out  # = F_out (from 2-round char)
    plaintext_diff = (X << 32) | X  # pre-round mixing trick
    KEY_CONST = <from feal.py>  # multiplier in key derivation

    # Generate chosen-plaintext pairs
    pairs = []
    for _ in range(8):
        p1 = random.getrandbits(64)
        p2 = p1 ^ plaintext_diff
        c1 = encrypt_fn(p1)
        c2 = encrypt_fn(p2)
        pairs.append((c1, c2))

    # Recover k5: only 65536 candidates
    for seed in range(65536):
        k5 = (seed * KEY_CONST) & 0xFFFFFFFF
        all_match = True
        for c1, c2 in pairs:
            # Extract intermediate values (Step 5)
            R3_1 = (c1 >> 32) ^ (c1 & 0xFFFFFFFF)
            R3_2 = (c2 >> 32) ^ (c2 & 0xFFFFFFFF)
            R4_1 = c1 >> 32
            R4_2 = c2 >> 32
            # Check: does this k5 produce the expected delta L3?
            delta_L3 = (R4_1 ^ R4_2) ^ (F(R3_1 ^ k5) ^ F(R3_2 ^ k5))
            if delta_L3 != expected_delta_L3:
                all_match = False
                break
        if all_match:
            return k5
    return 0
```

VERIFY: Test attack against feal.py first, then also test
against feal_in_c (from /tests/) to ensure compatibility.
The ciphertext layout extraction MUST match the actual
encrypt function — if it has post-round mixing, R3 = (c >> 32)
^ (c & 0xFFFFFFFF); if not, R3 = c & 0xFFFFFFFF.

## Common Mistakes in FEAL Attacks

1. Using XOR-based differential analysis when G uses addition:
   addition and XOR have different carry propagation. Use the
   0x80 difference property of modular addition.
2. Guessing key[5] as a full 32-bit value: with 16-bit seeds,
   iterate over 65536 seeds and compute the 32-bit key.
3. Not matching the exact ciphertext layout: FEAL variants
   with post-round mixing (left ^= right) have R3 = C_upper
   XOR C_lower, NOT R3 = C_lower. Getting this wrong makes
   the attack extract wrong intermediate values and fail silently.
4. Using heuristic scoring instead of hard filtering: the correct
   key MUST produce delta_L3 = expected_value for ALL pairs.
   A key that only scores highest is not reliable — filter to
   exactly one candidate.
5. Testing only against feal.py when the verifier uses feal_in_c:
   always check /tests/ for the actual validation mechanism.
6. **PEELING ALL ROUNDS WITH NESTED LOOPS**: Do NOT iterate
   over k5, k4, k3, k2 in nested 65536^4 loops — this takes
   hundreds of thousands of hours. The correct approach uses
   DIFFERENTIAL FILTERING to recover k5 alone in a single
   65536-iteration loop, checking delta_L3 against the expected
   value from the 3-round characteristic.
7. **COPYING THE F FUNCTION FROM THIS REFERENCE**: The G-function
   constant arguments (0 or 1) in the pseudocode above are
   EXAMPLES. Your /app/feal.py may use different constants.
   Always copy F and g directly from /app/feal.py and verify
   with a test value.
8. **CHECKING WRONG CONSTRAINTS ON L3_DIFF**: Do NOT check
   whether L3_diff is the same across different pairs — different
   plaintexts produce different L3 values even with the correct
   key, so L3_diff will naturally vary. Instead, check whether
   L3_diff equals the PREDICTED VALUE from the 3-round
   characteristic (e.g., 0x02000000). This predicted value is
   the same across all pairs that follow the characteristic.
9. **SEARCHING FOR SPECIFIC R3_DIFF VALUES IN RANDOM PAIRS**:
   Do NOT generate random plaintext pairs hoping to find ones
   with R3_diff = 0x80808080 (probability ~2^-32). Instead,
   USE CHOSEN PLAINTEXTS with the pre-round mixing trick
   (Step 4) to create pairs where the differential characteristic
   is guaranteed, then check delta_L3 for each k5 candidate.