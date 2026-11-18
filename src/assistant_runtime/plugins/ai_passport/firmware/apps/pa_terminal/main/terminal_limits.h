#pragma once

/* Protocol 2 limits shared by the transport, audio adapter and controller.
 * Audio uses signed 16-bit mono samples. Credit is counted in PCM bytes. */
#define PP_WIRE_MESSAGE_BYTES 16384u
#define PP_CAPTURE_RATE 16000u
#define PP_PLAYBACK_RATE 24000u
#define PP_PLAYBACK_WINDOW 8192u
#define PP_OK_DOUBLE_WINDOW_MS 300u
#define PP_TERMINAL_READ_TIMEOUT_MS 15000u
