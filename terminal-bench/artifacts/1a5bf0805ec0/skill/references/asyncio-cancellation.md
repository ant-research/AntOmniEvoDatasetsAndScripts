# Asyncio Cancellation and Signal Handling

## The Core Problem

`except CancelledError` handlers only fire when SIGINT is converted to CancelledError by `asyncio.run()`. If the caller uses `loop.run_until_complete()` or a custom event loop, SIGINT raises `KeyboardInterrupt` instead of CancelledError, bypassing the handler entirely — resulting in 0 cleanups.

Even when `asyncio.run()` IS used, the uncancel+await cleanup pattern is fragile: if `asyncio.gather()` also cancelled child tasks before the handler runs, child task cancel counts may be >1, causing `await` in `async with semaphore.__aexit__` to re-raise CancelledError, which can prevent proper cleanup.

## The Robust Pattern: loop.add_signal_handler

Install a custom SIGINT handler on the event loop that cancels child tasks BEFORE CancelledError or KeyboardInterrupt is raised. This works with ANY calling convention.

```python
import asyncio
import signal
import os
from typing import Callable, Awaitable

async def run_tasks(tasks: list[Callable[[], Awaitable[None]]], max_concurrent: int) -> None:
    semaphore = asyncio.Semaphore(max_concurrent)

    async def run_one(task: Callable[[], Awaitable[None]]) -> None:
        async with semaphore:
            await task()

    # 1. Create EXPLICIT Task objects (not bare coroutines)
    async_tasks = [asyncio.create_task(run_one(t)) for t in tasks]
    if not async_tasks:
        return

    loop = asyncio.get_running_loop()
    sigint_count = 0

    def _on_sigint():
        nonlocal sigint_count
        sigint_count += 1
        if sigint_count == 1:
            # First SIGINT: cancel all child tasks
            for t in async_tasks:
                if not t.done():
                    t.cancel()
        else:
            # Second SIGINT: exit immediately
            try:
                loop.remove_signal_handler(signal.SIGINT)
            except Exception:
                pass
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            os.kill(os.getpid(), signal.SIGINT)

    # 2. Install custom SIGINT handler
    try:
        loop.add_signal_handler(signal.SIGINT, _on_sigint)
    except (RuntimeError, OSError):
        pass

    try:
        # 3. gather with return_exceptions collects CancelledError results
        await asyncio.gather(*async_tasks, return_exceptions=True)
    finally:
        # 4. Always remove our handler
        try:
            loop.remove_signal_handler(signal.SIGINT)
        except Exception:
            pass

    # 5. Propagate results
    cancelled = False
    for t in async_tasks:
        if t.cancelled():
            cancelled = True
        elif t.exception() is not None:
            raise t.exception()
    if cancelled:
        raise asyncio.CancelledError()
```

## Why Each Step Matters

1. **Explicit `create_task()`**: Gives you references to cancel and await children. Passing bare coroutines to `gather` creates internal tasks you can't reference.

2. **`loop.add_signal_handler(signal.SIGINT, handler)`**: Called from the event loop thread when SIGINT arrives. Intercepts the signal BEFORE it becomes CancelledError (asyncio.run) or KeyboardInterrupt (loop.run_until_complete). Unlike `except CancelledError`, this works regardless of how the caller invokes the event loop.

3. **`gather(return_exceptions=True)`**: The main task is NOT cancelled by our handler (only children are). So `await gather(...)` continues normally. `return_exceptions=True` collects CancelledError results from children instead of raising the first one, which would prevent waiting for other children's cleanup.

4. **Remove handler in finally**: Prevents our custom handler from outliving run_tasks. If run_tasks completes normally, the original SIGINT behavior is restored.

5. **Propagate after gather**: After all children finish cleanup, check results. If any was cancelled, raise CancelledError so the caller sees the interruption. If any had a non-CancelledError exception, raise that instead.

6. **Second SIGINT**: Restores default signal handler and re-sends SIGINT for immediate exit, matching the expected behavior of `Ctrl-C` twice.

## The CancelledError+uncancel Pattern (Fallback)

If you CANNOT use `loop.add_signal_handler()` (e.g., in a library that doesn't own the event loop), the uncancel pattern handles the case where asyncio.run() IS the caller:

```python
try:
    await asyncio.gather(*async_tasks)
except asyncio.CancelledError:
    current = asyncio.current_task()
    if current is not None and current.cancelling() > 0:
        current.uncancel()
    for t in async_tasks:
        if not t.done():
            t.cancel()
    await asyncio.gather(*async_tasks, return_exceptions=True)
    raise asyncio.CancelledError()
```

**Limitation**: Only works when CancelledError IS raised (caller uses asyncio.run()). If SIGINT raises KeyboardInterrupt instead, the except block is never entered and children get no cleanup.

## Testing Pattern

SIGINT tests MUST:
- Use `subprocess.Popen` + `proc.send_signal(signal.SIGINT)` — NOT `task.cancel()`
- Test **ALL THREE cancellation scenarios separately** — each exercises different code paths and the verifier tests each independently:
  - Below max_concurrent: n=2, max_concurrent=3 (no tasks wait on semaphore)
  - At max_concurrent: n=2, max_concurrent=2 (all slots filled, none queued)
  - Above max_concurrent: n=3, max_concurrent=2 (some tasks queued behind semaphore)
  - **Do NOT test only the above scenario** — a bug that only affects the 'below' or 'at' path will pass the above test but fail the verifier.
- Assert **exact counts for BOTH started and cleaned up**: `assert stdout.count('Task started.') == min(n_tasks, max_concurrent)` and `assert stdout.count('Cleaned up.') == min(n_tasks, max_concurrent)`. The 'Task started.' count confirms only max_concurrent tasks began execution; the 'Cleaned up.' count confirms their finally blocks ran. Omitting 'Task started.' can miss bugs where queued tasks bypass the semaphore.
- Set `PYTHONPATH` for the subprocess so it can import the module
- Use `flush=True` or `PYTHONUNBUFFERED=1` in the subprocess

Example test (above max_concurrent):
```python
import subprocess, signal, time, os
env = {**os.environ, 'PYTHONPATH': '/app'}
proc = subprocess.Popen(
    ['python3', '-u', '/tmp/test_sigint.py'],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
time.sleep(0.5)
proc.send_signal(signal.SIGINT)
stdout, _ = proc.communicate(timeout=5)
stdout = stdout.decode()
assert stdout.count('Task started.') == 2  # min(3, 2) = 2
assert stdout.count('Cleaned up.') == 2   # min(3, 2) = 2
```
Repeat the same pattern for below (n=2, max_concurrent=3, expected started=2, cleaned=2) and at (n=2, max_concurrent=2, expected started=2, cleaned=2). All three must pass independently.

## Common Mistakes

- **Relying on except CancelledError alone**: Only works with asyncio.run(). Use `loop.add_signal_handler()` for robust signal handling.
- **Passing coroutines to gather**: `gather(*[run_one(t) for t in tasks])` creates internal tasks you can't cancel. Use `create_task()` first.
- **Catching CancelledError and awaiting without uncancel**: `except CancelledError: await gather(...)` — the await immediately re-raises.
- **Testing only with `task.cancel()`**: Works for programmatic cancellation but not OS signals. Always test with subprocess + SIGINT.
- **Testing only one cancellation scenario**: Below, at, and above max_concurrent exercise different code paths. Test all three.
- **Asserting only cleanup counts**: Also assert 'Task started.' counts to confirm only max_concurrent tasks began execution.