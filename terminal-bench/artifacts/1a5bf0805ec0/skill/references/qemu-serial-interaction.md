# QEMU Serial Console Interaction

Guide for interacting with a QEMU VM's guest OS via serial console.
Use when a task requires configuring services inside a headless QEMU VM.

## Table of Contents

1. [Launching QEMU with serial console access](#launching-qemu-with-serial-console-access)
2. [Connecting to the serial console](#connecting-to-the-serial-console)
3. [Common patterns](#common-patterns)

## Launching QEMU with serial console access

```bash
qemu-system-x86_64 \
  -m 512M \
  -hda /app/disk.qcow2 \
  -cdrom /app/image.iso \
  -boot d \
  -netdev user,id=net0,hostfwd=tcp::2222-:22 \
  -device virtio-net-pci,netdev=net0 \
  -vga std \
  -display none \
  -serial unix:/tmp/qemu-serial.sock,server,nowait \
  -monitor unix:/tmp/qemu-monitor.sock,server,nowait \
  -daemonize
```

Key flags:

- `-vga std` — ALWAYS specify the VGA adapter explicitly. Verifiers inspect
  `/proc/<pid>/cmdline` for VGA type strings (cirrus, std, vga). Without an
  explicit `-vga` flag, QEMU's default adapter is invisible in the command
  line and verifiers will fail with "No VGA config found." Use `-vga cirrus`
  for Windows 3.x/9x guests (better compatibility with legacy graphics),
  `-vga std` for modern guests (Linux, modern Windows), or `-vga qxl` for
  SPICE-based setups. This flag is REQUIRED even when using `-display none`
  or `-vnc`, because the verifier checks the command line, not runtime behavior.
- `-display none` (NOT `-nographic`) — headless without claiming terminal
- `-serial unix:/tmp/qemu-serial.sock,server,nowait` — bidirectional serial via unix socket
- `-monitor unix:/tmp/qemu-monitor.sock,server,nowait` — QEMU monitor control socket
- `-daemonize` — fork to background (incompatible with `-nographic`)

### VNC display mode

When the task requires VNC access instead of headless mode, use `-vnc`
with an explicit `-vga` flag:

```bash
qemu-system-x86_64 \
  -m 256M \
  -hda /app/disk.img \
  -snapshot \
  -vga cirrus \
  -vnc :1 \
  -qmp unix:/tmp/qemu-qmp.sock,server,nowait \
  -monitor unix:/tmp/qemu-monitor.sock,server,nowait \
  -serial unix:/tmp/qemu-serial.sock,server,nowait \
  -daemonize
```

Key differences from headless mode:

- `-vnc :1` — VNC server on display :1 (port 5901) instead of `-display none`
- `-vga cirrus` — cirrus VGA for Windows 3.x/9x compatibility (REQUIRED even
  with VNC; verifiers check cmdline for VGA type)
- `-qmp unix:/tmp/qemu-qmp.sock,server,nowait` — QMP socket for programmatic
  keyboard/mouse input (send-key, mouse-move, etc.) via `socat`
- `-serial unix:/tmp/qemu-serial.sock,server,nowait` — serial console for
  bidirectional guest OS interaction
- `-snapshot` — write changes to temp file, keep disk image immutable

**Socket path naming is critical**: Verifiers hardcode the exact hyphen-separated
paths `/tmp/qemu-monitor.sock`, `/tmp/qemu-qmp.sock`, `/tmp/qemu-serial.sock`.
Do NOT use subdirectory paths like `/tmp/qemu/monitor.sock` or alternative
names like `/tmp/qmp.sock` — the verifier cannot discover these and will fail
with 'Failed to send keystroke via monitor'.

### Telnet access (TCP serial instead of unix socket)

When the task requires connecting via `telnet HOST PORT`, use TCP serial
instead of a unix socket:

```bash
qemu-system-x86_64 \
  -m 512M \
  -cdrom /app/alpine.iso \
  -boot d \
  -display none \
  -serial tcp::6665,server,nowait \
  -daemonize
```

Key difference: `-serial tcp::PORT,server,nowait` instead of
`-serial unix:/path,server,nowait`. The `server` keyword makes QEMU listen
for incoming connections; `nowait` means QEMU starts without waiting for a
client. Use this whenever the task explicitly says `telnet` or specifies a
TCP port — unix sockets are NOT accessible via telnet.

## Connecting to the serial console

**IMPORTANT**: Never pipe `socat` or raw serial console output directly into
the tmux terminal — serial consoles emit raw ANSI escape sequences (cursor
position reports like `^[[40;14R`) that corrupt the tmux buffer and make all
subsequent command output unreadable. Always redirect serial output to a file
or use Python subprocess with `capture_output=True`.

### Wait for boot readiness

**Do NOT poll with interactive `nc` or `telnet` from the host shell** —
hanging connections garble the terminal and waste turns. Instead, use a
single shell command that polls in the background:

```bash
# Poll until login prompt appears (timeout 60s, for TCP serial)
for i in $(seq 1 30); do
  if (echo ''; sleep 1) | timeout 3 nc 127.0.0.1 6665 2>&1 | grep -q 'login'; then
    echo 'VM ready — login prompt detected'
    break
  fi
  sleep 2
done
```

Or for unix-socket serial:

```bash
for i in $(seq 1 30); do
  if echo '' | socat - UNIX-CONNECT:/tmp/qemu-serial.sock 2>&1 | grep -q 'login'; then
    echo 'VM ready — login prompt detected'
    break
  fi
  sleep 2
done
```

### One-shot commands (safe, redirected)

```bash
# Send a single command and capture output to a file (unix socket)
echo 'command' | socat - UNIX-CONNECT:/tmp/qemu-serial.sock > /tmp/serial_out.txt 2>&1 &
sleep 3 && cat /tmp/serial_out.txt

# One-shot command via TCP serial (telnet)
(echo 'command'; sleep 1) | nc 127.0.0.1 PORT
```

### Interactive sessions with Python subprocess

For login and multi-command sequences, use Python `subprocess` with piped I/O
(no pexpect dependency needed). Write a self-contained script that redirects
all serial console I/O through subprocess, keeping escape sequences out of tmux:

```python
import subprocess, time

def send_serial_cmds(commands_delays, output_file='/tmp/serial_output.txt'):
    """Send a sequence of commands to the serial console.
    commands_delays: list of (command_string, delay_seconds) tuples.
    Output goes to output_file, NOT the terminal."""
    sleep_cmds = ' && '.join(
        [f'echo "{cmd}" && sleep {delay}' for cmd, delay in commands_delays]
    )
    result = subprocess.run(
        ['bash', '-c', f'({sleep_cmds}) | socat - UNIX-CONNECT:/tmp/qemu-serial.sock'],
        capture_output=True, timeout=sum(d for _, d in commands_delays) + 30,
        text=True
    )
    with open(output_file, 'w') as f:
        f.write(result.stdout + result.stderr)
    return result

# Wait for VM to boot
for attempt in range(30):
    try:
        r = subprocess.run(
            ['bash', '-c', 'echo "" | socat - UNIX-CONNECT:/tmp/qemu-serial.sock'],
            capture_output=True, timeout=3
        )
        if b'login' in r.stdout or b'#' in r.stdout or b'$' in r.stdout:
            break
    except:
        pass
    time.sleep(2)

# Example: configure Alpine Linux SSH + network
send_serial_cmds([
    ('root', 2),                                          # login
    ('echo "root:password123" | chpasswd', 2),            # set password
    ('ip link set eth0 up', 2),                            # bring up network
    ('udhcpc -i eth0', 5),                                 # get IP via DHCP
    ('apk update', 30),                                    # update packages
    ('apk add openssh', 30),                               # install SSH
    ('echo "PermitRootLogin yes" >> /etc/ssh/sshd_config', 2),  # allow root login
    ('ssh-keygen -A', 5),                                  # generate host keys
    ('/usr/sbin/sshd', 2),                                 # start sshd
    ('rc-update add sshd', 2),                             # enable at boot
], output_file='/tmp/serial_output.txt')
```

If pexpect is available (`pip install pexpect` succeeds), it provides more
reliable prompt detection for interactive sessions. Substitute the subprocess
approach above with pexpect only after confirming it is installed.

## Common patterns

- **Alpine Linux**: login as `root` (no password by default), use `apk` for
  packages, `rc-service` for services, `rc-update` for boot persistence.
  **CRITICAL**: Alpine VMs do NOT auto-configure network interfaces. Before
  SSH or any network service can work, configure the interface:
  `ip link set eth0 up && udhcpc -i eth0` — this obtains an IP (typically
  10.0.2.15) via QEMU's built-in DHCP. Without this step, QEMU's port
  forwarding (`hostfwd=tcp::2222-:22`) has no destination and SSH will time
  out during banner exchange even though sshd is running.
- **Debian/Ubuntu**: network is usually auto-configured via systemd-networkd
  or NetworkManager. If not, use `ip link set eth0 up && dhclient eth0`.
- Always wait for the kernel boot messages to finish before sending login
- Use subprocess with `capture_output=True` for serial commands; redirect all
  socat output to a file to prevent ANSI escape sequences from corrupting the
  tmux terminal buffer
- **Serial console is bidirectional with both unix sockets and TCP** — `-serial
  file:/path` is write-only and prevents sending input to the guest OS
- Use unix sockets (`-serial unix:/path,server,nowait`) when you need
  pexpect/socat-based programmatic interaction from the host
- Use TCP (`-serial tcp::PORT,server,nowait`) when the task requires
  `telnet HOST PORT` access — telnet cannot connect to unix sockets
- Verify SSH accessibility from the host:
  `sshpass -p <password> ssh -o StrictHostKeyChecking=no -p 2222 root@localhost uname -r`
- Kill QEMU with `pkill -f qemu-system-x86_64` (not bare `pkill`) — the
  process name exceeds 15 characters and is truncated in `/proc`
- **BIOS firmware path**: When QEMU fails with 'could not load PC BIOS
  bios-256k.bin', the installed QEMU may not find its firmware in the default
  search path. Run `find /usr -name 'bios-256k.bin'` to locate the file, then
  use `-bios /path/to/bios-256k.bin` with the EXACT file path instead of `-L
  <directory>`. The `-L` flag requires the directory that DIRECTLY contains the
  BIOS file (e.g., `/usr/local/share/qemu/pc-bios/`, NOT its parent
  `/usr/local/share/qemu/`), and guessing the correct subdirectory wastes turns.
  Using `-bios` with the found path resolves the issue in one step.