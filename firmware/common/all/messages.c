#include "messages.h"

#include <stdio.h>

bool hokku_msg_fetch_error(char *buf, size_t len, const hokku_fetch_result_t *r,
                           const hokku_fetch_outcome_t *o, const char *url,
                           const char *retry_hint)
{
    if (o->action != HOKKU_FETCH_BACKOFF || !o->first_failure)
        return false;
    if (r && r->ota_failed)
        return false;

    if (r && r->wifi_failed)
        snprintf(buf, len,
                 "WiFi connect failed.\n"
                 "\n"
                 "Retrying (backing off).\n"
                 "%s", retry_hint);
    else
        snprintf(buf, len,
                 "Image download failed.\n"
                 "\n"
                 "Tried to connect to:\n"
                 "%s\n"
                 "\n"
                 "Retrying (backing off).\n"
                 "%s", url ? url : "", retry_hint);
    return true;
}

void hokku_msg_config_version(char *buf, size_t len, unsigned expected, unsigned found)
{
    snprintf(buf, len,
             "Config version\nmismatch.\n\n"
             "Expected: %u\nFound: %u\n\n"
             "Run hokku-setup to\nreconfigure.", expected, found);
}

void hokku_msg_ota_failed(char *buf, size_t len, const char *stage)
{
    snprintf(buf, len, "Firmware update\nfailed.\n\n(%s)\n\nWill retry later.", stage);
}
