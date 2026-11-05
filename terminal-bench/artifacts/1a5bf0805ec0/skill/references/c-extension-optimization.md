# C Extension Optimization for Numerical Computation

## Table of Contents
1. [Python-level shortcut: scipy.linalg.lapack direct calls](#python-level-shortcut-scipylinalg-lapack-direct-calls)
2. [When to use optimized libraries](#when-to-use-optimized-libraries)
3. [Checking for BLAS/LAPACK](#checking-for-blaslapack)
4. [CBLAS API for common operations](#cblas-api-for-common-operations)
5. [setup.py configuration](#setuppy-configuration)

## Python-level shortcut: scipy.linalg.lapack direct calls

Before writing a C extension, try `scipy.linalg.lapack` direct LAPACK calls instead
of high-level `numpy.linalg` wrappers. For small matrices (up to ~50x50), numpy's
Python overhead (input validation, array copying, type coercion, complex output
formatting) can dominate the actual LAPACK computation time. Direct calls through
`scipy.linalg.lapack` bypass most of this overhead.

Common replacements:
- `np.linalg.eig(A)` → `scipy.linalg.lapack.dgeev(A, compute_vl=False, compute_vr=True)`
  Returns (wr, wi, vl, vr, info). Eigenvalues are wr + 1j*wi. For complex conjugate
  pairs, eigenvectors come in consecutive columns: vec = vr[:,i] + 1j*vr[:,i+1].
- `np.linalg.solve(A, b)` → `scipy.linalg.lapack.dgesv(A, b)`
- `np.linalg.inv(A)` → `scipy.linalg.lapack.dgetrf` + `dgetri`

Speedup is typically 1.3x-2x for matrices up to 10x10. For larger matrices the
overhead fraction shrinks and the speedup diminishes. This approach avoids compilation,
build dependencies, and deployment complexity of a C extension.

**Micro-optimization for hot-path calls**: When the function is called repeatedly
(e.g., in a benchmark loop), cache the LAPACK function reference at module level
to avoid repeated attribute lookups through `scipy.linalg.lapack.dgeev` on each
call. Example:
```python
from scipy.linalg import lapack
_dgeev = lapack.dgeev  # cache at module level
def find_dominant_eigenvalue(A):
    wr, wi, vl, vr, info = _dgeev(A, compute_vl=False, compute_vr=True)
    ...
```
This saves ~10-20% of per-call overhead (attribute traversal + dict lookup) and
can make the difference for medium matrices (8-10x10) where the speedup margin
over numpy.linalg.eig is thinnest.

## When to use optimized libraries

When a C extension must perform matrix or vector operations (matrix-vector multiply,
dot product, matrix-matrix multiply, linear solve, eigenvalue decomposition), use
system-optimized BLAS/LAPACK libraries instead of writing raw C loops. Naive C loops
are often only marginally faster than Python for large matrices due to cache effects;
BLAS implementations use cache blocking, SIMD instructions, and multi-threading.

If scipy.linalg.lapack direct calls are not fast enough, THEN write a C extension
using the BLAS/LAPACK APIs below.

## Checking for BLAS/LAPACK

Run `ldconfig -p | grep -i blas` to check if BLAS is available. Common names:
- `libblas.so` — reference BLAS
- `libblas.so.3` — versioned
- `libopenblas.so` — OpenBLAS (optimized)
- `liblapack.so` — LAPACK

## CBLAS API for common operations

Include `<cblas.h>` and link with `-lblas`.

### Matrix-vector multiply: y = alpha * A * x + beta * y

```c
#include <cblas.h>

// For row-major (C-order) matrix A[n x n] and vector x[n]:
// Compute temp = A * x
cblas_dgemv(CblasRowMajor, CblasNoTrans,
            n, n, 1.0, A, n, x, 1, 0.0, temp, 1);
```

### Dot product: result = x^T * y

```c
double result = cblas_ddot(n, x, 1, y, 1);
```

### Full example: portfolio risk sqrt(x^T * S * x)

```c
#include <cblas.h>
#include <math.h>

// Step 1: temp = S * x
double *temp = (double*)malloc(n * sizeof(double));
cblas_dgemv(CblasRowMajor, CblasNoTrans,
            n, n, 1.0, cov, n, weights, 1, 0.0, temp, 1);

// Step 2: risk_squared = x^T * temp
double risk_squared = cblas_ddot(n, weights, 1, temp, 1);
double risk = sqrt(risk_squared);
free(temp);
```

## setup.py configuration

```python
from setuptools import Extension, setup
import numpy

module = Extension('module_name',
                   sources=['source.c'],
                   include_dirs=[numpy.get_include()],
                   libraries=['blas'],        # Link BLAS
                   extra_compile_args=['-O3'])  # Avoid -ffast-math for precision

setup(name='module_name', ext_modules=[module])
```

**Important**: Do NOT use `-ffast-math` when numerical precision matters (e.g., 1e-10
tolerance). `-ffast-math` enables unsafe floating-point optimizations that can violate
IEEE 754 compliance.

## Key points

1. Always check for BLAS/LAPACK before writing manual matrix operation loops in C.
2. Use CBLAS row-major interfaces (`CblasRowMajor`) for NumPy arrays (C-order by default).
3. Link with `libraries=['blas']` in setup.py.
4. Avoid `-ffast-math` when precision is required.
5. For LAPACK operations (linear solve, eigenvalues), use `lapacke.h` and link `['lapack']`.