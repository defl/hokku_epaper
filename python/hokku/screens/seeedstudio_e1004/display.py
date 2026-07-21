"""Display specification for the Seeed reTerminal E1004 (T133A01 panel).

The E1004's wire format is byte-identical to the huessen_epf1301 frame: the same
6-colour E Ink Spectra palette, the same 960,000-byte buffer laid out as two
600×1600 halves (1200×1600 total), the same nibble packing and geometry. The
panels are driven by different controllers (T133A01 vs UC8179C) but the server
produces the same bytes, so this is a pure model-id override of the huessen
Display — the render/dither pipeline keys entirely off these class attributes.

(If the E1004's inks are ever re-measured on hardware and differ, override just
``palette_measured_rgb`` here.)
"""

from __future__ import annotations

from hokku.screens.huessen_epf1301.display import HuessenEpf1301Display


class SeeedstudioE1004Display(HuessenEpf1301Display):
    model_id = "seeedstudio_e1004"
