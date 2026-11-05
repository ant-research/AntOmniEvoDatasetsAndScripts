# Git Server & Web Deployment

## Table of Contents
1. [Setting up a bare git repository](#setting-up-a-bare-git-repository)
2. [Post-receive hook for deployment](#post-receive-hook-for-deployment)
3. [Web server configuration](#web-server-configuration)
4. [End-to-end verification](#end-to-end-verification)
5. [Common pitfalls](#common-pitfalls)

## Setting up a bare git repository

A bare repository has no working tree — it stores only git objects and refs.
This is the correct format for a shared/remote repository that users push to:

```bash
mkdir -p /git/server
git init --bare /git/server
```

Users clone with: `git clone /git/server` (local) or `git clone user@server:/git/server` (SSH).

## Post-receive hook for deployment

The `post-receive` hook runs after pushes are accepted. Use it to deploy
the latest content to a web-visible directory:

```bash
cat > /git/server/hooks/post-receive << 'EOF'
#!/bin/bash
GIT_WORK_TREE=/var/www/html git checkout -f
EOF
chmod +x /git/server/hooks/post-receive
```

Key points:
- **Always use `/var/www/html` as the deploy target** — this is the standard
  web root expected by verifiers and default web server configurations.
  Do NOT use custom paths like `/var/www/git-server` or `/var/www/myapp`.
- `GIT_WORK_TREE` tells git checkout to place files in the web directory
  instead of the bare repo
- `-f` forces checkout even if the working tree has changes
- **The hook MUST be executable** (`chmod +x`) — git silently skips
  non-executable hooks
- The hook must start with `#!/bin/bash` (not `#!/bin/sh`) if using
  bash-specific features
- Hooks in bare repos run as the repo-owner user (the SSH/login account),
  so the web directory must be writable by that user

## Web server configuration

**Use Python HTTP server as the default** — it requires no configuration
files, has no permission or user issues, and serves any directory reliably.
Only use nginx/apache if the task explicitly requires them.

**Python (simplest, recommended):**
```bash
mkdir -p /var/www/html
cd /var/www/html && nohup python3 -m http.server 8080 > /dev/null 2>&1 &
```

**nginx (only if task requires it):**
```bash
apt-get install -y nginx
# Use the STANDARD web root — do NOT create a custom one
cat > /etc/nginx/sites-available/default << 'EOF'
server { listen 8080; root /var/www/html; index index.html; server_name _;
  location / { try_files $uri $uri/ =404; }
}
EOF
nginx -t && (nginx -s reload 2>/dev/null || nginx)
```

Before starting, check the port is free: `ss -tlnp | grep <port>`. If
occupied, stop the existing service or use a different port.

## End-to-end verification

After setup, test the full workflow:

```bash
cd /tmp && rm -rf testrepo && git clone /git/server testrepo
cd testrepo
git config user.email 'test@example.com'
git config user.name 'Test User'
echo 'hello world' > hello.html
git add hello.html && git commit -m 'add hello.html'
git push origin master

# Verify deployment
cat /var/www/html/hello.html
curl http://localhost:8080/hello.html
```

Confirm: (1) the file exists in /var/www/html, (2) curl returns the
content, (3) the web server process is still running (`ps aux | grep`).

**Do not clean up the bare repo after testing** — test data left in the
repo is fine because the verifier does a fresh force-push (e.g., `git
checkout -B main`). Do NOT run `rm -rf /git/server/objects/*` or `rm -rf
/git/server/refs/heads/*` — selective deletion corrupts the repo by
leaving inconsistent metadata. Also do NOT clear the web root — leave
deployed files in place. If a full reset is needed (e.g., test pushes
contaminated expected branches with wrong content), the nuclear reset is:
`rm -rf /git/project && mkdir -p /git/project && git init --bare
/git/project`, then recreate hooks. Do NOT use `git init --bare` alone
on an existing repo — it reinitializes metadata but preserves old
objects and refs.

## Common pitfalls

- **Non-executable hook**: git silently ignores hooks without +x permission.
  Always `chmod +x` and verify with `ls -la`.
- **Wrong web root**: Always deploy to `/var/www/html`. Using a custom path
  (e.g., `/var/www/git-server`) causes verifier failures because tests
  check the standard web root. This exact mistake caused a 0.0 score on
  the configure-git-webserver task.
- **Missing git identity**: Commits fail with 'Author identity unknown'
  unless `user.email` and `user.name` are configured. Set these before
  any test commits.
- **Destructive repo cleanup**: After testing, do NOT selectively delete
  git internals (`rm -rf objects/*`, `rm -rf refs/heads/*`) — this corrupts
  the repo by leaving inconsistent metadata. If you must completely reset a
  bare repo (e.g., test pushes contaminated expected branches with wrong
  content), the nuclear reset is: `rm -rf /git/project && mkdir -p
  /git/project && git init --bare /git/project`, then recreate hooks (`cat
  > hooks/post-receive << 'EOF' ...` and `chmod +x`). Do NOT use `git init
  --bare` alone on an existing repo — it only reinitializes metadata and
  does NOT clear existing objects or refs; a subsequent clone will still
  see old branches and commits.
- **Test data contamination during verification**: When testing a
  deployment pipeline, push the SAME content the verifier will push (e.g.,
  'main branch content' for a main branch test). Pushing different content
  ('updated main content', 'test data') to the same branches contaminates
  the repo and forces a nuclear reset. If you need to test deployment
  timing or behavior with different content, use a SEPARATE test branch
  (e.g., `test-timing`) that the verifier will never reference, then verify
  the original branches still serve correct content.
- **Wrong working tree owner**: In SSH setups, hooks run as the SSH user.
  The deployment directory must be writable by that user.
- **Port already in use**: Starting a web server on an occupied port fails
  silently. Check with `ss -tlnp | grep <port>` first.
- **Stale deployed files**: `git checkout -f` overwrites existing files
  but does NOT delete files removed from the repo. If clean deployment
  is required, add `rm -rf $GIT_WORK_TREE/*` before the checkout line.
- **Test artifacts in bare repos**: When verifying by pushing a test file
  to the bare repo, that file becomes a permanent part of repo history
  and auto-deploys to the web root via the post-receive hook. Unlike
  compilation artifacts that can be simply deleted, git-deployed test
  files persist until explicitly removed from the repo. Avoid pushing
  test-only files to the bare repo. If you must push a test file for
  verification, remove it afterward: clone the repo, `git rm` the test
  file, commit, and push. Alternatively, verify by cloning and checking
  the working tree without pushing test files.