"""Public surface of the image-manager subsystem.

Re-exports the three principal classes plus the dataclasses and the one
helper (`_hash_name`) that tests import directly. The implementations live
in:

* ``image_manager_abstract`` — AbstractImageManager + dataclasses + helpers.
* ``image_manager_single``   — SingleThreadedImageManager.
* ``image_manager_multi``    — MultiThreadedImageManager.

Selection between concretes happens in ``app_state.build_manager``.
"""
from hokku_server.image_manager_abstract import (
    AbstractImageManager,
    ConversionProgress,
    ConvertStatus,
    ImageRecord,
    _hash_name,
    _try_read_image_dims,
)
from hokku_server.image_manager_multi import MultiThreadedImageManager
from hokku_server.image_manager_single import SingleThreadedImageManager

__all__ = [
    "AbstractImageManager",
    "ConversionProgress",
    "ConvertStatus",
    "ImageRecord",
    "MultiThreadedImageManager",
    "SingleThreadedImageManager",
    "_hash_name",
]
