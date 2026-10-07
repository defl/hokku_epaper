// When a freshly installed firmware image keeps itself, and when it gives up and
// rolls back. One policy for every hokku screen. Pure C (see README.md); each
// board supplies the mechanics: how it fetches, waits, confirms and rolls back.
//
// The policy: an image installed by an OTA stays pending until the first fetch
// that reaches the server (fetch_outcome DISPLAY or KEEP: the server answered,
// so this image can join WiFi, talk HTTP and act on a reply). That fetch
// confirms it. A fetch that does not reach the server is retried in the same
// boot, HOKKU_OTA_CONFIRM_RETRY_S apart, up to HOKKU_OTA_CONFIRM_ATTEMPTS
// fetches in all; then the screen rolls back to the image it was running
// before. The decision always completes in the first boot of the new image,
// before any sleep: on every board a sleep ends in a reset, and a reset of an
// unconfirmed image already means rollback.
//
// What "pending", "confirm" and "roll back" are, per board:
//   ESP32  ESP-IDF app rollback: the bootloader marks a new image
//          PENDING_VERIFY; esp_ota_mark_app_valid_cancel_rollback() confirms,
//          esp_ota_mark_app_invalid_rollback_and_reboot() rolls back, and any
//          reset while pending rolls back too.
//   F7     see firmware/bigme_f7/main.c ("A/B try-boot rollback"): the boot cfg
//          keeps pointing at the previous slot until the image confirms; a
//          reset before that boots the previous slot.
// An image flashed over USB is not pending on either: the flasher selects it
// outright, and only a crash before the boot milestone rolls it back.
#pragma once

#include <stdbool.h>

#include "fetch_outcome.h"

#define HOKKU_OTA_CONFIRM_ATTEMPTS  3   /* fetches a new image gets to reach the server */
#define HOKKU_OTA_CONFIRM_RETRY_S   5   /* wait between them */

typedef enum {
    HOKKU_OTA_NOT_PENDING,   /* nothing to decide: carry on */
    HOKKU_OTA_CONFIRM,       /* the server was reached: keep this image */
    HOKKU_OTA_RETRY,         /* not reached yet: wait, fetch again */
    HOKKU_OTA_ROLLBACK,      /* out of attempts: go back to the previous image */
} hokku_ota_step_t;

/* The step after fetch number `attempt` (1-based) of a boot. */
hokku_ota_step_t hokku_ota_confirm_step(bool pending, hokku_fetch_action_t action,
                                        unsigned attempt);

/* A board's mechanics for the first fetch of a boot. */
typedef struct {
    bool  pending;                                /* this image awaits confirmation */
    hokku_fetch_action_t (*fetch)(void *ctx);     /* one complete fetch, acted on */
    void (*wait_s)(void *ctx, unsigned seconds);
    void (*confirm)(void *ctx);
    void (*rollback)(void *ctx);                  /* does not return on hardware */
    void *ctx;
} hokku_ota_first_fetch_t;

/* Run a boot's first fetch under the policy: one fetch when not pending;
 * otherwise fetch until the server is reached (then confirm) or the attempts
 * run out (then roll back). Returns the action of the last fetch. */
hokku_fetch_action_t hokku_ota_first_fetch(const hokku_ota_first_fetch_t *ops);
