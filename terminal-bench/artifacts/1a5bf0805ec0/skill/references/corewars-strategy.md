# CoreWars Strategy Guide

## Table of Contents
1. [Core Concepts](#core-concepts)
2. [Warrior Archetypes](#warrior-archetypes)
3. [Winning Against Specific Opponent Types](#opponent-specific-strategies)
4. [Redcode-94 Quick Reference](#redcode-94-reference)

## Core Concepts

CoreWars warriors compete in a circular memory array (core). Each warrior executes
instructions sequentially. A warrior loses when its instruction pointer reaches a
DAT (data) instruction. The goal is to kill the opponent by bombing DAT at their
code while keeping your own code alive.

Key parameters that determine warrior strength:
- **Step size**: Distance between bomb targets. Must be coprime with core size to
  cover all positions. Common steps: 2667, 3000 (coprime with 8000).
- **Bombing speed**: Instructions per bomb dropped. Fewer = faster bombing.
- **Process control**: SPL creates additional execution threads. More processes
  mean more bombs per round but each thread runs slower.
- **Resilience**: Imp rings and self-replicating code survive longer under attack.

## Warrior Archetypes

### Core Clear (most versatile)
Wipes memory systematically with SPL or DAT bombs. The strongest all-around
archetype because it works against bombers (covers memory faster), replicators
(wipes copies as they appear), vampires (clears fangs before they redirect), and
moving warriors (overwrites them as they copy). Sample code:

```
;redcode-94
;name ClearBomb
;assert CORESIZE == 8000

dec     equ     3000                ; step for clear (coprime with 8000)
gate    equ     clear-4             ; gate location

        org     clear-1

bptr    dat     bomb,       9       ; pointer
bomb    spl     #dec,       11      ; SPL bomb
clear   mov     *bptr,      >gate   ; clear forward
        mov     *bptr,      >gate   ; double clear
        djn.f   clear,      }bomb   ; loop

        end
```

Key: SPL bombs stall enemy processes while clearing continues. The double MOV
per iteration clears faster. Step must be coprime with CORESIZE (8000) for full
coverage — 2667 and 3000 both work.

### Bomber (Stone)
Drops DAT bombs at regular intervals without scanning. Fast and simple.
Strong against other bombers and vampires, weak against replicators.

```
bomb    mov kill, @bptr
        add #step, bptr
        jmp bomb
bptr    dat #0, #offset
kill    dat #0, #0
```

### Scanner
Scans memory for non-zero locations, then bombs them. Effective against opponents
that occupy few memory cells. Weak against fast replicators.

### Paper (Replicator)
Copies itself to new locations. Strong against bombers (survives bombing) but
weak against core clears (wiped faster than replication).

### Imp / Imp Ring
`MOV 0, 1` moves itself forward. Nearly unkillable as an imp ring but cannot
kill opponents — only ties. Used as a backup strategy, not a primary.

## Opponent-Specific Strategies

### vs Stone (step-4 bomber)
Core clear wins by covering memory faster than the bomber. A bomber with a
different step coprime to 4 can also win the bombing race. Paper/replicator
survives bombing but ties rather than winning.

### vs Paper (replicator)
Core clear outpaces replication by wiping faster than copies spread. Scanner
that detects and kills copies also works but is slower.

### vs Vampire (JMP fang scatterer)
Core clear works by clearing fangs before they redirect execution. Fast bomber
also kills vampire before fangs spread. Imp ring is partially immune to
redirection but only ties.

### vs Snake (self-copying moving warrior)
Core clear with dense sweep catches the moving target. SPL bombs stall the
snake while clearing continues. Step coprime with snake's movement stride
ensures eventual contact.

### vs G2-Clear (systematic clear)
A faster or different-step core clear wins the clearing race. Starting position
and step coprime to the opponent's step determines which clear survives.

## Redcode-94 Quick Reference

Common instructions:
- `DAT #a, #b` — data (kills process that executes it)
- `MOV a, b` — copy source to destination
- `ADD a, b` — add source to destination
- `JMP a` — jump to address a
- `JMZ a, b` — jump to a if b is zero
- `JMN a, b` — jump to a if b is non-zero
- `DJN a, b` — decrement b, jump to a if b is non-zero
- `SPL a` — split execution (new process at a)
- `CMP/SEQ a, b` — skip next if a == b
- `SLT a, b` — skip next if a < b

Addressing modes:
- `#n` — immediate (value n)
- `$n` — direct (address offset n)
- `@n` — indirect (value at address offset n, used as further offset)
- `<n` — predecrement indirect
- `>n` — postincrement indirect

pMARS testing:
- Always use `-f` flag to fix warrior position (matches verifier behavior)
- Use `-b -r 100` for batch mode with 100 rounds
- Results format: `Results: W1 W2 T` (W1 = your wins, W2 = opponent wins, T = ties)
- Run at least 3 independent tests of 100 rounds before concluding a threshold is met
- Variance is ~5% between runs; a warrior scoring 76/100 could score 71-81 on the next run