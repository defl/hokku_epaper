#include "backoff.h"

int hokku_backoff_seconds(unsigned prior_failures, int base_seconds, int max_seconds)
{
    if (base_seconds < 1) base_seconds = 1;
    if (max_seconds < base_seconds) max_seconds = base_seconds;

    /* Double base once per prior failure, in a wider type so a large
     * prior_failures can't overflow int; stop as soon as we reach the cap. */
    long secs = base_seconds;
    for (unsigned i = 0; i < prior_failures && secs < max_seconds; i++)
        secs *= 2;
    if (secs > max_seconds) secs = max_seconds;
    return (int)secs;
}
