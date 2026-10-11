"""Helpers for owned blocking work that must finish before database shutdown."""
import asyncio


async def finish_io(function, *args):
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Repeated cancellation still cannot stop the thread. Keep joining
            # before allowing a monitor restart or SQLite shutdown.
            cancelled = True
        except Exception:
            if not cancelled:
                raise
    if cancelled:
        try:
            task.result()
        finally:
            raise asyncio.CancelledError
    return task.result()
