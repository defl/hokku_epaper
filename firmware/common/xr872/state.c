#include "state.h"

#include <stddef.h>
#include <stdio.h>
#include <string.h>

#include "image/fdcm.h"
#include "frame_proto.h"   /* frame_proto_crc32: the shared CRC-32 */

/*
 * Flash area: 0x341000-0x343000, right after the config blob (0x340000) in the
 * free region above the OEM config partition (see hokku_config.c). Two 4 KB
 * erase blocks; nothing else writes here (the flashers touch only the boot
 * slots, the OTA cfg and the config sector).
 */
#define HOKKU_STATE_FLASH  0
#define HOKKU_STATE_ADDR   0x341000U
#define HOKKU_STATE_SIZE   0x2000U

/* The wear figures in state.h assume a 64-byte record. */
typedef char hokku_state_size_check[(sizeof(hokku_state_t) == 64) ? 1 : -1];

static hokku_state_t  g_state;
static hokku_state_t  g_saved;      /* what flash holds, to skip unchanged writes */
static fdcm_handle_t *g_fdcm;

static uint32_t state_crc(const hokku_state_t *s)
{
    return frame_proto_crc32(0, (const uint8_t *)s, (uint32_t)offsetof(hokku_state_t, crc));
}

static void state_zero(void)
{
    memset(&g_state, 0, sizeof(g_state));
    g_state.magic   = HOKKU_STATE_MAGIC;
    g_state.version = HOKKU_STATE_VERSION;
    hokku_sched_init(&g_state.sched);
}

void hokku_state_load(void)
{
    hokku_state_t rec;

    state_zero();
    memset(&g_saved, 0, sizeof(g_saved));   /* nothing known in flash: first save writes */

    g_fdcm = fdcm_open(HOKKU_STATE_FLASH, HOKKU_STATE_ADDR, HOKKU_STATE_SIZE);
    if (g_fdcm == NULL) {
        printf("hokku: state fdcm_open failed - starting from zero\n");
        return;
    }
    if (fdcm_read(g_fdcm, &rec, sizeof(rec)) != sizeof(rec) ||
        rec.magic != HOKKU_STATE_MAGIC || rec.version != HOKKU_STATE_VERSION ||
        rec.crc != state_crc(&rec)) {
        printf("hokku: no valid saved state - starting from zero\n");
        return;
    }
    g_state = rec;
    g_saved = rec;
}

hokku_state_t *hokku_state_get(void)
{
    return &g_state;
}

int hokku_state_save(void)
{
    g_state.magic   = HOKKU_STATE_MAGIC;
    g_state.version = HOKKU_STATE_VERSION;
    g_state.crc     = state_crc(&g_state);
    if (memcmp(&g_state, &g_saved, sizeof(g_state)) == 0)
        return 0;

    if (g_fdcm == NULL) {
        g_fdcm = fdcm_open(HOKKU_STATE_FLASH, HOKKU_STATE_ADDR, HOKKU_STATE_SIZE);
        if (g_fdcm == NULL)
            return -1;
    }
    if (fdcm_write(g_fdcm, &g_state, (uint16_t)sizeof(g_state)) != sizeof(g_state))
        return -1;
    g_saved = g_state;
    return 0;
}
