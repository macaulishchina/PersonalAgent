"""Local wake detection and voice activity only; Codex handles speech understanding."""

import sherpa_onnx


class Recognizer:
    def __init__(self, config):
        models = config.models
        kws = models / "kws/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
        # Keyword text is user-configurable; turn it into the model's phonetic tokens.
        from pypinyin import pinyin, Style

        text = config.wake_word
        parts = []
        for char in text:
            initial = pinyin(char, style=Style.INITIALS, strict=False)[0][0]
            final = pinyin(char, style=Style.FINALS_TONE, strict=False)[0][0]
            if initial:
                parts.append(initial)
            parts.append(final)
        keywords = config.state_dir / "keywords.txt"
        keywords.write_text(" ".join(parts) + " @" + text + "\n")
        self.spotter = sherpa_onnx.KeywordSpotter(
            tokens=str(kws / "tokens.txt"),
            encoder=str(kws / "encoder-epoch-13-avg-2-chunk-16-left-64.int8.onnx"),
            decoder=str(kws / "decoder-epoch-13-avg-2-chunk-16-left-64.onnx"),
            joiner=str(kws / "joiner-epoch-13-avg-2-chunk-16-left-64.int8.onnx"),
            keywords_file=str(keywords),
            num_threads=2,
        )
        self.keyword_stream = self.spotter.create_stream()
        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = str(models / "silero_vad.onnx")
        cfg.silero_vad.min_silence_duration = 0.55
        cfg.silero_vad.min_speech_duration = 0.2
        cfg.silero_vad.max_speech_duration = 30
        cfg.sample_rate = 16000
        self.vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=60)

    def keyword(self, samples):
        self.keyword_stream.accept_waveform(16000, samples)
        while self.spotter.is_ready(self.keyword_stream):
            self.spotter.decode_stream(self.keyword_stream)
            word = self.spotter.get_result(self.keyword_stream)
            if word:
                self.spotter.reset_stream(self.keyword_stream)
                return word
        return None

    def reset_keyword(self):
        # Do not carry a partial keyword from the previous conversation into standby.
        self.keyword_stream = self.spotter.create_stream()
