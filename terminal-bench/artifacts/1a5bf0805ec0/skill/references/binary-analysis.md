# Binary Analysis & Secret Extraction

## Table of Contents
1. [Static analysis workflow](#static-analysis-workflow)
2. [XOR brute-force with known plaintext](#xor-brute-force-with-known-plaintext)
3. [Anti-debugging bypass strategies](#anti-debugging-bypass-strategies)

## Static analysis workflow

When a task involves extracting secrets from a compiled binary:

1. `file <binary>` — identify architecture and format
2. `strings <binary>` — find readable strings (format strings, messages, API names)
3. Pay attention to format strings like `"Secret: %s"` — they indicate the actual data is stored separately (usually in `.data`) and passed at runtime
4. `objdump -s -j .data <binary>` — examine the data section for encoded/obfuscated content
5. `objdump -s -j .rodata <binary>` — examine read-only data for embedded constants

Prefer static analysis (strings, objdump) over dynamic analysis (GDB, running the program) when:
- The binary has anti-debugging protections (ptrace calls detected in strings/disassembly)
- The secret can be located and decoded from the binary's data sections directly
- Running the program risks triggering security checks that hide or destroy the secret

## XOR brute-force with known plaintext

The most common binary secret obfuscation is single-byte XOR encoding. When the task specifies a format (e.g., `FLAG{...}`, a specific prefix, or a hash format), use it as known plaintext to find the XOR key:

```python
encoded = bytes([0x04, 0x0e, 0x03, 0x05, ...])  # from objdump -s -j .data
known_prefix = b"FLAG{"  # from task description

for key in range(256):
    candidate = bytes([b ^ key for b in encoded])
    if candidate.startswith(known_prefix):
        print(f"Key: 0x{key:02x}")
        print(f"Decoded: {candidate}")
        break
```

Key technique: the task description often reveals the expected format (e.g., "secret key is in the format `FLAG{...}`"). Use this as the known plaintext prefix to constrain the brute-force search to a single valid key.

## Anti-debugging bypass strategies

Common anti-debugging techniques and how to handle them:

- **ptrace(PTRACE_TRACEME)**: The binary calls `ptrace(0, 0, 0, 0)` at startup. If it returns -1, the program detects a debugger. Static analysis bypasses this entirely.
- **ptrace(PTRACE_ATTACH)**: Secondary check calling `ptrace(17, ...)`. Same static analysis bypass.
- **Timing checks**: `gettimeofday` or `clock` calls measuring execution time. Not defeated by static analysis, but rare in CTF tasks.

When dynamic analysis is necessary (e.g., the secret is computed at runtime, not stored):
- Use `LD_PRELOAD` to hook `ptrace` and return 0: `echo 'int ptrace() { return 0; }' > fake.c && gcc -shared -o fake.so fake.c && LD_PRELOAD=./fake.so ./binary`
- Use GDB with a ptrace breakpoint and set the return value to 0 after the call