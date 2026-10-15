"""Explicit audio input streams. PCM returned here is task data, never a voice turn."""

from .streams import AudioStreams


class AudioInputs(AudioStreams):
    kind = "microphone"
    resource = "audio.input"
