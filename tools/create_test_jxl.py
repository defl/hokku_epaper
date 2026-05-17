from pathlib import Path
from PIL import Image
import pillow_jxl

_ROOT = Path(__file__).resolve().parent.parent
_OUT = _ROOT / "images" / "test"
_OUT.mkdir(parents=True, exist_ok=True)

test_img_path = _OUT / "test_jxl.jxl"
img = Image.new("RGB", (800, 600), color="red")
img.save(test_img_path, "JXL")
print(f"Created test JXL image: {test_img_path}")
print(f"File size: {test_img_path.stat().st_size} bytes")
