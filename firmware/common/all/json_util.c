#include "json_util.h"

void json_escape(char *dst, size_t dstlen, const char *src)
{
    if (dstlen == 0) return;
    size_t j = 0;
    for (size_t i = 0; src[i] != '\0'; i++) {
        unsigned char c = (unsigned char)src[i];
        if (c == '"' || c == '\\') {
            if (j + 2 >= dstlen) break;
            dst[j++] = '\\';
            dst[j++] = (char)c;
        } else if (c < 0x20) {
            continue;
        } else {
            if (j + 1 >= dstlen) break;
            dst[j++] = (char)c;
        }
    }
    dst[j] = '\0';
}
