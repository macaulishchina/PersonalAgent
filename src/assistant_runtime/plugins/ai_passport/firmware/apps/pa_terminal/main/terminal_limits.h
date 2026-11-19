#pragma once

/* Protocol 2 limits shared by the transport, audio adapter and controller.
 * Audio uses signed 16-bit mono samples. Credit is counted in PCM bytes. */
#define PP_WIRE_MESSAGE_BYTES 16384u
#define PP_CAPTURE_RATE 16000u
#define PP_CAPTURE_BYTES (PP_CAPTURE_RATE * 2u / 50u)
/* Pack up to 40 ms in one WS fragment and TLS record. */
#define PP_INPUT_BATCH_BYTES (PP_CAPTURE_BYTES * 2u)
#define PP_WS_IO_BYTES 1536u
#define PP_INPUT_EARLY_BYTES (PP_CAPTURE_RATE * 2u)
#define PP_INPUT_AUDIO_ID_BYTES 16u
#define PP_INPUT_TAGGED_HEADER_BYTES (8u + PP_INPUT_AUDIO_ID_BYTES)
#define PP_PLAYBACK_RATE 24000u
#define PP_PLAYBACK_WINDOW 8192u
#define PP_OK_DOUBLE_WINDOW_MS 300u
#define PP_TERMINAL_READ_TIMEOUT_MS 15000u
