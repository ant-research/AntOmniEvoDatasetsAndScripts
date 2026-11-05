# Distribution Optimization with Equality Constraints

## When to use this reference

When a task requires finding a probability distribution that satisfies exact equality constraints (KL divergence equals a target value, moment constraints, probability sum constraints). Do NOT use scipy.optimize.minimize with sum-of-squared-errors for equality constraints — it finds minima that may not be exact zeros. Use scipy.optimize.root or scipy.optimize.fsolve instead.

## KL divergence constraints

For a distribution P over n elements with uniform reference U = 1/n:

- Forward KL(P||U) = log(n) + sum(P * log(P)) = log(n) - H(P)
- Backward KL(U||P) = -log(n) - (1/n) * sum(log(P))

Key insight: these two constraints take specific forms for structured distributions. A K-level distribution (K distinct probability values) has at most K parameters, so at least K constraints can be satisfied exactly.

## Three-level distribution strategy

When both KL(P||U) and KL(U||P) must equal the same target, a three-level distribution works well:

1. Divide the n elements into 3 groups: group 1 (k1 elements with probability p1), group 2 (k2 elements with probability p2), group 3 (n-k1-k2 elements with probability p3).
2. Parameters: p1, p2, k1, k2. Constraints: sum=1, forward KL=target, backward KL=target. This is 4 parameters with 3 constraints, leaving 1 free parameter to sweep.
3. For each candidate value of the free parameter, solve the 3 remaining unknowns analytically or with root-finding.

## scipy.optimize.root vs minimize for equality constraints

```python
from scipy.optimize import root

# CORRECT: use root() for equality constraints
result = root(equations, x0=initial_guess)
if result.success:
    # result.x satisfies equations exactly (residual near zero)
    pass

# WRONG: minimize(sum_of_squares) can find a minimum that is NOT zero
# The optimizer stops when gradient is zero, not when the constraint is satisfied.
```

## Parameter count vs constraint count

- More parameters than constraints: use root() with extra free parameters fixed at reasonable values, or sweep the free parameters.
- Equal parameters and constraints: use root() directly.
- Fewer parameters than constraints: the system is over-determined; add more levels/groups to the parameterization.

## Numerical stability

- Always use `np.clip(p, 1e-300, 1.0)` before taking `log(p)` to avoid -inf.
- For distributions with very small probabilities, use log-space parameterization: let the optimizer vary `log_p` values, then compute `p = exp(log_p) / sum(exp(log_p))`.
- Normalize after solving: `P = P / P.sum()` to ensure exact sum-to-1.

## Verification pattern

After finding the distribution, verify against the EXACT formulas the test uses — the test may normalize the distribution again before computing KL. Always compute KL using the test's exact code to avoid tolerance mismatches.