#!/usr/bin/env python3
"""GPT-2 124M verification script.
Usage: python3 gpt2-verify.py <checkpoint_path> <bpe_path> <input_text> [num_tokens]
Prints the next N tokens (default 20) using argmax sampling.
Uses numpy with memory-mapped loading to avoid OOM.
"""
import sys, struct, math
try:
    import numpy as np
except ImportError:
    print("ERROR: numpy required. Install with: pip install numpy", file=sys.stderr)
    sys.exit(1)

def bytes_to_unicode():
    bs = list(range(ord('!'), ord('~')+1)) + list(range(ord('\xa1'), ord('\xac')+1)) + list(range(ord('\xae'), ord('\xff')+1))
    cs = bs[:]
    n = 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256+n)
            n += 1
    return dict(zip(bs, [chr(c) for c in cs]))

def load_bpe(path):
    b2u = bytes_to_unicode()
    u2b = {v: k for k, v in b2u.items()}
    merges = []
    with open(path) as f:
        f.readline()  # skip #version
        for line in f:
            parts = line.strip().split()
            if len(parts) == 2:
                merges.append(tuple(parts))
    bpe_ranks = {pair: i for i, pair in enumerate(merges)}
    vocab = {b2u[b]: b for b in range(256)}
    for i, (a, b) in enumerate(merges):
        vocab[a+b] = 256+i
    vocab['<|endoftext|>'] = len(vocab)
    id2token = {v: k for k, v in vocab.items()}
    return bpe_ranks, vocab, id2token, u2b

def bpe_encode(text, bpe_ranks, vocab):
    b2u = bytes_to_unicode()
    tokens = [b2u[b] for b in text.encode('utf-8')]
    word = tuple(tokens)
    while True:
        pairs = {(word[i], word[i+1]) for i in range(len(word)-1)}
        if not pairs:
            break
        bigram = min(pairs, key=lambda p: bpe_ranks.get(p, float('inf')))
        if bigram not in bpe_ranks:
            break
        first, second = bigram
        new_word = []
        i = 0
        while i < len(word):
            try:
                j = word.index(first, i)
            except ValueError:
                new_word.extend(word[i:])
                break
            new_word.extend(word[i:j])
            i = j
            if i < len(word)-1 and word[i] == first and word[i+1] == second:
                new_word.append(first+second)
                i += 2
            else:
                new_word.append(word[i])
                i += 1
        word = tuple(new_word)
    return [vocab[t] for t in word]

def decode_token(token_id, id2token, u2b):
    s = id2token.get(token_id, '')
    return ''.join(chr(u2b.get(c, ord(c))) if c in u2b else c for c in s)

def layer_norm(x, g, b, eps=1e-5):
    mean = np.mean(x, axis=-1, keepdims=True)
    var = np.var(x, axis=-1, keepdims=True)
    return g * (x - mean) / np.sqrt(var + eps) + b

def gelu(x):
    return 0.5 * x * (1 + np.tanh(np.sqrt(2/np.pi) * (x + 0.044715 * x**3)))

def softmax(x, axis=-1):
    x = x - np.max(x, axis=axis, keepdims=True)
    e = np.exp(x)
    return e / np.sum(e, axis=axis, keepdims=True)

def gpt2_forward(token_ids, weights, n_layers=12, d_model=768, n_heads=12, d_head=64):
    wte = weights['wte']
    wpe = weights['wpe']
    x = wte[token_ids] + wpe[:len(token_ids)]
    kv_cache = []
    for layer_idx in range(n_layers):
        prefix = f'l{layer_idx}_'
        ln1g, ln1b = weights[prefix+'ln1g'], weights[prefix+'ln1b']
        qkvw, qkvb = weights[prefix+'qkvw'], weights[prefix+'qkvb']
        cprojw, cprojb = weights[prefix+'cprojw'], weights[prefix+'cprojb']
        ln2g, ln2b = weights[prefix+'ln2g'], weights[prefix+'ln2b']
        fcw, fcb = weights[prefix+'fcw'], weights[prefix+'fcb']
        fcprojw, fcprojb = weights[prefix+'fcprojw'], weights[prefix+'fcprojb']
        # Pre-norm attention
        h = layer_norm(x, ln1g, ln1b)
        # QKV: weight is [768, 2304], stored as [out_features, in_features] in TF
        # So multiply: h @ qkvw.T + qkvb
        qkv = h @ qkvw.T + qkvb  # [seq, 2304]
        q, k, v = np.split(qkv, 3, axis=-1)  # each [seq, 768]
        q = q.reshape(len(token_ids), n_heads, d_head).transpose(1, 0, 2)
        k = k.reshape(len(token_ids), n_heads, d_head).transpose(1, 0, 2)
        v = v.reshape(len(token_ids), n_heads, d_head).transpose(1, 0, 2)
        # Store for KV cache
        if layer_idx < len(kv_cache):
            prev_k, prev_v = kv_cache[layer_idx]
            k = np.concatenate([prev_k, k], axis=1)
            v = np.concatenate([prev_v, v], axis=1)
        kv_cache.append((k.copy(), v.copy()))
        attn = q @ k.transpose(0, 2, 1) / math.sqrt(d_head)
        # Causal mask
        seq_q, seq_k = q.shape[1], k.shape[1]
        mask = np.triu(np.ones((seq_q, seq_k)), k=seq_k-seq_q+1) * (-1e10)
        attn = attn + mask
        attn = softmax(attn, axis=-1)
        out = (attn @ v).transpose(1, 0, 2).reshape(len(token_ids), d_model)
        out = out @ cprojw.T + cprojb
        x = x + out  # Residual
        # Pre-norm MLP
        h = layer_norm(x, ln2g, ln2b)
        fc = gelu(h @ fcw.T + fcb)
        fc = fc @ fcprojw.T + fcprojb
        x = x + fc  # Residual
    # Final layer norm
    x = layer_norm(x, weights['lnfg'], weights['lnfb'])
    # Project to vocab
    logits = x @ wte.T  # Tie weights
    return logits[-1]  # Last position logits

def load_weights(ckpt_path):
    """Load GPT-2 124M weights from raw float32 checkpoint using numpy memmap."""
    data = np.memmap(ckpt_path, dtype=np.float32, mode='r')
    w = {}
    idx = 0
    def load(shape):
        nonlocal idx
        size = int(np.prod(shape))
        arr = np.array(data[idx:idx+size]).reshape(shape)
        idx += size
        return arr
    w['wte'] = load((50257, 768))
    w['wpe'] = load((1024, 768))
    for i in range(12):
        p = f'l{i}_'
        w[p+'ln1g'] = load((768,))
        w[p+'ln1b'] = load((768,))
        w[p+'qkvw'] = load((768, 2304))
        w[p+'qkvb'] = load((2304,))
        w[p+'cprojw'] = load((768, 768))
        w[p+'cprojb'] = load((768,))
        w[p+'ln2g'] = load((768,))
        w[p+'ln2b'] = load((768,))
        w[p+'fcw'] = load((768, 3072))
        w[p+'fcb'] = load((3072,))
        w[p+'fcprojw'] = load((3072, 768))
        w[p+'fcprojb'] = load((768,))
    w['lnfg'] = load((768,))
    w['lnfb'] = load((768,))
    assert idx == 124439808, f"Weight count mismatch: {idx} != 124439808"
    return w

def main():
    if len(sys.argv) < 4:
        print("Usage: python3 gpt2-verify.py <ckpt> <bpe> <input_text> [num_tokens]", file=sys.stderr)
        sys.exit(1)
    ckpt_path, bpe_path, input_text = sys.argv[1], sys.argv[2], sys.argv[3]
    num_tokens = int(sys.argv[4]) if len(sys.argv) > 4 else 20
    bpe_ranks, vocab, id2token, u2b = load_bpe(bpe_path)
    print(f"Loading checkpoint from {ckpt_path}...", file=sys.stderr)
    weights = load_weights(ckpt_path)
    print(f"Checkpoint loaded. Tokenizing input...", file=sys.stderr)
    token_ids = bpe_encode(input_text, bpe_ranks, vocab)
    print(f"Input tokens: {token_ids}", file=sys.stderr)
    print(f"Input token strings: {[id2token[t] for t in token_ids]}", file=sys.stderr)
    output_text = input_text
    for step in range(num_tokens):
        logits = gpt2_forward(token_ids, weights)
        next_id = int(np.argmax(logits))
        output_text += decode_token(next_id, id2token, u2b)
        token_ids.append(next_id)
        print(f"Step {step}: token {next_id} = '{id2token[next_id]}'", file=sys.stderr)
    print(output_text)

if __name__ == '__main__':
    main()