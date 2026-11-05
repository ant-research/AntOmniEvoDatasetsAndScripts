# Git Forensics & History Cleanup

## Table of Contents
1. [Finding erased commits](#finding-erased-commits)
2. [Inspecting dangling objects](#inspecting-dangling-objects)
3. [Cleanup sequence](#cleanup-sequence)
4. [Verification checklist](#verification-checklist)

## Finding erased commits

When a secret or content was committed then removed by rewriting history, the commit objects often remain as dangling objects. Find them:

- `git reflog` — shows HEAD movements including resets and rebases that removed commits
- `git fsck --lost-found` — lists all dangling commits, blobs, and trees; writes them to `.git/lost-found/`
- `git log --all --diff-filter=D -- <path>` — find commits that deleted a file

## Inspecting dangling objects

- `git show <hash>` — show commit message and diff for a dangling commit
- `git cat-file -p <hash>` — raw contents of any git object
- `git cat-file -t <hash>` — type of object (commit, blob, tree)
- `git diff <hash>^ <hash>` — full diff introduced by a commit

## Cleanup sequence

To permanently remove dangling objects containing secrets from a repository:

1. `git reflog expire --expire=now --all` — expire all reflog entries so they no longer keep dangling commits reachable
2. `git gc --prune=now` — garbage-collect and prune all unreachable objects immediately. Use `--aggressive` for more thorough repacking

If the secret was in a branch that was force-pushed or rebased:
1. `git reflog expire --expire=now --all`
2. `git repack -a -d` followed by `git prune`
3. `git gc --prune=now`

## Verification checklist

After cleanup, verify no traces remain. Run ALL of these checks — each catches different edge cases:

1. `git show <dangling-hash>` — must fail with "unknown revision"
2. `git reflog` — must be empty (no entries referencing the removed commit)
3. `git fsck --unreachable` — must report no dangling objects
4. `git fsck --lost-found` — must report no dangling objects
5. `git log --all -p -S 'secret_pattern'` — must find no commits containing the secret string
6. `grep -r 'secret_pattern' .git/` — must find no plaintext traces in the git directory
7. `strings .git/objects/pack/*.pack 2>/dev/null | grep -i secret_pattern` — must find no traces in pack files

Also verify that unrelated commits and files remain intact:
- `git log --oneline --all` — check commit history is preserved
- `cat <unrelated-file>` — check working tree files are unchanged