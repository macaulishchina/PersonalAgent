"""Audio output providers shared by fixed voice and independent device operations."""

from .streams import AudioStreams


class AudioOutputs(AudioStreams):
    kind = "speaker"
    resource = "audio.output"
