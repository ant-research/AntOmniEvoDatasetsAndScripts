# Distributed Parallelism Patterns and Verification

## Table of Contents
1. Communication-Contract Patterns
2. Determining Input/Output Contracts from Task Descriptions
3. Verification Methodology
4. Common Pitfalls

## 1. Communication-Contract Patterns

### Tensor Parallelism — Column Parallel (split output dimension)
- **Weight**: Split master_weight[rank*partition:(rank+1)*partition, :] — each rank holds a column partition.
- **Bias**: Sharded same as output dimension — each rank holds bias[rank*partition:(rank+1)*partition].
- **Input**: Full input replicated on every rank.
- **Forward**: Compute partial output = F.linear(x, weight_shard, bias_shard). Then all_gather across ranks and concatenate along the last dimension to produce full output.
- **Output**: Full output (batch × out_features), identical across all ranks.
- **Gradient**: Each rank's weight.grad is the partial gradient for its partition — matches the corresponding slice of the single-process reference weight.grad.

### Tensor Parallelism — Row Parallel (split input dimension)
- **Weight**: Split master_weight[:, rank*partition:(rank+1)*partition] — each rank holds a row partition.
- **Bias**: Full (out_features) on every rank, added AFTER all_reduce, not before.
- **Input**: Pre-scattered — each rank receives only its slice of input features. The forward method must NOT re-slice the input.
- **Forward**: Compute partial output = F.linear(x_scattered, weight_shard) (no bias). Then all_reduce (sum) across ranks. Then add bias.
- **Output**: Full output (batch × out_features), identical across all ranks.
- **Gradient**: Each rank's weight.grad corresponds to the input-dimension slice of the reference weight.grad.

### Data Parallelism
- **Weight**: Full copy on every rank.
- **Input**: Different data per rank (data is split across the batch dimension).
- **Forward**: Normal forward pass. Gradients are averaged (all_reduce) during backward.
- **Output**: Different output per rank (different data in).

## 2. Determining Input/Output Contracts from Task Descriptions

Task descriptions use specific language that maps to communication contracts:

| Task phrasing | Meaning | Implementation |
|---|---|---|
| 'concatenated as if using all_gather' | Each rank produces partial output; forward must gather and concatenate | Implement all_gather + cat in forward(); return full output |
| 'summed as if using all_reduce' | Each rank produces partial output; forward must sum | Implement all_reduce in forward(); return full output |
| 'each rank gets its partition' | Weight/bias is split; each rank holds one shard | Split in __init__, store shard as Parameter |
| 'bias remains full' | Bias is NOT sharded | Store full-size bias; add after any reduction |
| 'bias sharded in the same way' | Bias is split like the output dimension | Store bias shard matching weight partition |

Key question: Does the test send full input or pre-scattered input?
- If the task says 'partial outputs should be summed' → input is pre-scattered (each rank processes a partition)
- If the task says 'output should be concatenated' → input is full (each rank sees the complete input)
- When uncertain, check the test: look for how the test creates and passes input to the module

## 3. Verification Methodology

Always verify distributed code against a single-process reference:

```python
import torch, torch.nn as nn, torch.distributed as dist, torch.multiprocessing as mp, os

def verify(rank, world_size, ParallelClass, in_features, out_features, bias):
    os.environ['MASTER_ADDR'] = 'localhost'
    os.environ['MASTER_PORT'] = '29500'
    dist.init_process_group('gloo', rank=rank, world_size=world_size)

    # Single-process reference
    ref = nn.Linear(in_features, out_features, bias=bias)
    torch.manual_seed(42)
    nn.init.uniform_(ref.weight, -0.1, 0.1)
    if bias: nn.init.zeros_(ref.bias)

    # Distributed version using same weights
    parallel = ParallelClass(in_features, out_features, bias, ref.weight.clone())

    # Test forward: each rank must produce the SAME full output as the reference
    x = torch.randn(2, in_features)
    ref_out = ref(x)
    par_out = parallel(x.clone())  # or x[:, rank*partition:(rank+1)*partition] for row parallel
    assert torch.allclose(par_out, ref_out, atol=1e-5), f'Output mismatch on rank {rank}'

    # Test backward: each rank's gradient must match the reference slice
    par_out.sum().backward()
    ref_out.sum().backward()
    # Compare grad slices as appropriate for the parallelism type

    dist.destroy_process_group()
```

Critical: the assertion `torch.allclose(par_out, ref_out)` checks that the distributed module produces the FULL output, not just a partition. If this fails, the communication primitive (all_gather or all_reduce) is missing or incorrect.

Never rely on shape-only assertions. Testing `output.shape == expected_shape` passes even when the implementation is wrong — the values must match the reference.

## 4. Common Pitfalls

1. **Missing communication primitives**: The most common error. When the task says 'as if using all_gather/all_reduce,' implement those operations in forward(). Without them, each rank only returns partial output.
2. **Re-slicing pre-scattered input**: In row parallelism, the test sends already-scattered input. The forward method must use the input directly, not slice it again with rank-based indexing.
3. **Adding bias before reduction**: In row parallelism, bias must be added AFTER all_reduce, not before. Adding bias before reduction causes each rank to add the full bias to its partial output, and the reduction sums N copies of the bias instead of one.
4. **Shape-only testing**: Custom tests that only check output shapes pass even when communication primitives are missing. Always compare output VALUES against a single-process reference.
5. **world_size=1 hides bugs**: All distributed bugs are invisible when world_size=1 because all_gather/all_reduce are no-ops and there is no slicing. Always test with world_size > 1.
6. **dist.all_gather and dist.all_reduce break autograd**: These collective operations produce tensors WITHOUT grad_fn — calling .backward() on the output raises RuntimeError. You MUST wrap them in custom torch.autograd.Function subclasses that implement both forward and backward:
   - **AllGatherWithGrad**: forward performs all_gather + cat; backward extracts this rank's partition (output[:, rank*partition:(rank+1)*partition]) — the gradient for each rank IS its own partition since all_gather just concatenates.
   - **AllReduceWithGrad**: forward performs all_reduce(sum); backward passes grad_output through unchanged — since forward output = sum(x_i) is identical across ranks, each rank already has the same upstream gradient, and ∂L/∂x_i = ∂L/∂output × 1 = ∂L/∂output. No all_reduce in backward.
   Never use bare dist.all_gather or dist.all_reduce on tensors that require gradients. Pattern:
   ```python
   class AllGatherFunc(torch.autograd.Function):
       @staticmethod
       def forward(ctx, x, world_size, rank):
           ctx.world_size = world_size
           ctx.rank = rank
           gathered = [torch.zeros_like(x) for _ in range(world_size)]
           dist.all_gather(gathered, x)
           return torch.cat(gathered, dim=-1)
       @staticmethod
       def backward(ctx, grad_output):
           partition_size = grad_output.shape[-1] // ctx.world_size
           return grad_output[..., ctx.rank*partition_size:(ctx.rank+1)*partition_size], None, None

   class AllReduceFunc(torch.autograd.Function):
       @staticmethod
       def forward(ctx, x):
           out = x.clone()
           dist.all_reduce(out, op=dist.ReduceOp.SUM)
           return out
       @staticmethod
       def backward(ctx, grad_output):
           # Each rank already has the same upstream gradient because
           # the forward output (sum) is identical across ranks.
           # Since output = sum(x_i), ∂L/∂x_i = ∂L/∂output × 1 = ∂L/∂output.
           # The gradient just passes through — no all_reduce needed.
           return grad_output
   ```