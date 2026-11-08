#include "terminal_model.h"
#include <assert.h>
#include <string.h>
static void test_input_lifecycle(void) {
    pp_input_t s = {0};
    assert(pp_input_start(&s, 100));
    uint32_t generation = s.generation;
    assert(!pp_input_start(&s, 101));
    assert(pp_input_capturing(&s));
    assert(!pp_input_listening(&s)); /* Prebuffer is invisible to short/double clicks. */
    assert(pp_input_begin(&s));
    assert(pp_input_listening(&s));
    pp_input_release(&s); /* release must work while provider opens */
    assert(!pp_input_listening(&s));
    assert(!pp_input_capturing(&s));
    assert(s.phase == PP_INPUT_OPENING);
    assert(!pp_input_ready(&s, generation - 1, 1920000));
    assert(pp_input_ready(&s, generation, 1920000));
    assert(s.phase == PP_INPUT_DRAINING);
    assert(pp_input_drained(&s));
    assert(pp_input_transcript(&s, generation));
    assert(s.phase == PP_INPUT_SUBMITTING);
    assert(!pp_input_start(&s, 110));
    assert(!pp_input_done(&s, generation + 1));
    assert(pp_input_done(&s, generation));

    assert(pp_input_start(&s, UINT32_MAX - 999));
    generation = s.generation;
    assert(pp_input_begin(&s));
    assert(pp_input_ready(&s, generation, 1920000));
    assert(!pp_input_limit(&s, 98, 2000));
    assert(pp_input_limit(&s, 1000, 2000));
    assert(!pp_input_capturing(&s));
    assert(s.phase == PP_INPUT_DRAINING && s.limited);
    assert(pp_input_drained(&s));
    assert(pp_input_transcript(&s, generation));
    assert(s.phase == PP_INPUT_CONFIRM);
    assert(!pp_input_done(&s, generation)); /* never auto-submit over-limit text */
    assert(pp_input_confirm(&s));
    assert(!pp_input_confirm(&s));
    assert(pp_input_done(&s, generation));

    /* Recovery stops capture and never submits or revives an old ready callback. */
    s.phase = PP_INPUT_UNKNOWN;
    pp_input_restore_confirmation(&s);
    generation = s.generation;
    assert(s.phase == PP_INPUT_CONFIRM && !pp_input_capturing(&s));
    assert(!pp_input_ready(&s, generation - 1, 1920000));
    assert(!pp_input_ready(&s, generation, 1920000));
    assert(!pp_input_done(&s, generation));
    assert(pp_input_confirm(&s));
    assert(pp_input_done(&s, generation));

    assert(pp_input_start(&s, 2000));
    generation = s.generation;
    pp_input_release(&s); /* short press: no ASR */
    assert(s.phase == PP_INPUT_IDLE);
    assert(!pp_input_ready(&s, generation, 1920000));
    assert(!pp_input_transcript(&s, generation));
    assert(pp_input_start(&s, 3000));
    assert(pp_input_begin(&s));
    generation = s.generation;
    pp_input_abort(&s);
    assert(pp_input_start(&s, 4000));
    assert(pp_input_begin(&s));
    assert(!pp_input_ready(&s, generation, 1920000)); /* cancelled callback cannot revive input */
}
static void test_audio_limit(void) {
    pp_input_t s = {0};
    for (unsigned attempt = 0; attempt < 3; attempt++) {
        assert(pp_input_start(&s, attempt * 70000));
        unsigned generation = s.generation;
        assert(pp_input_begin(&s));
        assert(!pp_input_ready(&s, generation, 0));
        assert(!pp_input_ready(&s, generation, 1920001));
        assert(pp_input_ready(&s, generation, 1920000));
        for (unsigned i = 0; i < 3000; i++) {
            assert(pp_input_audio(&s, 640));
            if (i < 2999)
                assert(s.phase == PP_INPUT_RECORDING);
        }
        /* A faster microphone reaches its sample limit before the wall clock.
         * Even a queued 3001st frame must never enter transport. */
        assert(s.audio_bytes == 1920000 && s.limited);
        assert(!pp_input_capturing(&s) && s.phase == PP_INPUT_DRAINING);
        assert(!pp_input_audio(&s, 640));
        assert(pp_input_drained(&s));
        assert(pp_input_transcript(&s, generation) && s.phase == PP_INPUT_CONFIRM);
        if (attempt == 0)
            pp_input_abort(&s);
        else {
            assert(pp_input_confirm(&s));
            assert(pp_input_done(&s, generation));
        }
    }
    assert(pp_input_start(&s, 250000));
    assert(pp_input_begin(&s));
    assert(pp_input_ready(&s, s.generation, 32000));
    assert(!pp_input_audio(&s, 0) && !pp_input_audio(&s, 3));
    assert(!pp_input_limit(&s, 250999, 60000));
    assert(pp_input_limit(&s, 251000, 60000));
    assert(pp_input_audio(&s, 640)); /* drain the allowed tail after wall stop */
    pp_input_abort(&s);
    assert(!pp_input_audio(&s, 640));
}
int main(void) {
    test_input_lifecycle();
    test_audio_limit();
    pp_button_t b = {0};
    assert(pp_press(&b, 0, false) == PP_CAPTURE);
    assert(pp_tick(&b, 199) == PP_NONE);
    assert(pp_tick(&b, 200) == PP_BEGIN);
    assert(pp_tick(&b, 201) == PP_NONE);
    assert(pp_release(&b, 1000) == PP_FINISH);
    assert(pp_tick(&b, 1400) == PP_NONE);
    assert(pp_press(&b, 2000, false) == PP_CAPTURE);
    assert(pp_release(&b, 2100) == PP_DISCARD);
    assert(pp_tick(&b, 2300) == PP_NONE);
    assert(pp_press(&b, 2350, false) == PP_CAPTURE);
    assert(pp_release(&b, 2400) == (PP_DISCARD | PP_TOGGLE));
    assert(pp_tick(&b, 3000) == PP_NONE);
    assert(pp_press(&b, 4000, true) == (PP_CAPTURE | PP_STOP));
    assert(pp_release(&b, 4100) == PP_DISCARD);
    assert(pp_tick(&b, 4500) == PP_NONE);
    b = (pp_button_t){.wake_guard = true};
    assert(pp_press(&b, 0, false) == PP_NONE);
    assert(pp_release(&b, 1) == PP_NONE);
    assert(pp_press(&b, 10, false) == PP_CAPTURE);
    assert(pp_release(&b, 11) == PP_DISCARD);
    assert(pp_tick(&b, 312) == PP_MENU);
    b = (pp_button_t){0};
    pp_press(&b, UINT32_MAX - 100, false);
    assert(pp_tick(&b, 100) == PP_BEGIN);
    char out[8];
    assert(pp_text(out, 7, "中文X") == 6);
    assert(!strcmp(out, "中文"));
    assert(pp_text(out, 8, "中\xff文") == 7);
    assert(!strcmp(out, "中?文"));
    assert(pp_text(out, 2, "\xf0\x9f") == 1);
    assert(!strcmp(out, "?"));
    return 0;
}
