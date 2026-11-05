#!/bin/bash
# extract-function-sigs.sh -- Extract function interface requirements from task text
# Usage: bash extract-function-sigs.sh <task_text_file>
# Reads the task description and outputs function skeletons with correct
# parameter ordering based on the task's stated conventions.
#
# NOTE: The /skill directory is read-only, so this script lacks execute permission.
# Always invoke with: bash /skill/scripts/extract-function-sigs.sh <task_text_file>
# Do NOT try chmod +x — it will fail with 'Read-only file system'.

TASK_FILE="$1"
if [ -z "$TASK_FILE" ] || [ ! -f "$TASK_FILE" ]; then
  echo "Usage: $0 <task_text_file>" >&2
  echo "Save the task description to a file first, then pass it to this script." >&2
  exit 1
fi

echo "=== Function Signature Extraction ==="
echo ""

echo "Required function names (from task):"
grep -oiE '(function named|function called|include a [a-z]+ function named) ['\''"`]*[a-zA-Z_][a-zA-Z0-9_]*' "$TASK_FILE" | \
  sed -E 's/.*named[ ]*|.*called[ ]*//' | sed "s/['\"\`']$//" | sort -u | while read fname; do
  echo "  - $fname"
done

echo ""
echo "Primary input analysis:"
grep -oiE 'primary input should be (a |an )?[a-z- ]+' "$TASK_FILE" | head -3

echo ""
echo "Calling convention clues:"
grep -oiE '(e[.]g[.]|for example|such as|like|dnorm)[^.]*('"'[a-zA-Z_][a-zA-Z0-9_]*"'\\''|[^.)]*)' "$TASK_FILE" | head -5
# Also look for function call patterns in the task
grep -oiE '[a-zA-Z_][a-zA-Z0-9_]* *\([^)]*\)' "$TASK_FILE" | head -10

echo ""
echo "CRITICAL: When the task says 'The primary input should be a function',"
echo "that function MUST be the FIRST parameter in your implementation."
echo "Do NOT follow R's convention of putting n first (as in rnorm, runif)."
echo "The task's stated convention takes precedence over library conventions."
echo ""
echo "Suggested skeleton for R functions where task specifies 'primary input is a function':"
echo "  func_name <- function(density, domain, n = 1000, ...) {"
echo "    # Accept raw density; compute log internally"
echo "    log_h <- function(x) log(density(x))"
echo "    ..."
echo "  }"