import asyncio
import threading

import pytest

from gtbms.workers import finish_io


@pytest.mark.asyncio
async def test_cancellation_joins_owned_database_work():
    entered = threading.Event()
    release = threading.Event()
    written = []
    def operation():
        entered.set()
        assert release.wait(5)
        written.append('committed')
    task = asyncio.create_task(finish_io(operation))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(.02)
    assert not task.done()
    task.cancel()
    await asyncio.sleep(.02)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert written == ['committed']
