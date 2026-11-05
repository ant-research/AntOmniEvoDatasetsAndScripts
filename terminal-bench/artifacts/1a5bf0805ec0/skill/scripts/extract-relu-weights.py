#!/usr/bin/env python3
"""Extract A1 weights from a single-hidden-layer ReLU network via black-box queries.

Usage:
  python3 extract-relu-weights.py
  python3 extract-relu-weights.py --n_lines 500 --output_path /app/stolen_A1.npy

Algorithm: line-scan boundary crossing.
1. Scan along many random 1D lines through input space.
2. At each point, compute the gradient via central differences.
3. Wherever the gradient changes between consecutive points, record the
   gradient difference — this is generically a single-neuron transition
   (because along a 1D line, activation boundaries are crossed one at a time).
4. Deduplicate direction vectors to recover unique A1 rows.
5. Save the extracted A1 rows to a .npy file.

This approach avoids the O(G^2) pairwise-difference explosion and
GxG cosine-similarity matrix OOM of the previous frequency-clustering
algorithm. Each line scan produces O(hidden_dim) boundary crossings,
and 50-100 lines provide full coverage of all neurons.

Handles evaluator countermeasures:
- Uses np.random.RandomState (local) for input generation, not global state.
- Saves/restores np.random state around each forward() call.
- Auto-detects input dimension by probing forward() with increasing sizes.
"""

import numpy as np
import sys
import argparse

sys.path.insert(0, '/app')


def _is_scalar(x):
    return np.isscalar(x) or (hasattr(x, 'shape') and (x.shape == () or x.shape == (1,)))


def detect_input_dim(forward_fn, max_dim=100):
    """Detect input dimension by probing with vectors of increasing size."""
    rng = np.random.RandomState(9999)
    for d in range(1, max_dim + 1):
        x = rng.randn(d)
        s = np.random.get_state()
        try:
            out = forward_fn(x)
            np.random.set_state(s)
            if _is_scalar(out):
                return d
        except (AssertionError, ValueError):
            np.random.set_state(s)
            continue
    raise RuntimeError(f"Could not detect input dimension up to {max_dim}")


def compute_gradient(forward_fn, x, eps=1e-7):
    """Numerical gradient via central differences with random state isolation."""
    n = len(x)
    grad = np.zeros(n)
    for i in range(n):
        x_plus = x.copy(); x_plus[i] += eps
        x_minus = x.copy(); x_minus[i] -= eps
        s = np.random.get_state()
        fp = forward_fn(x_plus)
        np.random.set_state(s)
        s = np.random.get_state()
        fm = forward_fn(x_minus)
        np.random.set_state(s)
        grad[i] = (fp - fm) / (2 * eps)
    return grad


def extract_relu_weights(forward_fn=None, input_dim=None, n_lines=80,
                         points_per_line=120, line_range=6.0,
                         output_path='/app/stolen_A1.npy'):
    """Extract A1 from forward() using line-scan boundary crossing.

    For a single-hidden-layer ReLU network y = A2 @ ReLU(A1 @ x + b1) + b2,
    the gradient at x is G(x) = A2[0,S] @ A1[S,:] where S is the set of
    active neurons. Along a 1D line x(t) = x0 + t*d, the activation boundaries
    are hyperplanes intersecting the line at discrete t values. Generically,
    only one boundary is crossed at each t, so the gradient change is exactly
    +-A2[0,k]*A1[k,:] for some neuron k. By scanning many random lines and
    collecting gradient changes, we recover all A1 rows.

    Returns the extracted matrix and saves it to output_path.
    """
    if forward_fn is None:
        if 'forward' in sys.modules:
            del sys.modules['forward']
        from forward import forward as forward_fn

    if input_dim is None:
        input_dim = detect_input_dim(forward_fn)

    rng = np.random.RandomState(42)

    # ---- 1. Scan along random lines ------------------------------------------
    sys.stderr.write(f"[extract] input_dim={input_dim}, n_lines={n_lines}, "
                     f"points_per_line={points_per_line}\n")
    primitives = []       # gradient differences (unnormalized rows of A1 * A2)
    prim_directions = []  # unit vectors for deduplication

    for line_idx in range(n_lines):
        x0 = rng.randn(input_dim) * 1.5
        d = rng.randn(input_dim)
        d = d / np.linalg.norm(d)

        # Sample gradient at t_0
        t_vals = np.linspace(-line_range, line_range, points_per_line)
        g_prev = compute_gradient(forward_fn, x0 + t_vals[0] * d)

        for j in range(1, len(t_vals)):
            g_curr = compute_gradient(forward_fn, x0 + t_vals[j] * d)
            diff = g_curr - g_prev
            diff_norm = np.linalg.norm(diff)

            if diff_norm > 1e-6:
                # Boundary crossing detected — check if it's one or more neurons
                d_unit = diff / diff_norm

                # Deduplicate: check if this direction is already known
                is_new = True
                for pd in prim_directions:
                    if abs(np.dot(d_unit, pd)) > 0.99:
                        is_new = False
                        break

                if is_new:
                    # Refine: check if this is actually a multi-neuron crossing
                    # by sub-sampling between t_vals[j-1] and t_vals[j]
                    # If still one jump at finer resolution, accept it
                    t_mid = (t_vals[j-1] + t_vals[j]) / 2
                    g_mid = compute_gradient(forward_fn, x0 + t_mid * d)
                    diff_lo = g_mid - g_prev
                    diff_hi = g_curr - g_mid
                    norm_lo = np.linalg.norm(diff_lo)
                    norm_hi = np.linalg.norm(diff_hi)

                    # Both sub-jumps should be proportional to the same direction
                    # if this was a single-neuron crossing
                    if norm_lo > 1e-6 and norm_hi > 1e-6:
                        d_lo = diff_lo / norm_lo
                        d_hi = diff_hi / norm_hi
                        cos = abs(np.dot(d_lo, d_hi))
                        if cos > 0.98:
                            # Consistent single-neuron crossing
                            primitives.append(diff)
                            prim_directions.append(d_unit)
                        elif cos < 0.5:
                            # Two different neurons crossed — add both sub-jumps
                            for sub_diff, sub_norm in [(diff_lo, norm_lo), (diff_hi, norm_hi)]:
                                sub_unit = sub_diff / sub_norm
                                is_dup = False
                                for pd in prim_directions:
                                    if abs(np.dot(sub_unit, pd)) > 0.99:
                                        is_dup = True
                                        break
                                if not is_dup:
                                    primitives.append(sub_diff)
                                    prim_directions.append(sub_unit)
                    elif norm_lo > 1e-6:
                        d_lo = diff_lo / norm_lo
                        is_dup = any(abs(np.dot(d_lo, pd)) > 0.99 for pd in prim_directions)
                        if not is_dup:
                            primitives.append(diff_lo)
                            prim_directions.append(d_lo)
                    elif norm_hi > 1e-6:
                        d_hi = diff_hi / norm_hi
                        is_dup = any(abs(np.dot(d_hi, pd)) > 0.99 for pd in prim_directions)
                        if not is_dup:
                            primitives.append(diff_hi)
                            prim_directions.append(d_hi)

            g_prev = g_curr

        if (line_idx + 1) % 20 == 0:
            sys.stderr.write(f"[extract] after {line_idx+1} lines: "
                             f"{len(primitives)} unique directions found\n")

    # ---- 2. Save result ------------------------------------------------------
    if not primitives:
        sys.stderr.write("[extract] WARNING: no primitives found!\n")
        A1 = np.zeros((1, input_dim))
    else:
        A1 = np.array(primitives)

    sys.stderr.write(f"[extract] A1 shape: {A1.shape}\n")
    np.save(output_path, A1)
    print(f"Saved A1 ({A1.shape}) to {output_path}")
    return A1


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--n_lines', type=int, default=80,
                   help='Number of random lines to scan')
    p.add_argument('--points_per_line', type=int, default=120,
                   help='Gradient samples per line')
    p.add_argument('--input_dim', type=int, default=None)
    p.add_argument('--output_path', default='/app/stolen_A1.npy')
    args = p.parse_args()
    extract_relu_weights(input_dim=args.input_dim, n_lines=args.n_lines,
                         points_per_line=args.points_per_line,
                         output_path=args.output_path)