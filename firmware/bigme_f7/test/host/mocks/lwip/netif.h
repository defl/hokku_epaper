#pragma once
#include "ip_addr.h"

struct netif {
    ip_addr_t ip_addr;
};

/* Test sets this directly (NULL = "no netif yet" path in net_cb). */
static struct netif *netif_list;

/* ── Recording state so tests can assert what net_cb actually did ──────── */
static int       _mock_netif_set_addr_called;
static ip_addr_t _mock_netif_set_addr_ip;
static ip_addr_t _mock_netif_set_addr_nm;
static ip_addr_t _mock_netif_set_addr_gw;
static int       _mock_netif_set_up_called;

static inline void netif_set_addr(struct netif *nif, ip_addr_t *ip, ip_addr_t *nm, ip_addr_t *gw)
{
    (void)nif;
    _mock_netif_set_addr_called = 1;
    _mock_netif_set_addr_ip = *ip;
    _mock_netif_set_addr_nm = *nm;
    _mock_netif_set_addr_gw = *gw;
}
static inline void netif_set_up(struct netif *nif) { (void)nif; _mock_netif_set_up_called = 1; }
