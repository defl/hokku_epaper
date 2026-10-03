#include "ota_confirm.h"

hokku_ota_step_t hokku_ota_confirm_step(bool pending, hokku_fetch_action_t action,
                                        unsigned attempt)
{
    if (!pending)
        return HOKKU_OTA_NOT_PENDING;
    if (action == HOKKU_FETCH_DISPLAY || action == HOKKU_FETCH_KEEP)
        return HOKKU_OTA_CONFIRM;
    if (attempt < HOKKU_OTA_CONFIRM_ATTEMPTS)
        return HOKKU_OTA_RETRY;
    return HOKKU_OTA_ROLLBACK;
}

hokku_fetch_action_t hokku_ota_first_fetch(const hokku_ota_first_fetch_t *ops)
{
    for (unsigned attempt = 1;; attempt++) {
        hokku_fetch_action_t a = ops->fetch(ops->ctx);
        switch (hokku_ota_confirm_step(ops->pending, a, attempt)) {
        case HOKKU_OTA_NOT_PENDING:
            return a;
        case HOKKU_OTA_CONFIRM:
            ops->confirm(ops->ctx);
            return a;
        case HOKKU_OTA_RETRY:
            ops->wait_s(ops->ctx, HOKKU_OTA_CONFIRM_RETRY_S);
            break;
        case HOKKU_OTA_ROLLBACK:
        default:
            ops->rollback(ops->ctx);
            return a;
        }
    }
}
