#pragma once

#include <stdbool.h>

bool pp_board_keys_released(void);

/* Terminal handoff for PA and the hardware lab. Quiesce application workers
 * and finish durable writes before calling. Applications may arm an extra
 * timer wake source. The board always arms the shared key pad for GPIO wake.
 * Failure after handoff logs the failed stage and reboots. */
_Noreturn void pp_board_enter_deep_sleep(void);
