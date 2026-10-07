#include "screen_ident.h"

#include <stdio.h>
#include <string.h>

void hokku_mac_format(const uint8_t mac[6], char *out, size_t len)
{
    if (!out || len == 0) return;
    out[0] = '\0';
    if (!mac || len < HOKKU_MAC_STR_LEN) return;
    uint8_t any = 0;
    for (int i = 0; i < 6; i++) any |= mac[i];
    if (!any) return;
    snprintf(out, len, "%02x:%02x:%02x:%02x:%02x:%02x",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

bool hokku_screen_name_valid(const char *name)
{
    if (!name) return false;
    size_t n = strlen(name);
    if (n == 0 || n > HOKKU_SCREEN_NAME_MAX) return false;
    if (name[0] == ' ' || name[n - 1] == ' ') return false;
    for (size_t i = 0; i < n; i++) {
        char c = name[i];
        bool ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                  (c >= '0' && c <= '9') || (c != '\0' && strchr(HOKKU_SCREEN_NAME_PUNCT, c));
        if (!ok) return false;
    }
    return true;
}
