#!/bin/bash
# deprecated-api-search.sh — Search for deprecated NumPy/Python API patterns
# across ALL source file types (including .pyx, .pxd, .i, templates).
#
# NOTE: The /skill directory is read-only, so this script lacks execute permission.
# Always invoke with: bash /skill/scripts/deprecated-api-search.sh <directory>
# Do NOT try chmod +x — it will fail with 'Read-only file system'.
#
# Usage: bash deprecated-api-search.sh <directory> [pattern ...]
# If no patterns given, searches for common deprecated NumPy type aliases.
# If patterns given, uses them as additional grep patterns.
#
# Always searches WITHOUT --include filters so Cython, SWIG, and template
# files are not missed. Excludes .git directories and .so/.pyc binaries.
# Exit 0 if no deprecated patterns found, 1 if any found.

set -euo pipefail

if [ $# -eq 0 ]; then
    echo "Usage: deprecated-api-search.sh <directory> [pattern ...]"
    echo "Example: deprecated-api-search.sh /app/myproject"
    echo "Example: deprecated-api-search.sh /app/myproject 'n\\.int' 'np\\.float'"
    exit 2
fi

dir="$1"
shift

# Default deprecated patterns: NumPy type aliases removed in NumPy 2.0
# and Python 3.13 fractions.gcd removal.
if [ $# -eq 0 ]; then
    patterns=(
        'n\.int\b'
        'np\.int\b'
        'numpy\.int\b'
        'n\.float\b'
        'np\.float\b'
        'numpy\.float\b'
        'n\.complex\b'
        'np\.complex\b'
        'numpy\.complex\b'
        'n\.str\b'
        'np\.str\b'
        'n\.bool\b'
        'np\.bool\b'
        'n\.long\b'
        'np\.long\b'
        'from fractions import gcd'
    )
else
    patterns=("$@")
fi

# Exclusion patterns: these are the REPLACEMENT types (not deprecated)
exclude_patterns=(
    'n\.int64'
    'n\.int32'
    'n\.int_'
    'np\.int64'
    'np\.int32'
    'n\.float64'
    'n\.float32'
    'np\.float64'
    'np\.float32'
    'n\.complex128'
    'n\.complex64'
    'np\.complex128'
    'np\.complex64'
)

found=0
for pattern in "${patterns[@]}"; do
    # Search ALL file types, excluding .git and binary extensions
    matches=$(grep -rn "$pattern" "$dir" \
        --exclude-dir=.git \
        --exclude='*.pyc' --exclude='*.so' --exclude='*.o' --exclude='*.egg-info' \
        2>/dev/null || true)

    if [ -z "$matches" ]; then
        continue
    fi

    # Filter out already-fixed replacement types
    filtered="$matches"
    for excl in "${exclude_patterns[@]}"; do
        filtered=$(echo "$filtered" | grep -v "$excl" || true)
    done

    # Also filter lines that are just error-handling (e.g., 'has no attribute .int')
    filtered=$(echo "$filtered" | grep -v 'no attribute' || true)

    if [ -n "$filtered" ]; then
        echo "=== DEPRECATED: $pattern ==="
        echo "$filtered"
        found=1
    fi
done

if [ $found -eq 1 ]; then
    echo ""
    echo "WARNING: Deprecated API patterns found above. These MUST be fixed in ALL"
    echo "source file types (.py, .pyx, .pxd, .i, etc.) before recompiling."
    echo "Common replacements: n.int -> int or n.int64, n.float -> n.float64,"
    echo "n.complex -> n.complex128, np.int -> np.int64, np.float -> np.float64"
    exit 1
else
    echo "No deprecated API patterns found across any source file type."
    exit 0
fi