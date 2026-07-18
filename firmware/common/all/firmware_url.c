#include "firmware_url.h"

#include <string.h>
#include <stdio.h>

void firmware_url_build(char *out, size_t outsz, const char *base_url, const char *leaf)
{
    if (outsz == 0) return;

    /* Anchor on "/hokku/": keep the base through that segment and append leaf,
     * e.g. "http://host/hokku/screen/" + "firmware.bin" ->
     *      "http://host/hokku/firmware.bin". This is robust to the path depth
     * after /hokku/ (the screen endpoint may or may not have a trailing
     * segment). If the URL doesn't contain /hokku/ at all, fall back to copying
     * the base verbatim rather than guessing. */
    const char *hk = strstr(base_url, "/hokku/");
    if (hk) {
        size_t prefix = (size_t)(hk - base_url) + 7;   /* through "/hokku/" */
        if (prefix < outsz) {
            memcpy(out, base_url, prefix);
            out[prefix] = '\0';
            strncat(out, leaf, outsz - strlen(out) - 1);
            return;
        }
    }
    snprintf(out, outsz, "%s", base_url);              /* unrecognised URL shape */
}
