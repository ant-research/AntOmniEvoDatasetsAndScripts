# Multi-Language Source Files (Polyglot Strategy)

## When to load this reference

WHEN creating a source file that must be simultaneously valid in multiple
programming languages or execution contexts (e.g., a file runnable by both
Python and a C compiler, or by both Bash and Python).

## Core Constraint

Every parser/interpreter/compiler that reads the file must accept it as
syntactically valid. A Python/C polyglot fails if Python raises SyntaxError
on C code OR if gcc rejects Python syntax. Both parsers must succeed.

## General Techniques

### 1. Dual-purpose comment/preprocessor constructs

In C, `#if 0 ... #endif` excludes code from compilation. In Python, any
line starting with `#` is a comment. So `#if 0` and `#endif` are comments to
Python but preprocessor directives to C. Use this to hide Python code from
C (wrap Python code in `#if 0`/`#endif` — C skips it, Python runs it
because these are just comments).

### 2. String-literal hiding

When one language (e.g., C) requires code that is syntactically invalid in
the other (e.g., Python), hide that code inside a multi-line string literal
in the second language. Python's `"""..."""` triple-quoted strings can
contain arbitrary text. The key: place the opening `"""` AFTER an `exit()`
call so Python never executes the string — it only needs to parse it.

For C, the `"""` delimiters must be inside `#if 0`/`#endif` blocks so
the C preprocessor never sees them. C's preprocessor removes these blocks
before the C compiler parses the file.

### 3. Early exit in one context

Use `exit()` (Python) or an early `return` (any language) in the code path
of one language before it reaches code intended for the other language.
This lets you have `"""` open a string after Python exits, so the C code
inside the string is syntactically valid Python but never executed.

## Worked Example: Python/C Polyglot

```
#if 0
import sys
def fib(n):
    if n <= 1: return n
    a, b = 0, 1
    for _ in range(2, n + 1): a, b = b, a + b
    return b
print(fib(int(sys.argv[1])))
exit()
"""
#endif
#include <stdio.h>
#include <stdlib.h>
long long fib(int n) {
    if (n <= 1) return n;
    long long a = 0, b = 1;
    for (int i = 2; i <= n; i++) {
        long long temp = a + b;
        a = b;
        b = temp;
    }
    return b;
}
int main(int argc, char **argv) {
    printf("%lld\n", fib(atoi(argv[1])));
    return 0;
}
#if 0
"""
#endif
```

What Python sees:
- `#if 0` — comment
- Python code — executes, prints Fibonacci, calls exit()
- `"""` — starts a triple-quoted string (never executed due to exit())
- `#endif` through `int main(...)` — all inside the string (syntactically valid)
- `#if 0` — inside the string
- `"""` — ends the string
- `#endif` — comment

What C sees:
- `#if 0` through `exit()` — preprocessed away
- `"""` inside `#if 0` — preprocessed away
- `#endif` — ends the skip block
- `#include`, function definitions, `main` — compiled normally
- `#if 0` through `"""` — preprocessed away
- `#endif` — ends the skip block

## Common Mistakes

1. Putting `"""` outside `#if 0`/`#endif` — C compiler sees the triple quotes
   and produces errors or warnings about unterminated string literals.
2. Forgetting `exit()` before `"""` — Python reaches the string and treats
   it as a no-op expression, but more critically, any code AFTER the closing
   `"""` that is not valid Python will cause a SyntaxError.
3. Using `char*_="""` or `#define X """` — these are not valid Python syntax
   and cause SyntaxError before Python even starts executing.
4. Assuming Python only parses up to `exit()` — Python parses the ENTIRE
   file for syntax validity before executing any line. Code after `exit()`
   must still be syntactically valid Python.