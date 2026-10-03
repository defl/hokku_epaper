// Unit tests for ota_confirm (pure): when a freshly installed image keeps
// itself and when it rolls back, the same on every board.
#include "test_harness.h"

#include "../ota_confirm.c"

static void test_step_table(void)
{
    CHECK(hokku_ota_confirm_step(false, HOKKU_FETCH_BACKOFF, 1) == HOKKU_OTA_NOT_PENDING,
          "step: a confirmed image has nothing to decide, even after an outage");
    CHECK(hokku_ota_confirm_step(true, HOKKU_FETCH_DISPLAY, 1) == HOKKU_OTA_CONFIRM,
          "step: an image from the server confirms");
    CHECK(hokku_ota_confirm_step(true, HOKKU_FETCH_KEEP, 3) == HOKKU_OTA_CONFIRM,
          "step: a keep reply confirms (label filter with no match must not roll back)");
    CHECK(hokku_ota_confirm_step(true, HOKKU_FETCH_BACKOFF, 1) == HOKKU_OTA_RETRY,
          "step: first miss retries");
    CHECK(hokku_ota_confirm_step(true, HOKKU_FETCH_BACKOFF, HOKKU_OTA_CONFIRM_ATTEMPTS - 1)
              == HOKKU_OTA_RETRY, "step: misses before the last retry");
    CHECK(hokku_ota_confirm_step(true, HOKKU_FETCH_BACKOFF, HOKKU_OTA_CONFIRM_ATTEMPTS)
              == HOKKU_OTA_ROLLBACK, "step: the last miss rolls back");
}

/* A scripted board. */
typedef struct {
    const hokku_fetch_action_t *script;
    unsigned fetches, waits, waited_s, confirms, rollbacks;
} fake_t;

static hokku_fetch_action_t fake_fetch(void *c) { fake_t *f = c; return f->script[f->fetches++]; }
static void fake_wait(void *c, unsigned s)    { fake_t *f = c; f->waits++; f->waited_s += s; }
static void fake_confirm(void *c)             { ((fake_t *)c)->confirms++; }
static void fake_rollback(void *c)            { ((fake_t *)c)->rollbacks++; }

static hokku_fetch_action_t run(fake_t *f, bool pending)
{
    hokku_ota_first_fetch_t ops = { .pending = pending, .fetch = fake_fetch,
                                    .wait_s = fake_wait, .confirm = fake_confirm,
                                    .rollback = fake_rollback, .ctx = f };
    return hokku_ota_first_fetch(&ops);
}

static void test_loop(void)
{
    static const hokku_fetch_action_t miss_then_keep[] = {
        HOKKU_FETCH_BACKOFF, HOKKU_FETCH_KEEP };
    fake_t f = { .script = miss_then_keep };
    CHECK(run(&f, true) == HOKKU_FETCH_KEEP && f.fetches == 2 && f.confirms == 1 &&
          f.rollbacks == 0 && f.waited_s == HOKKU_OTA_CONFIRM_RETRY_S,
          "loop: a transient miss is retried, then the image confirms");

    static const hokku_fetch_action_t always_down[] = {
        HOKKU_FETCH_BACKOFF, HOKKU_FETCH_BACKOFF, HOKKU_FETCH_BACKOFF, HOKKU_FETCH_BACKOFF };
    fake_t g = { .script = always_down };
    run(&g, true);
    CHECK(g.fetches == HOKKU_OTA_CONFIRM_ATTEMPTS && g.rollbacks == 1 && g.confirms == 0 &&
          g.waits == HOKKU_OTA_CONFIRM_ATTEMPTS - 1,
          "loop: never reaching the server rolls back after the last attempt");

    fake_t h = { .script = always_down };
    CHECK(run(&h, false) == HOKKU_FETCH_BACKOFF && h.fetches == 1 && h.waits == 0 &&
          h.confirms == 0 && h.rollbacks == 0,
          "loop: not pending -> exactly one fetch, no confirm, no rollback");

    static const hokku_fetch_action_t display[] = { HOKKU_FETCH_DISPLAY };
    fake_t k = { .script = display };
    run(&k, true);
    CHECK(k.fetches == 1 && k.confirms == 1 && k.waits == 0, "loop: first-try image confirms");
}

int main(void)
{
    test_step_table();
    test_loop();
    TEST_MAIN_END();
}
