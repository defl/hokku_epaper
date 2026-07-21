#pragma once
#include <stdint.h>

enum net_ctrl_msg_type {
    NET_CTRL_MSG_WLAN_CONNECTED = 1,
    NET_CTRL_MSG_NETWORK_UP,
    NET_CTRL_MSG_NETWORK_DOWN,
    NET_CTRL_MSG_ALL,
};
#define CTRL_MSG_TYPE_NETWORK 1
/* Real EVENT_SUBTYPE extracts a bitfield from a packed event word; the mock
 * treats the "event" a test passes in as already being the subtype. */
#define EVENT_SUBTYPE(e) ((uint16_t)(e))

typedef struct observer_base { int _unused; } observer_base;
typedef void (*net_ctrl_cb)(uint32_t event, uint32_t data, void *arg);

static inline observer_base *sys_callback_observer_create(uint32_t type, uint32_t subtype,
                                                            net_ctrl_cb cb, void *arg)
{
    (void)type; (void)subtype; (void)cb; (void)arg;
    static observer_base ob;
    return &ob;
}
static inline int sys_ctrl_attach(observer_base *ob) { (void)ob; return 0; }
