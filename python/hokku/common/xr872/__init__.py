"""XRADIOTECH XR872 mask-BROM flasher — pure-Python primitives that catch the
BROM and safely write slot 0 on a Bigme F7 (XR872AT).

Moved here from the dev-tree ``tools/`` so they ship in the hokku-server .deb;
the web "Flash a screen" F7 path (``hokku.screens.bigme_f7.bootstrap``) and the
CLI tools in ``tools/`` both import them. Import the submodules directly
(``flasher``, ``slot0``, ``catch``) so ``serial`` stays a lazy dependency of the
caller rather than being pulled in at package import.

Safety (slot 0 + its A/B cfg sector only, bootloader and OEM slot 1 never
touched, cfg flip written last, verify-or-abort) lives in
:func:`hokku.common.xr872.slot0.flash_slot0` and is unchanged by the move.
"""
