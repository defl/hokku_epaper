from PIL import Image
from pathlib import Path
import pillow_jxl

# Create a simple test image and save as JXL
test_img_path = Path("images/test/test_jxl.jxl")
img = Image.new("RGB", (800, 600), color="red")
img.save(test_img_path, "JXL")
print(f"Created test JXL image: {test_img_path.name}")
print(f"File size: {test_img_path.stat().st_size} bytes")
