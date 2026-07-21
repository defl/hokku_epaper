#pragma once
#include "netif.h"

static int _mock_dhcp_stop_called;
static inline void dhcp_stop(struct netif *nif) { (void)nif; _mock_dhcp_stop_called = 1; }
