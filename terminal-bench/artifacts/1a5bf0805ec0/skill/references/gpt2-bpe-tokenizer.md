# GPT-2 Byte-Level BPE Tokenizer Reference

## Table of Contents
1. [Overview](#overview)
2. [Byte-to-Unicode Mapping](#byte-to-unicode-mapping)
3. [Building the Vocabulary](#building-the-vocabulary)
4. [BPE Merge Algorithm](#bpe-merge-algorithm)
5. [Encoding Input Text](#encoding-input-text)
6. [Decoding Token IDs to Text](#decoding-token-ids-to-text)
7. [TF Checkpoint Format](#tf-checkpoint-format)
8. [Minimal C Implementation Strategy](#minimal-c-implementation-strategy)

## Overview

GPT-2 uses a byte-level BPE tokenizer. Unlike standard BPE that operates on characters,
GPT-2 operates on bytes mapped to unicode code points to avoid issues with control
characters. The vocab.bpe file contains merge rules, and encoder.json (or a
hardcoded mapping) maps token strings to IDs.

**Key gotcha**: Never use raw ASCII byte values as token IDs. GPT-2's base vocabulary
is 256 tokens, but these are NOT bytes 0-255 mapped directly. They are bytes 0-255
mapped through a specific byte-to-unicode table, then BPE merges build additional
tokens from those unicode strings.

## Byte-to-Unicode Mapping

GPT-2 maps the 256 byte values to unicode code points as follows:

```python
def bytes_to_unicode():
    # These byte ranges map to themselves (same unicode code point)
    bs = list(range(ord('!'), ord('~')+1)) + list(range(ord('¡'), ord('¬')+1)) + list(range(ord('®'), ord('ÿ')+1))
    cs = bs[:]
    n = 0
    # All other bytes (0-32, 127-160, 173) map to code points starting at 256
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    return dict(zip(bs, [chr(c) for c in cs]))
```

This gives a mapping where:
- Printable ASCII (33-126) maps to themselves
- Latin supplement (161-172, 174-255) maps to themselves
- Control chars and whitespace map to code points 256-311
- Specifically: space (byte 32) → Ġ (U+0120, code point 288)
- Byte 10 (newline) → Ċ (U+010A, code point 266)

**Critical**: In C, you need to encode these unicode code points as UTF-8 when
building the token strings. For example, Ġ (U+0120) is encoded as 0xC4 0xA0 in
UTF-8 (2 bytes). A char array must accommodate multi-byte UTF-8 sequences.

## Building the Vocabulary

1. Start with 256 base tokens: the byte-to-unicode mapping values (as strings)
2. Read vocab.bpe merge rules (skip the #version header line)
3. Each merge rule is like "Ġ t" meaning merge the tokens "Ġ" and "t" into "Ġt"
4. The merged token string is the concatenation of the two component strings
5. Token IDs are assigned in order: base 256 tokens get IDs 0-255, then each
   merge creates the next ID (256, 257, ..., 50255)
6. Total vocabulary: 50257 tokens (256 base + 50000 merges + 1 special  )

**Token ID 50256 is  ** — the special end-of-text token, not a merge
and not Ġ. The Ġ character (U+0120) is the byte-to-unicode mapping for the
space byte (32), one of the 256 base tokens.

## BPE Merge Algorithm

To tokenize input text:
1. Convert input string to bytes
2. Map each byte through the byte-to-unicode table to get a list of unicode strings
3. Apply BPE merges iteratively:
   - Find the pair of adjacent tokens with the LOWEST merge rank
   - If no pair has a merge rule, stop
   - Replace that pair with the merged token
   - Repeat
4. The resulting token strings map to token IDs via the vocabulary

```python
def bpe(token, bpe_ranks):
    word = tuple(token)
    while True:
        pairs = {(word[i], word[i+1]) for i in range(len(word)-1)}
        bigram = min(pairs, key=lambda p: bpe_ranks.get(p, float('inf')))
        if bigram not in bpe_ranks:
            break
        first, second = bigram
        new_word = []
        i = 0
        while i < len(word):
            try:
                j = word.index(first, i)
                new_word.extend(word[i:j])
                i = j
            except ValueError:
                new_word.extend(word[i:])
                break
            if i < len(word)-1 and word[i] == first and word[i+1] == second:
                new_word.append(first + second)
                i += 2
            else:
                new_word.append(word[i])
                i += 1
        word = tuple(new_word)
    return word
```

## Encoding Input Text

For the input 'THIS SOFTWARE IS PROVIDED "AS IS", WITHOUT':
1. Convert to bytes
2. Map bytes: T→T, H→H, I→I, S→S, space→Ġ, S→S, O→O, F→F, T→T, W→W, A→A, R→R, E→E, etc.
3. Unicode string: 'THISĠSOFTWAREĠISĠPROVIDEDĠ"ASĠIS",ĠWITHOUT'
4. Apply BPE merges → list of token strings
5. Look up each token string in vocabulary → list of integer token IDs

The first token for 'THIS' context would be something like token 'THIS' (not 'T','H','I','S'
separately) because BPE merges common subwords.

## Decoding Token IDs to Text

To decode output token IDs back to text:
1. Look up each token ID in the reverse vocabulary → token string
2. Concatenate all token strings
3. Map each unicode character back through the reverse of the byte-to-unicode table
4. The result is the decoded text

**This step is essential**: printing integer token IDs instead of decoded text
is the most common failure mode. The verifier checks for readable text like
'WARRANTY OF ANY KIND, EXPRESS OR IMPLIED', not numeric IDs.

## TF Checkpoint Format

The gpt2-124M.ckpt file is raw float32 binary data with NO header or index.
File size: 497,759,232 bytes = 124,439,808 float32 values.

Weight layout (sequential in file):
- wte (token embeddings): [50257, 768] = 38,597,376 floats
- wpe (position embeddings): [1024, 768] = 786,432 floats
- For each of 12 layers:
  - ln1g (layer norm 1 gamma): [768]
  - ln1b (layer norm 1 beta): [768]
  - qkv_w (Q/K/V projection): [768, 2304] = 1,769,472 floats
  - qkv_b: [2304]
  - c_proj_w (attention output): [768, 768] = 589,824 floats
  - c_proj_b: [768]
  - ln2g (layer norm 2 gamma): [768]
  - ln2b (layer norm 2 beta): [768]
  - fc_w (MLP up): [768, 3072] = 2,359,296 floats
  - fc_b: [3072]
  - fc_proj_w (MLP down): [3072, 768] = 2,359,296 floats
  - fc_proj_b: [768]
- ln_fg (final layer norm gamma): [768]
- ln_fb (final layer norm beta): [768]

**Critical — weight matrix orientation**: TF checkpoints store all weight
matrices as [out_features, in_features] (row-major). The correct matrix
multiplication for each layer is: output = input @ W.T + bias (NOT input @ W).
For qkv_w stored as [768, 2304]: output = input @ qkv_w.T + qkv_b.
For c_proj_w stored as [768, 768]: output = input @ c_proj_w.T + c_proj_b.
For fc_w stored as [768, 3072]: output = input @ fc_w.T + fc_b.
For fc_proj_w stored as [3072, 768]: output = input @ fc_proj_w.T + fc_proj_b.
In C, this means for matrix access: W[j*in_dim + i] where j is the output
neuron index and i is the input neuron index. The inner loop iterates over
i (input dimension), accumulating dot product for output j.
To verify your transposition is correct: compute logits for input token 0
(position 0, token embedding wte[0] + wpe[0]), pass through all 12 layers,
and check that the argmax token is a common word like 'The' — not a rare
token like 'Ranked' or 'Maurit' that indicates wrong matrix orientation.

## Minimal C Implementation Strategy

To fit in <5000 bytes, use these compression techniques:
1. Combine the byte-to-unicode mapping into a lookup table (188 entries that
   map to code points 256+ can be computed, not stored)
2. Store BPE merges as pairs of (rank, left_len) so merged string = concat
3. Use global/static arrays instead of dynamic allocation
4. Simplify attention: compute Q*K^T for all heads in a loop
5. Use GELU approximation: 0.5*x*(1+tanhf(sqrtf(2/M_PI)*(x+0.044715*x*x*x)))
6. Output decoded text, not token IDs

Before writing C, validate with the Python verification script:
```
cp /skill/scripts/gpt2-verify.py /tmp/gpt2-verify.py
timeout 120 python3 /tmp/gpt2-verify.py /app/gpt2-124M.ckpt /app/vocab.bpe 'THIS SOFTWARE IS PROVIDED "AS IS", WITHOUT' > /tmp/gpt2_ref_output.txt 2>&1
cat /tmp/gpt2_ref_output.txt
```
The last line of stdout is the full generated text (input + 20 continuation tokens).
Stderr shows per-step token IDs and strings for debugging.
If you must write your own Python reference, use `np.memmap` (NOT struct.unpack
— loading 124M floats into a tuple OOMs): `data = np.memmap(path, dtype=np.float32, mode='r')`.
After getting the reference output, compare your C program's output character-by-character
against it. If the first generated token differs, the bug is in tokenization or
weight loading; if later tokens differ, the bug is in the forward pass or cache.