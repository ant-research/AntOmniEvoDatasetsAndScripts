# Range Coding Encoder Implementation

When a task requires writing an encoder/compressor for a given decoder/decompressor that
uses arithmetic/range coding, follow this methodology.

## Methodology: Verify Forward, Then Invert

1. **First implement a Python decoder matching the C decoder** — translate the C
decoder line-by-line to Python. Do NOT skip this step.
2. **Verify the Python decoder** — run both the compiled C decoder and your Python
decoder on the same compressed input (or on crafted test inputs) and confirm byte-
identical output. This validates that your Python code correctly models the C code.
3. **Invert the verified Python decoder to create the encoder** — each decode
operation has a corresponding encode operation that narrows the same interval.
4. **Test round-trip** — encode then decode and verify the output matches the
original input.

## How Range Coding Works (Encoder Perspective)

A range coder maintains an interval [low, low + range) that represents all
possible compressed bitstreams consistent with the symbols encoded so far.

### Decoder state (what you read from the C code):
- `fraction`: the encoded value, read from the compressed stream
- `range`: the size of the current interval
- When `range < radix`: `range *= radix; fraction *= radix; fraction += gc() - 1`
  This reads a new byte from the compressed stream and expands the working
  precision.

### Encoder state (what you must implement):
- `low`: the lower bound of the current interval
- `range`: the size of the current interval (same as decoder)
- When `range >= radix * threshold`: output `low // radix + 1` (or similar
  normalization); `low = low % radix; range *= radix`

### The Key Inversion

The decoder decides a bit by computing:
```python
split = range * (counts[0] + 1) // (counts[0] + counts[1] + 2)
bit = 1 if fraction >= split else 0
if bit: fraction -= split; range -= split
else: range = split
counts[bit] += 1
```

The encoder must narrow the same interval:
```python
# Before encoding bit:
split = range * (counts[0] + 1) // (counts[0] + counts[1] + 2)

# For bit=0: interval is [low, low + split)
if bit == 0:
    range = split
# For bit=1: interval is [low + split, low + range)
else:
    low += split
    range -= split
counts[bit] += 1
```

After narrowing, normalize (output bytes) when range is small:
```python
while range < some_threshold:
    output_byte(low // radix + 1)  # +1 because decoder reads gc()-1
    low = low % radix
    range *= radix
```

### Finalization

After encoding all symbols, emit enough bytes to uniquely identify the final
interval. Typically:
```python
low += range - 1  # or low += 1, depending on convention
while range < radix:
    output_byte(low // radix + 1)
    low = low % radix
    range *= radix
output_byte(low // radix + 1)  # final byte
```

## Common Pitfalls

1. **Byte emission uses +1**: The decoder reads `gc() - 1`, so the encoder must
emit `byte_value + 1` where `0` maps to EOF. Byte values in the output stream
are in range [1, 255], not [0, 254].

2. **Integer division direction**: The split calculation uses floor division.
When the true split falls between two integers, the encoder and decoder must
agree on which side of the boundary a value falls. Always replicate the exact
C code's integer arithmetic, including C's truncation-toward-zero semantics.

3. **Normalization threshold**: The decoder normalizes when `range < radix`. The
encoder must normalize at the SAME threshold — if the encoder normalizes at a
different point, the byte boundaries won't align with the decoder's reads.

4. **Adaptive counts must be identical**: Both encoder and decoder update the
same counts array after every bit. If the encoder uses a different count update
order or different initial values, all subsequent bits will decode incorrectly.

5. **get_integer encoding**: The unary prefix uses `get_bit(++tmp + ctx)` reading
1s until a 0 is hit, or vice versa depending on the convention. Carefully check
whether the loop reads `while (!get_bit(...))` (reading 0s until 1) or
`while (get_bit(...))` (reading 1s until 0). This determines the order of bits
to encode for the unary prefix.