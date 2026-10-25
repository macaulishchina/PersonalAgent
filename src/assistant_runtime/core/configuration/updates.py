"""Prepare all owners before committing; retain old settings on any failure."""

import asyncio
from assistant_runtime.api.configuration import ConfigurationUpdate


class UpdateCoordinator:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.failed_owners = set()
        self.cleanup_failures = set()
        self.failure = None
        self.authorized_tasks = set()

    async def finish(self, operation):
        task = asyncio.create_task(operation)
        self.authorized_tasks.add(task)
        try:
            while True:
                try:
                    return await asyncio.shield(task)
                except asyncio.CancelledError:
                    if task.done():
                        return task.result()
                    # Finish owned rollback/cleanup even if the caller cancels repeatedly.
                    continue
        finally:
            self.authorized_tasks.discard(task)

    async def apply(self, preparations):
        async with self.lock:
            self.cleanup_failures.clear()
            self.failure = None
            prepared, attempted = [], []
            committed = False
            try:
                for owner, prepare in preparations:
                    self.failure = (owner, "prepare")
                    change = await prepare()
                    if not isinstance(change, ConfigurationUpdate):
                        raise TypeError("Owner must return a prepared configuration update")
                    prepared.append((owner, change))
                for owner, change in prepared:
                    self.failure = (owner, "commit")
                    attempted.append((owner, change))
                    await change.commit()
                committed = True
                self.failure = None
                self.failed_owners.difference_update(owner for owner, _ in prepared)
            except BaseException:
                for owner, change in reversed(attempted):
                    try:
                        await self.finish(change.rollback())
                    except BaseException:
                        self.failed_owners.add(owner)
                raise
            finally:
                for owner, change in reversed(prepared):
                    if change.release:
                        try:
                            await self.finish(change.release(committed))
                        except BaseException:
                            (self.cleanup_failures if committed else self.failed_owners).add(owner)


def attributes(changes, *, ready=None):
    """An atomic event-loop settings swap for owners without resource replacement."""
    before = [(target, key, getattr(target, key)) for target, key, _ in changes]

    async def commit():
        if ready is not None and not ready():
            from assistant_runtime.api.configuration import ConfigurationPending

            raise ConfigurationPending()
        for target, key, value in changes:
            setattr(target, key, value)

    async def rollback():
        for target, key, value in reversed(before):
            setattr(target, key, value)

    return ConfigurationUpdate(commit, rollback)
