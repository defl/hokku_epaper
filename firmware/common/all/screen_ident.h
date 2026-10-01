/*
 * Screen identity: the MAC string every firmware sends as X-Screen-Mac, and the
 * rule for the name the server sends back in the response's X-Screen-Name.
 *
 * The server keys each screen by its MAC and owns the name of every screen it
 * knows: a new screen is registered under the name it reports, and from then on
 * the server's name (changed in the web UI) is sent back on every response. The
 * screen saves it when it differs from its own. Pure C (see README.md): the
 * caller reads the MAC bytes from its own SDK and persists the name to its own
 * config store.
 */
#ifndef HOKKU_SCREEN_IDENT_H
#define HOKKU_SCREEN_IDENT_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* "aa:bb:cc:dd:ee:ff" + NUL */
#define HOKKU_MAC_STR_LEN        18
/* Longest name a rename may carry, in bytes. Fits every board's name buffer
 * (the F7's is 64 including the NUL). Mirrored by the server's validator. */
#define HOKKU_SCREEN_NAME_MAX    63

/* Characters a name may use besides ASCII letters and digits. */
#define HOKKU_SCREEN_NAME_PUNCT  " -_.'()"

/* Response header carrying the server's name for this screen. */
#define HOKKU_HDR_SCREEN_NAME    "X-Screen-Name"

/* Format mac as lowercase "aa:bb:cc:dd:ee:ff" into out (needs >= 18 bytes).
 * An all-zero MAC means "unknown" and yields "". */
void hokku_mac_format(const uint8_t mac[6], char *out, size_t len);

/* Whether name is acceptable as a screen name: 1..HOKKU_SCREEN_NAME_MAX bytes of
 * ASCII letters, digits and HOKKU_SCREEN_NAME_PUNCT, with no leading or trailing
 * space. It travels in HTTP headers, in the server's per-screen URLs and into
 * the web UI, hence the narrow alphabet. */
bool hokku_screen_name_valid(const char *name);

#endif /* HOKKU_SCREEN_IDENT_H */
