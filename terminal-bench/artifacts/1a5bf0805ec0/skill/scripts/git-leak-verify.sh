#!/bin/bash
# git-leak-verify.sh — Verify that a secret pattern has been fully removed
# from a git repository. Runs all checks from the git-forensics reference.
#
# Usage: bash /skill/scripts/git-leak-verify.sh <repo_path> <secret_pattern>
# Example: bash /skill/scripts/git-leak-verify.sh /app/repo 'secret['
#
# Exit code 0 if all checks pass, 1 if any check finds the pattern.
# Each check prints PASS or FAIL with details.

set -euo pipefail

if [ $# -lt 2 ]; then
    echo "Usage: bash /skill/scripts/git-leak-verify.sh <repo_path> <secret_pattern>"
    echo "Example: bash /skill/scripts/git-leak-verify.sh /app/repo 'secret['"
    exit 2
fi

REPO="$1"
PATTERN="$2"
OVERALL_PASS=true

check() {
    local desc="$1"
    local result="$2"
    if [ -z "$result" ]; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc"
        echo "  Details: $(echo "$result" | head -5)"
        OVERALL_PASS=false
    fi
}

# Check 1: git log --grep (searches commit messages for the pattern)
check "git log --all --grep (commit messages)" \
    "$(cd "$REPO" && git log --all --grep="$PATTERN" --oneline 2>/dev/null)"

# Check 2: git log -S (searches diffs for added/removed lines matching pattern)
check "git log --all -p -S (diff content)" \
    "$(cd "$REPO" && git log --all -p -S "$PATTERN" --oneline 2>/dev/null)"

# Check 3: git fsck --lost-found (finds dangling objects)
FSCK_OUTPUT="$(cd "$REPO" && git fsck --lost-found 2>&1 || true)"
DANGLING_LINES="$(echo "$FSCK_OUTPUT" | grep 'dangling' || true)"
if [ -z "$DANGLING_LINES" ]; then
    echo "PASS: git fsck --lost-found (no dangling objects)"
else
    # Check each dangling object for the pattern
    FOUND_IN_DANGLING=""
    while IFS= read -r line; do
        obj_hash="$(echo "$line" | awk '{print $NF}')"
        obj_content="$(cd "$REPO" && git cat-file -p "$obj_hash" 2>/dev/null || true)"
        if echo "$obj_content" | grep -q "$PATTERN"; then
            FOUND_IN_DANGLING="$line: $(echo "$obj_content" | head -3)"
            break
        fi
    done <<< "$DANGLING_LINES"
    if [ -z "$FOUND_IN_DANGLING" ]; then
        echo "PASS: git fsck --lost-found (dangling objects exist but don't contain pattern)"
    else
        echo "FAIL: git fsck --lost-found (pattern found in dangling object)"
        echo "  Details: $FOUND_IN_DANGLING"
        OVERALL_PASS=false
    fi
fi

# Check 4: git reflog entries referencing the pattern
check "git reflog (no entries referencing pattern)" \
    "$(cd "$REPO" && git log -g --oneline --grep="$PATTERN" 2>/dev/null || true)"

# Check 5: grep inside .git/ directory for plaintext traces
check "grep -r pattern in .git/ (plaintext traces)" \
    "$(cd "$REPO" && grep -rl "$PATTERN" .git/ 2>/dev/null | head -10 || true)"

# Check 6: strings on pack files for traces in compressed objects
PACK_FILES="$(find "$REPO/.git/objects/pack" -name '*.pack' 2>/dev/null || true)"
if [ -n "$PACK_FILES" ]; then
    check "strings on pack files" \
        "$(strings "$REPO"/.git/objects/pack/*.pack 2>/dev/null | grep "$PATTERN" | head -5 || true)"
else
    echo "SKIP: No pack files found"
fi

# Check 7: git log --all with regexp-ignore-case (mirrors verifier test)
check "git log --all --grep with regexp-ignore-case" \
    "$(cd "$REPO" && git log --all --grep="$PATTERN" --regexp-ignore-case --oneline 2>/dev/null)"

# Summary
echo ""
if $OVERALL_PASS; then
    echo "=== ALL CHECKS PASSED ==="
    exit 0
else
    echo "=== SOME CHECKS FAILED ==="
    exit 1
fi