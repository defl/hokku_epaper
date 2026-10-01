#pragma once
#include "lwip/netif.h"
#include "net/wlan/wlan.h"   /* shared call trace (_mock_wlan_record) */

/* Only the "clear the address" form (all NULL) is used by main.c. */
static inline int netifapi_netif_set_addr(struct netif *nif, const void *ip,
                                          const void *nm, const void *gw)
{
    (void)nif; (void)ip; (void)nm; (void)gw;
    _mock_wlan_record(MOCK_NETIF_CLEAR_ADDR);
    return 0;
}