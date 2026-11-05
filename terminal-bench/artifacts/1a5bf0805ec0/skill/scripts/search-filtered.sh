#!/bin/bash
# search-filtered.sh — Search for secret-token patterns in all files using
# length-based matching that excludes short code identifiers automatically.
# Returns clean output showing ONLY the matched token values (not full file
# lines), so real tokens in long JSON/data files are not buried in noise.
#
# Usage: search-filtered.sh <pattern1> [pattern2] ...
# Example: search-filtered.sh AKIA ghp_ gho_ ghu_ hf_
#
# For each prefix, expands it to a length-based regex (e.g. hf_ → hf_[a-zA-Z0-9]{20,})
# that matches real tokens but NOT short code identifiers like hf_model or hf_token.
# Output shows only MATCH: <file>:<line>: <token_value> lines, so the agent can
# immediately see the actual token and distinguish it from surrounding content.
# Exit code 0 if no matches, 1 if any found.

set -euo pipefail

if [ $# -eq 0 ]; then
    echo "Usage: search-filtered.sh <pattern1> [pattern2] ..."
    echo "Example: search-filtered.sh AKIA ghp_ gho_ ghu_ hf_"
    exit 2
fi

# Token-length heuristics for known secret prefixes.
# Real secret tokens have distinctive lengths that separate them from
# short code identifiers. Using length-based matching eliminates the
# need for brittle code-reference lists that can filter out actual
# secrets on the same line as code references (e.g., a JSON line
# containing both "hf_token" as a key and a real HF token as a value).
get_token_pattern() {
    case "$1" in
        hf_)   echo 'hf_[a-zA-Z0-9]{20,}' ;;
        AKIA)  echo 'AKIA[A-Z0-9]{12,}' ;;
        ghp_)  echo 'ghp_[a-zA-Z0-9]{30,}' ;;
        gho_)  echo 'gho_[a-zA-Z0-9]{30,}' ;;
        ghu_)  echo 'ghu_[a-zA-Z0-9]{30,}' ;;
        *)     echo "$1" ;;  # fallback: use prefix as-is
    esac
}

found=0
for prefix in "$@"; do
    token_pattern=$(get_token_pattern "$prefix")
    # Step 1: Find all files containing matches (file:line only, no content)
    file_lines=$(grep -rPn "$token_pattern" . --exclude-dir=.git 2>/dev/null | cut -d: -f1-2 || true)
    if [ -z "$file_lines" ]; then
        continue
    fi

    # Step 2: For each file:line, extract ONLY the matched token values (not full lines).
    # This avoids displaying long JSON lines that bury the actual token in noise.
    while IFS= read -r fl; do
        file=$(echo "$fl" | cut -d: -f1)
        linenum=$(echo "$fl" | cut -d: -f2)
        # Extract only the matched token strings from this line, filter out placeholders
        matches=$(sed -n "${linenum}p" "$file" | grep -oP "$token_pattern" | grep -v '<your-' || true)
        for m in $matches; do
            echo "MATCH: $file:$linenum: $m"
            found=1
        done
    done <<< "$file_lines"
done

if [ $found -eq 1 ]; then
    echo ""
    echo "WARNING: Actual secret tokens found above (lines starting with MATCH:)."
    echo "Replace each MATCH value with the appropriate <your-...> placeholder, then re-run this script."
    echo "Do NOT dismiss these matches — the length-based pattern already excludes short code identifiers."
    exit 1
else
    echo "All token-pattern searches returned zero matches (after filtering placeholders)."
    exit 0
fi