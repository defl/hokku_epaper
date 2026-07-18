"""Registry of all known e-paper screen display specifications.

``DISPLAY_REGISTRY`` is the single source of truth: map of ``model_id``
→ ``Display`` instance.  Adding a new screen model means adding one
entry here; nothing else in the pipeline changes.
"""

from __future__ import annotations

from hokku.screens.bigme_f7.display import BigmeF7Display
from hokku.screens.display import Display
from hokku.screens.huessen_epf1301.display import HuessenEpf1301Display
from hokku.screens.seeedstudio_e1004.display import SeeedstudioE1004Display

_DISPLAY_CLASSES: list[type[Display]] = [
    HuessenEpf1301Display,
    SeeedstudioE1004Display,
    BigmeF7Display,
]

DISPLAY_REGISTRY: dict[str, Display] = {cls.model_id: cls() for cls in _DISPLAY_CLASSES}
