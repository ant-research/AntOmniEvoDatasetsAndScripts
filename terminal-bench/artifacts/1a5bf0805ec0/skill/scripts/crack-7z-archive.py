#!/usr/bin/env python3
"""Automated 7z archive password cracking script.

Handles the full workflow: dependency installation, hash extraction,
and password cracking with fallbacks. Usage:
  python3 /skill/scripts/crack-7z-archive.py /path/to/archive.7z /path/to/solution.txt

The script tries these approaches in order:
1. Install liblzma-dev + Compress::Raw::Lzma via non-interactive CPAN,
   then use 7z2john.pl + john with password.lst
2. pip install py7zr (with --index-url fallback) and test passwords from
   /app/john/run/password.lst against the archive
3. If 7z binary is available, use `7z t` with password.lst entries
"""
import subprocess
import sys
import os

def run(cmd, timeout=120):
    """Run a shell command, return (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, '', 'timeout'

def install_liblzma_dev():
    """Install liblzma-dev so CPAN can compile Compress::Raw::Lzma."""
    rc, out, err = run('apt-get install -y liblzma-dev 2>&1', timeout=60)
    return rc == 0

def install_cpan_module():
    """Install Compress::Raw::Lzma via non-interactive CPAN."""
    rc, out, err = run(
        "PERL_MM_USE_DEFAULT=1 perl -MCPAN -e 'install Compress::Raw::Lzma' 2>&1",
        timeout=300
    )
    # Verify installation
    rc2, _, _ = run("perl -e 'use Compress::Raw::Lzma; print 1' 2>&1")
    return rc2 == 0

def extract_hash_with_7z2john(archive_path):
    """Try to extract hash using 7z2john.pl."""
    hash_file = '/tmp/7z_hash.txt'
    rc, out, err = run(
        f'perl /app/john/run/7z2john.pl {archive_path} > {hash_file} 2>&1'
    )
    if rc != 0:
        return None
    try:
        content = open(hash_file).read().strip()
        if content and '$7z$' in content:
            return hash_file
    except:
        pass
    return None

def crack_with_john(hash_file, archive_path):
    """Crack hash using john with password.lst, return password or None."""
    pot_file = '/tmp/john_crack.pot'
    # Remove old pot to ensure fresh crack
    run(f'rm -f {pot_file}')
    rc, out, err = run(
        f'/app/john/run/john --pot={pot_file} '
        f'--wordlist=/app/john/run/password.lst {hash_file} 2>&1',
        timeout=600
    )
    # Read cracked password
    rc2, out2, _ = run(f'/app/john/run/john --pot={pot_file} --show {hash_file} 2>&1')
    if out2:
        for line in out2.split('\n'):
            if ':' in line and not line.startswith('0 password'):
                password = line.split(':')[1] if ':' in line else None
                if password:
                    return password
    return None

def crack_incremental_with_john(hash_file):
    """Try john incremental mode for short passwords (1-4 chars).

    Per archive-hash-cracking.md Step 1, this is the fastest approach
    for 7z AES because short passwords (4-digit PINs, short words)
    are found in seconds vs. thousands of wordlist entries at ~30 p/s.
    """
    pot_file = '/tmp/john_incremental.pot'
    run(f'rm -f {pot_file}')
    rc, out, err = run(
        f'/app/john/run/john --pot={pot_file} '
        f'--incremental --min-length=1 --max-length=4 {hash_file} 2>&1',
        timeout=300
    )
    rc2, out2, _ = run(f'/app/john/run/john --pot={pot_file} --show {hash_file} 2>&1')
    if out2:
        for line in out2.split('\n'):
            if ':' in line and not line.startswith('0 password'):
                password = line.split(':')[1] if ':' in line else None
                if password:
                    return password
    return None

def extract_archive_content(archive_path, password):
    """Extract archive with the given password and return text file contents.

    After cracking the password, tasks typically require the CONTENT of
    files inside the archive, not the password itself. Per
    archive-hash-cracking.md: 'After finding the password with 7z t,
    do a single clean extraction: 7z x -p<password> archive.7z
    -o/tmp/extracted'.
    """
    import shutil
    extract_dir = '/tmp/7z_extracted'
    if os.path.exists(extract_dir):
        shutil.rmtree(extract_dir)
    os.makedirs(extract_dir, exist_ok=True)
    # Try 7z command first
    rc, _, _ = run(f'7z x -p{password} {archive_path} -o{extract_dir} -y 2>&1', timeout=30)
    if rc != 0 or not os.listdir(extract_dir):
        # Fallback: try py7zr
        try:
            import py7zr
            with py7zr.SevenZipFile(archive_path, 'r', password=password) as z:
                z.extractall(path=extract_dir)
        except Exception:
            pass
    # Read all text files in extracted directory
    contents = {}
    if os.path.exists(extract_dir):
        for root, dirs, files in os.walk(extract_dir):
            for fname in files:
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, 'r', errors='replace') as f:
                        content = f.read().strip()
                        if content:
                            contents[fpath] = content
                except Exception:
                    pass
    return contents

def install_py7zr():
    """Install py7zr, trying default mirror first then pypi.org."""
    rc, _, _ = run('pip3 install py7zr 2>&1', timeout=60)
    if rc == 0:
        return True
    # Fallback to pypi.org (default mirror may not have it)
    rc, _, _ = run(
        'pip3 install py7zr --index-url https://pypi.org/simple/ 2>&1',
        timeout=120
    )
    if rc == 0:
        return True
    return False

def crack_with_py7zr(archive_path, solution_path):
    """Use py7zr to test passwords from password.lst."""
    script = f'''
import py7zr
import sys

archive = {repr(archive_path)}
wordlist = '/app/john/run/password.lst'

total = 0
found = None
with open(wordlist, 'r', errors='ignore') as f:
    for line in f:
        pw = line.strip()
        if not pw or pw.startswith('#'):
            continue
        total += 1
        if total %% 500 == 0:
            print(f'Tested {{total}} passwords...', flush=True)
        try:
            with py7zr.SevenZipFile(archive, 'r', password=pw) as z:
                found = pw
                break
        except Exception:
            continue

if found:
    print(f'FOUND: {{found}}')
    with open({repr(solution_path)}, 'w') as out:
        out.write(found)
else:
    print(f'Not found after {{total}} passwords')
    sys.exit(1)
'''
    script_file = '/tmp/py7zr_crack.py'
    with open(script_file, 'w') as f:
        f.write(script)
    rc, out, err = run(f'python3 -u {script_file} 2>&1', timeout=600)
    print(out)
    if 'FOUND:' in out:
        return True
    return False

def crack_with_7z_test(archive_path, solution_path):
    """Use `7z t` command to test passwords if 7z binary available."""
    # Check if 7z is available
    rc, _, _ = run('which 7z 2>/dev/null')
    if rc != 0:
        return False
    script = f'''
import subprocess
import sys

archive = {repr(archive_path)}
wordlist = '/app/john/run/password.lst'

total = 0
found = None
with open(wordlist, 'r', errors='ignore') as f:
    for line in f:
        pw = line.strip()
        if not pw or pw.startswith('#'):
            continue
        total += 1
        if total %% 500 == 0:
            print(f'Tested {{total}} passwords...', flush=True)
        r = subprocess.run(
            ['7z', 't', f'-p{{pw}}', archive],
            capture_output=True, timeout=10
        )
        if r.returncode == 0:
            found = pw
            break

if found:
    print(f'FOUND: {{found}}')
    with open({repr(solution_path)}, 'w') as out:
        out.write(found)
else:
    print(f'Not found after {{total}} passwords')
    sys.exit(1)
'''
    script_file = '/tmp/7zt_crack.py'
    with open(script_file, 'w') as f:
        f.write(script)
    rc, out, err = run(f'python3 -u {script_file} 2>&1', timeout=600)
    print(out)
    if 'FOUND:' in out:
        return True
    return False

def main():
    if len(sys.argv) < 3:
        print(f'Usage: {sys.argv[0]} <archive.7z> <solution_output_path>')
        sys.exit(1)

    archive_path = sys.argv[1]
    solution_path = sys.argv[2]

    if not os.path.exists(archive_path):
        print(f'ERROR: Archive not found: {archive_path}')
        sys.exit(1)

    # Approach 1: 7z2john.pl + john (most reliable)
    print('=== Approach 1: liblzma-dev + CPAN + 7z2john.pl + john ===')
    print('Installing liblzma-dev...')
    install_liblzma_dev()
    print('Installing Compress::Raw::Lzma via CPAN...')
    if install_cpan_module():
        print('CPAN module installed successfully.')
        hash_file = extract_hash_with_7z2john(archive_path)
        if hash_file:
            print(f'Hash extracted to {hash_file}')
            # Step 1: Try incremental mode first (fast for short passwords)
            print('Trying incremental mode (1-4 chars)...')
            password = crack_incremental_with_john(hash_file)
            if not password:
                # Step 2: Fall back to wordlist
                print('Incremental mode did not find password. Trying wordlist...')
                password = crack_with_john(hash_file, archive_path)
            if password:
                print(f'PASSWORD FOUND: {password}')
                contents = extract_archive_content(archive_path, password)
                if contents:
                    for fpath, content in contents.items():
                        print(f'  {fpath}: {content}')
                    first_content = next(iter(contents.values()))
                    with open(solution_path, 'w') as f:
                        f.write(first_content)
                    print(f'Written extracted content to {solution_path}')
                else:
                    with open(solution_path, 'w') as f:
                        f.write(password)
                    print(f'No text files found in archive; written password to {solution_path}')
                return
            else:
                print('John did not find password with incremental or wordlist mode.')
        else:
            print('7z2john.pl hash extraction failed.')
    else:
        print('CPAN module installation failed.')

    # Approach 2: py7zr
    print('=== Approach 2: py7zr password testing ===')
    if install_py7zr():
        print('py7zr installed successfully.')
        if crack_with_py7zr(archive_path, solution_path):
            # crack_with_py7zr wrote the password; replace with archive content
            _upgrade_solution_to_content(archive_path, solution_path)
            return
        print('py7zr did not find password.')
    else:
        print('py7zr installation failed.')

    # Approach 3: 7z t command
    print('=== Approach 3: 7z t password testing ===')
    if crack_with_7z_test(archive_path, solution_path):
        # crack_with_7z_test wrote the password; replace with archive content
        _upgrade_solution_to_content(archive_path, solution_path)
        return
    print('7z t did not find password.')

    print('All approaches failed to crack the archive password.')
    sys.exit(1)

def _upgrade_solution_to_content(archive_path, solution_path):
    """Read the password from solution file and replace it with extracted content.

    crack_with_py7zr and crack_with_7z_test write the cracked password to
    the solution file. But the task usually requires the CONTENT of files
    inside the archive, not the password. This function reads the password,
    extracts the archive, and overwrites the solution file with the content.
    """
    try:
        with open(solution_path, 'r') as f:
            password = f.read().strip()
        if not password:
            return
        contents = extract_archive_content(archive_path, password)
        if contents:
            for fpath, content in contents.items():
                print(f'  {fpath}: {content}')
            first_content = next(iter(contents.values()))
            with open(solution_path, 'w') as f:
                f.write(first_content)
            print(f'Upgraded solution from password to extracted content in {solution_path}')
    except Exception as e:
        print(f'Warning: could not extract archive content: {e}')

if __name__ == '__main__':
    main()