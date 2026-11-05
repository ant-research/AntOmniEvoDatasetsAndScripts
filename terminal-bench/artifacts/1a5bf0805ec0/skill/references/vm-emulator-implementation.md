# VM/Emulator Implementation from Scratch

## Table of Contents
1. [ELF memory model and pre-initialization](#elf-memory-model-and-pre-initialization)
2. [Typed-array memory for JavaScript VMs](#typed-array-memory-for-javascript-vms)
3. [MIPS branch delay slots — CRITICAL](#mips-branch-delay-slots--critical)
4. [Initialization-loop fast-forwarding](#initialization-loop-fast-forwarding)
5. [Syscall hook optimization](#syscall-hook-optimization)
6. [Performance debugging](#performance-debugging)

## MIPS branch delay slots — CRITICAL

MIPS has a **branch delay slot**: the instruction immediately after a branch
or jump instruction is ALWAYS executed before the branch takes effect,
regardless of whether the branch is taken or not. This is NOT optional —
it is a fundamental architectural feature of the MIPS ISA.

**Why this matters**: If your VM does not execute the delay slot instruction,
every taken branch, JR, JAL, and JALR will silently skip one instruction.
The program's control flow will diverge immediately from correct execution,
producing completely wrong results with no obvious error message. Programs
will never reach their main logic, syscalls will never fire, and no output
will be produced.

**Affected instructions**: BEQ, BNE, BLEZ, BGTZ, BLTZ, BGEZ, BLTZAL,
BGEZAL, J, JAL, JR, JALR — any instruction that changes the PC.

**Implementation pattern** (JavaScript):
```javascript
step() {
  const instr = this.r32(this.pc);
  // ... decode instruction, determine bt (branch taken?) and np (target PC)
  if (bt) {
    // Save the branch target, execute the delay slot first
    const savedTarget = np;
    this.pc += 4;       // point to delay slot instruction
    this.step();        // execute delay slot (recursive call)
    if (this.running) this.pc = savedTarget;  // now branch
  } else {
    this.pc = np;       // normal next instruction (pc + 4)
  }
}
```

**IMPORTANT**: A branch instruction inside a delay slot is architecturally
undefined on real MIPS hardware. Guard against this by treating branch/jump
instructions in delay slots as NOPs, or by setting a flag (`_inDelaySlot`)
that prevents the inner step() call from taking any branches.

**Node.js Buffer pitfall**: When loading ELF segments with `fs.readFileSync()`,
the returned value is a Buffer, not an ArrayBuffer. `new Uint8Array(buf, offset, len)`
can throw `RangeError: offset is out of bounds` because the Buffer's underlying
ArrayBuffer may be larger than the Buffer's visible region. Always use:
`new Uint8Array(buf.buffer, buf.byteOffset + offset, len)` to correctly
reference the Buffer's data within its shared ArrayBuffer.

## ELF memory model and pre-initialization

When building a VM/emulator that loads an ELF binary, you MUST pre-initialize
memory at load time, not rely on the guest program to initialize it.

**BSS pre-zeroing**: For each PT_LOAD segment, allocate memory for the full
`p_memsz` (not just `p_filesz`). The bytes from `p_filesz` to `p_memsz` are
the BSS region — they must be zero-filled at load time. In JavaScript:
```javascript
const mem = new Uint8Array(p_memsz);  // automatically zero-filled
mem.set(new Uint8Array(elfBytes, p_offset, p_filesz));  // load file-backed data
```
In Python: `bytearray(p_memsz)` is zero-filled by default.

**Why this matters**: Guest programs (especially C programs with custom
stdlibs) contain CRT startup code that explicitly zeros BSS via loops like:
```
loop: sw $zero, offset($t0)
      addiu $t0, $t0, 4
      bne $t0, $t1, loop
```
An interpreted VM executes each instruction individually. For a 1GB BSS
segment, this loop takes ~268 million iterations (1GB / 4 bytes per word)
× ~4 instructions per iteration = ~1 billion instructions. At 1-10 million
interpreted instructions/second, this takes 100-1000 seconds — the program
never reaches useful work within any reasonable timeout.

**The fix**: Because memory is pre-zeroed (`Uint8Array`/`bytearray`), BSS-
zeroing SW instructions write 0 to already-0 memory — they are no-ops that
can be safely fast-forwarded (see Initialization-loop fast-forwarding).

**Stack pre-allocation**: Allocate the stack region (typically 0x7FFF0000-
0x80000000 on MIPS) as a separate zero-filled buffer. Set the stack pointer
register ($sp) to the top of this region at VM startup.

## Typed-array memory for JavaScript VMs

**Never use plain objects {} or sparse arrays for VM memory**. Access is
10-100x slower than typed arrays. Use:
```javascript
// Flat memory model: single ArrayBuffer with typed views
const MEM_SIZE = 512 * 1024 * 1024;  // 512MB
const memBuffer = new ArrayBuffer(MEM_SIZE);
const memU8 = new Uint8Array(memBuffer);
const memU32 = new Uint32Array(memBuffer);  // word-aligned view
const memView = new DataView(memBuffer);    // unaligned access
```

**Read/write methods**:
- Byte: `memU8[addr]` / `memU8[addr] = val`
- Word (aligned): `memU32[addr >>> 2]` / `memU32[addr >>> 2] = val`
- Word (unaligned): `memView.getUint32(addr, true)` / `memView.setUint32(addr, val, true)`
  (`true` = little-endian; set based on ELF header byte 5)

**Performance**: `Uint32Array` indexed access is 2-5x faster than `DataView`
for aligned reads. Use `Uint32Array` for the hot instruction-decode path and
`DataView` only for unaligned accesses.

## Initialization-loop fast-forwarding

After pre-zeroing memory, detect BSS-zeroing loops and fast-forward through
them by computing the trip count and jumping to the branch target.

**Pattern**: A tight loop that writes zeros (or the same value) to sequential
addresses and branches back:
```
SW $zero, offset($reg)       # store zero to memory
ADDIU $reg, $reg, stride     # increment address by stride
BNE $reg, $end_reg, loop     # branch back if not done
```

**Fast-forward algorithm**:
1. Detect: current instruction writes a value to memory, next instruction
   increments an address register, following instruction is a conditional
   branch back to the store.
2. Validate: the value being stored equals what is already in memory at
   the target address (for pre-zeroed BSS, this is always 0 from $zero
   register, so the write is a no-op).
3. Compute trip count: `(end_reg - reg) / stride`.
4. Advance: set `reg = end_reg`, jump PC past the loop.

**Critical safety check**: Only fast-forward when the stored value matches
existing memory content. If the store writes a non-zero value, the loop is
initializing data (not BSS) and must be executed normally — skipping it
corrupts program state. A common failure mode is detecting the SW+ADDIU+BNE
pattern and skipping ALL such loops, including ones that write non-zero
data to heap objects during malloc initialization.

**Alternative: hook memset/memcpy**: If the guest program has identifiable
memset or memcpy functions (check `nm binary | grep memset`), intercept calls
to these functions at the VM level and execute them with JavaScript's
`memU8.fill()` / `memU8.copyWithin()` instead of interpreting each store
instruction. This handles the BSS-zeroing case and also provides 1000x+
speedup for any bulk memory operation.

## Syscall hook optimization

Many VM tasks use custom C stdlibs that implement file I/O, memory allocation,
and process control through a small set of system call functions. Instead of
interpreting each instruction in these functions, hook the syscall entry point.

**Pattern**: The binary has a `syscall6` or similar function at a known
address (find with `nm binary | grep syscall`). The function loads a syscall
number into a register and executes a MIPS `SYSCALL` instruction.

**Hook approach**:
1. At VM startup, identify syscall entry addresses from the symbol table.
2. When PC reaches a syscall entry point, extract arguments from registers
   directly and handle the syscall in JavaScript (read file, write file,
   alloc memory, etc.), then set the return value in $v0 and return.
3. This avoids interpreting hundreds of instructions per syscall in the
guest's custom stdlib wrapper.

**Common MIPS Linux syscalls** (O32 ABI):
- 4003 = read, 4004 = write, 4005 = open, 4006 = close
- 4019 = lseek, 4045 = brk, 4085 = exit
- 4146 = writev, 4194 = mmap
Note: MIPS O32 uses 4000+Linux_number, NOT x86_64 numbers (0=read, etc.).
If the binary uses x86_64 syscall numbers in its custom stdlib, check the
source code's `#define SYS_*` constants and implement those numbers.

## Critical anti-pattern: custom frame-saving logic

**NEVER implement a custom saveFrame() or writeBMP() function in the VM**
that outputs frame data independently of the guest program's own code. The
guest program (e.g., DOOM's DG_DrawFrame) saves frames via its own
open/write/close syscalls to a specific path (e.g., /tmp/frame.bmp). The
VM must make those file I/O syscalls work correctly so the guest code saves
frames at the expected path with the correct format and resolution.

Common failure mode: the VM implements a custom saveFrame that writes
to /app/frame_0000.bmp at the VM's internal framebuffer resolution
(640x400), while the verifier expects /tmp/frame.bmp at the guest
program's actual screen resolution (320x200 = DOOMGENERIC_RESX x
DOOMGENERIC_RESY). The custom saveFrame bypasses the guest's DG_DrawFrame
and produces output at the wrong path AND wrong resolution.

The correct approach: implement accurate open/write/close syscall handling
that accumulates write data in a buffer and flushes to disk on close().
The guest program's own DG_DrawFrame will call open("/tmp/frame.bmp"),
write(bmp_header_and_pixel_data), close() -- the VM just needs to make
those syscalls work. No custom frame-saving code is needed.

**Verification before task_complete**: Run `ls -la /tmp/frame.bmp` and
`file /tmp/frame.bmp` to confirm the frame exists at the exact path the
verifier checks, with the expected resolution and format. If the file
does not exist at that path, the VM's file I/O syscalls are broken --
fix the syscalls, do NOT add a custom saveFrame workaround.

## Performance debugging

**Measure instructions per second**: Add a counter and print IPS every
million instructions:
```javascript
if (insnCount % 1000000 === 0) {
    const elapsed = (Date.now() - startTime) / 1000;
    console.error(`IPS: ${(insnCount/elapsed/1e6).toFixed(1)}M`);
}
```

**Hotspot identification**: When IPS is unexpectedly low, log the PC and
instruction opcode for every instruction over a 100K-instruction window.
The most frequent PC+opcode pair is the hotspot. Common hotspots:
- BSS zeroing (SW+ADDIU+BNE loop) → pre-zero memory + fast-forward
- String compare (LBU+BNE loop) → hook strcmp
- Memory copy (LW+SW+ADDIU+BNE loop) → hook memcpy/memset

**Target performance**: For real-time applications (games, video), the VM
must execute 50-100+ million instructions per second to boot within 30
seconds. A naive interpreter with typed arrays achieves 1-10 MIPS; adding
fast-forward for init loops brings it to 50+ MIPS during startup.
If the VM cannot reach the required IPS, consider JITting hot loops or
translating the binary to the host architecture.

**Timeout discipline**: Always run the VM with `timeout` and output
generation checkpoints: `timeout 300 node vm.js > /tmp/vm_output.txt 2>&1`.
After each run, check: (1) was a frame produced? (2) what was the final
instruction count and IPS? (3) what was the last logged message? Do NOT
run the VM interactively — it will block the terminal for minutes.