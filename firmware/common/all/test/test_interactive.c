// Unit tests for USB-interactive mode (interactive.c). Pure C, no platform
// headers or mocks.
#include "test_harness.h"

#include "interactive.c"

int main(void)
{
    /* Off at startup, without anyone having to clear it. This is the property
     * that makes "any reset clears it" true: the flag is plain RAM, so a screen
     * always powers on ready to do its normal job. */
    CHECK(!hokku_interactive_requested(), "interactive: off at startup");
    CHECK(!hokku_interactive_engaged(true), "interactive: not engaged at startup, USB present");
    CHECK(!hokku_interactive_engaged(false), "interactive: not engaged at startup, on battery");

    hokku_interactive_set(true);
    CHECK(hokku_interactive_requested(), "interactive: request recorded");
    CHECK(hokku_interactive_engaged(true), "interactive: engaged when requested and on USB");

    /* The safety rule. A host can set the mode and then the cable can be pulled;
     * if that still suppressed sleep, the screen would sit awake until the
     * battery was flat. Requested-but-unplugged must behave exactly like a
     * normal screen. */
    CHECK(!hokku_interactive_engaged(false),
          "interactive: NOT engaged on battery, even when requested");

    /* Requested survives a USB dropout, so a brief glitch does not silently end
     * a measurement run — only the engaged decision follows USB. */
    CHECK(hokku_interactive_requested(), "interactive: request survives a USB dropout");
    CHECK(hokku_interactive_engaged(true), "interactive: re-engages when USB returns");

    hokku_interactive_set(false);
    CHECK(!hokku_interactive_requested(), "interactive: cleared on request");
    CHECK(!hokku_interactive_engaged(true), "interactive: not engaged once cleared");

    /* Idempotent: a host that re-asserts the mode before every upload (the
     * sensible thing to do, since a crash reboot silently clears it) must not
     * toggle anything by doing so. */
    hokku_interactive_set(true);
    hokku_interactive_set(true);
    CHECK(hokku_interactive_engaged(true), "interactive: re-asserting is idempotent");

    TEST_MAIN_END();
}
