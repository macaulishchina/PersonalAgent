"""Audio policy updates preserve the current implementation and stream ownership."""

from assistant_runtime.api.configuration import ConfigurationUpdate


def volume_update(current, candidate, providers):
    if {k: v for k, v in vars(current).items() if k != "minimum_volume"} != {
        k: v for k, v in vars(candidate).items() if k != "minimum_volume"
    }:
        return NotImplemented  # The host's owned replacement path handles connection changes.
    targets = [current, *providers]
    previous = [target.minimum_volume for target in targets]

    async def commit():
        for target in targets:
            target.minimum_volume = candidate.minimum_volume

    async def rollback():
        for target, value in zip(targets, previous):
            target.minimum_volume = value

    return ConfigurationUpdate(commit, rollback)
