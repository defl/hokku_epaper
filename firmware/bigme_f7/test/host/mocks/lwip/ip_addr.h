#pragma once
#include <string.h>

/* Simplified stand-in for lwIP's dual-stack ip_addr_t union: just enough to
 * exercise net_cb's static-IP parsing/branching without real IP math. */
typedef struct {
    char text[16];
    int  valid;
} ip_addr_t;

static inline int ipaddr_aton(const char *s, ip_addr_t *out)
{
    if (!s || !*s) { out->valid = 0; return 0; }
    strncpy(out->text, s, sizeof(out->text) - 1);
    out->text[sizeof(out->text) - 1] = '\0';
    out->valid = 1;
    return 1;
}
static inline const char *ipaddr_ntoa(const ip_addr_t *a) { return a->text; }
/* No real dual-stack union in this mock, so extracting the v4 view is a no-op. */
static inline ip_addr_t *ip_2_ip4(ip_addr_t *a) { return a; }
