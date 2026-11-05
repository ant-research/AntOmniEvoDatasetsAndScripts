# Binary Reverse Engineering: Algorithm Recreation

## Table of Contents
1. [Core principle](#core-principle)
2. [Methodology](#methodology)
3. [Extracting floating-point constants](#extracting-floating-point-constants)
4. [Dynamic validation with GDB](#dynamic-validation-with-gdb)
5. [Anti-patterns](#anti-patterns)

## Core principle

**Complete algorithm understanding BEFORE writing any implementation code.**
Writing code from partial understanding then tweaking parameters empirically
never converges: the parameter space is too large and errors compound across
interdependent values. Observed failure: 65+ turns of empirical tweaking
without reaching a correct solution.

## Methodology

### Step 1: Full behavioral observation
Run the binary and capture ALL output:
```bash
./mystery > /tmp/stdout.txt 2>&1; echo "exit: $?"
cat /tmp/stdout.txt   # progress messages, format hints, pipeline stages
file image.ppm         # output file type and dimensions
```

**Critical**: Progress messages reveal the algorithm. "Downsampled with ffmpeg"
means a multi-resolution rendering pipeline exists. "Supersampled" means
anti-aliasing via multiple samples per pixel. Ignoring these clues leads to
fundamental algorithm errors that empirical parameter matching cannot fix.

### Step 2: Enumerate user-defined functions
```bash
nm <binary> | grep ' [Tt] ' | grep -v '^$
' | sort
```
Function names reveal the algorithm structure (e.g., trace, sphere_intersect,
sky_color = raytracer; parse, compile, emit = compiler).

### Step 3: Extract ALL floating-point constants
Write a Python script to extract constants from .rodata (see section below).
Do NOT read them manually from objdump hex output — it is error-prone and
tedious. A 20-line script extracts all constants at once.

### Step 4: Disassemble key functions systematically
For each user-defined function:
```bash
objdump -d <binary> | awk '/^[0-9a-f]+ <FUNCNAME>:/{found=1} found{print} /^$/{if(found) exit}'
```
For ARM aarch64: literal pools (constant data) follow each function body.
Include 20+ lines after the last instruction to capture the pool.

### Step 5: Dynamic validation with GDB
Set breakpoints at key functions and print calculated values to verify
your understanding matches the binary's actual behavior (see section below).

### Step 6: Implement once, then verify
Only after full algorithm understanding, write the implementation. Compare
output pixel-by-pixel (or byte-by-byte) with the original. If >1% mismatch,
the algorithm is wrong — return to steps 3-5. Do NOT attempt empirical
parameter adjustment.

## Extracting floating-point constants

Use a Python script to systematically extract all float/double values from
the binary's read-only data section:

```python
import struct, subprocess

# Get .rodata offset and size from readelf
result = subprocess.run(['readelf', '-S', '<binary>'], capture_output=True, text=True)
for line in result.stdout.splitlines():
    if '.rodata' in line:
        parts = line.split()
        offset = int(parts[4], 16)
        size = int(parts[5], 16)
        break

with open('<binary>', 'rb') as f:
    f.seek(offset)
    data = f.read(size)

# Extract 4-byte floats and 8-byte doubles
floats = []
for i in range(0, len(data) - 3, 4):
    val = struct.unpack('<f', data[i:i+4])[0]
    if -1e6 < val < 1e6 and val != 0 and not (val != val):  # finite, nonzero
        floats.append((offset + i, val))

# Print sorted by value for pattern detection
for addr, v in sorted(floats, key=lambda x: x[1]):
    print(f'  0x{addr:06x}: {v:.6f}')
```

Map each constant to its algorithmic role based on magnitude:
- Very small (<0.01): epsilon values, ray offset for shadow acne
- Small (0.01-1.0): ratios, normal components, color multipliers
- Medium (1.0-10.0): dimensions, radii, coordinates, FOV-related
- Large (>100): image dimensions, iteration counts

Group constants by proximity in memory — constants used by the same function
are typically stored together in the same literal pool.

## Dynamic validation with GDB

For rendering algorithms, verify calculated values against the binary:

```bash
# Break at start of rendering loop or key function
gdb -batch \
  -ex 'set pagination off' \
  -ex 'break trace' \
  -ex 'run' \
  -ex 'print/x $x0' \
  -ex 'print/x $x1' \
  -ex 'info registers s0 s1 s2 s3' \
  ./mystery
```

For ARM aarch64 floating-point: values are in s0-s31 (single) or d0-d31
(double) registers. Set breakpoints inside the rendering loop and print
ray direction, intersection parameters, and shading values to verify
they match your extracted algorithm.

## Anti-patterns

1. **Empirical parameter tweaking**: Changing one parameter, running, comparing,
   and repeating. This cannot converge because parameters are interdependent
   (camera FOV affects coordinate mapping AND sphere apparent size), the
   search space is too large for manual exploration, and each trial wastes
   1-2 turns on compilation + execution. Instead: extract parameters directly
   from the binary.

2. **Partial disassembly analysis**: Reading only a few functions then guessing
   the rest. Instead: disassemble EVERY user-defined function and trace
   inter-function data flow (what trace() passes to sphere_intersect(), what
   values sky_color() computes, etc.).

3. **Ignoring stdout messages**: Progress output contains algorithm hints.
   "downsampled", "supersampled", "anti-aliased" all indicate multi-pass
   rendering that must be replicated. "Creating high-resolution image" followed
   by a standard-resolution output means supersampling is used internally.

4. **Manual constant reading**: Reading hex from objdump is error-prone.
   Instead: write a Python script to extract all constants at once and print
   them as decimal floats.