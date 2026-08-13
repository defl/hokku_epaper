#include "interactive.h"

/* Plain RAM, at file scope. Both properties matter:
 *
 * Plain RAM (not NVS, not RTC-retained) is what makes "any reset clears it"
 * true, on every SoC, without each board having to remember to clear it.
 *
 * File scope because the host tests compile firmware sources with `static`
 * #defined away — a function-local `static` would silently become an
 * uninitialised local there. At file scope the variable keeps static storage
 * duration and zero-initialisation with or without the keyword. */
static bool s_requested;

void hokku_interactive_set(bool on)
{
    s_requested = on;
}

bool hokku_interactive_requested(void)
{
    return s_requested;
}

bool hokku_interactive_engaged(bool usb_present)
{
    return s_requested && usb_present;
}
