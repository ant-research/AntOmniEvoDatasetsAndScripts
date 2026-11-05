# Encrypted Archive Hash Cracking

## Table of Contents
1. [Hash extraction workflow](#hash-extraction-workflow)
2. [Choosing a cracking strategy](#choosing-a-cracking-strategy)
3. [Password validation: test vs extract](#password-validation-test-vs-extract)
4. [Handling missing dependencies](#handling-missing-dependencies)

## Hash extraction workflow

To crack a password-protected archive, first extract the hash, then crack it:

1. **Identify the archive type** (7z, zip, rar, openssl-encrypted) and locate the corresponding `*2john` tool in `/app/john/run/` (or install John the Ripper if not present).
2. **Extract the hash** using the appropriate tool:
   - 7z: `perl /app/john/run/7z2john.pl archive.7z > /tmp/hash.txt`
   - zip: `/app/john/run/zip2john archive.zip > /tmp/hash.txt`
   - rar: `/app/john/run/rar2john archive.rar > /tmp/hash.txt`
3. **Crack with John**: `/app/john/run/john [options] /tmp/hash.txt`

Run the `*2john` tool without output redirection first to catch errors (missing Perl modules, malformed archives). If it fails, install missing dependencies and retry.

## Choosing a cracking strategy

Encrypted archives (especially 7z AES) use high iteration counts, making each
password test expensive. A wrong strategy can take hours; the right one takes
seconds. **Always estimate cracking time from the initial rate before committing**
to a long run.

### Step 1: Short-password brute-force first

Start with John's incremental mode for short passwords (1–4 characters):
```
/app/john/run/john --incremental --min-length=1 --max-length=4 /tmp/hash.txt
```
This covers digits, short words, and common patterns. For 7z AES at ~30 p/s,
4-character lowercase+digit brute-force (~2M candidates) finishes in ~18 hours
but 1–4 character ASCII with the default charset completes much faster due to
John's frequency-ordered candidate generation.

### Step 2: Targeted wordlist with common passwords

If incremental mode fails, test the most common passwords from the wordlist:
```
head -3546 /app/john/run/password.lst | grep -v '^#!' > /tmp/common_pw.txt
/app/john/run/john --wordlist=/tmp/common_pw.txt /tmp/hash.txt
```
The first 3546 entries in password.lst are the most commonly observed passwords
(sorted by frequency), making this the highest-yield subset.

### Step 3: Full wordlist (only if time permits)

Only attempt the full wordlist if the estimated time fits the budget:
```
/app/john/run/john --wordlist=/app/john/run/password.lst /tmp/hash.txt
```
At ~30 p/s for 7z AES, the full 1.8M list takes ~16 hours. **If the ETA exceeds
your remaining budget, do not start this** — try other approaches instead (different
wordlists, rule-based mutations, or checking for hints in the task environment).

### Step 4: Rules and mutations

If the wordlist alone fails, apply John's built-in rules for common mutations:
```
/app/john/run/john --wordlist=/app/john/run/password.lst --rules /tmp/hash.txt
```

## Password validation: test vs extract

**Always use `7z t -p<password>` (test mode) for password verification, not
`7z x -p<password>` (extract mode).**

- `7z t` validates the password without creating any files. It returns exit code 0
  on success, non-zero on failure. Fast and clean.
- `7z x` extracts files to disk, creating directories and files even for wrong
  passwords (7z creates the directory structure before validating). This leaves
  behind corrupted files and cluttered /tmp directories that cause confusion.

Example batch test loop:
```bash
while read -r pw; do
  if 7z t -p"$pw" archive.7z >/dev/null 2>&1; then
    echo "FOUND: $pw"
    break
  fi
done < /tmp/wordlist.txt
```

After finding the password with `7z t`, do a single clean extraction:
```bash
7z x -p"<password>" archive.7z -o/tmp/extracted
```

## Handling missing dependencies

`*2john` Perl scripts may require modules not installed by default:
- `7z2john.pl` requires `Compress::Raw::Lzma` — install with
  `apt-get install -y libcompress-raw-lzma-perl`
- Other scripts may require `Compress::Zlib`, `Digest`, etc.

Run the `*2john` script without output redirection first to see errors directly.
If a module is missing, install it and retry. Do not redirect stderr to /dev/null
when running these tools for the first time.

### Fallback: non-interactive CPAN installation

When `apt-get install` fails (repos unavailable, offline environment), install
Perl modules via CPAN in **non-interactive** mode:
```
PERL_MM_USE_DEFAULT=1 perl -MCPAN -e 'install Compress::Raw::Lzma'
```
**NEVER run CPAN without `PERL_MM_USE_DEFAULT=1`** — CPAN opens an interactive
configuration prompt that reads from /dev/tty, making it unrecoverable in a
tmux session. This has caused terminal death spirals costing 200+ turns.

### Fallback: Python-based hash extraction

If the Perl module cannot be installed by any method, extract the 7z hash
manually with a Python script:
1. Read the raw 7z file bytes.
2. Parse the 7z signature header (magic `377abcaf271c`), find the
   packed stream position, extract the encryption parameters (salt, IV,
   iteration count) from the codec metadata.
3. Construct the john-compatible hash string:
   `$7z$0$19$0$salt_hex$iv_hex$iter$count$encoded_crc$
   The exact format is documented in John's 7z_fmt_plug.c source.
4. Write the hash to a file and crack with john as usual.

### Fallback: direct password testing with py7zr

If `pip install py7zr` succeeds (even when apt fails), use it to test
candidate passwords directly against the archive:
```python
import py7zr, io
passwords = open('/app/john/run/password.lst').read().splitlines()
for pw in passwords:
    try:
        with py7zr.SevenZipFile('/app/secrets.7z', 'r', password=pw) as z:
            print(f'FOUND: {pw}')
            break
    except Exception:
        continue
```
This avoids hash extraction entirely and works when only py7zr is available.