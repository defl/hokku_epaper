from PIL import Image
import numpy as np

# Create a simple test image with a face-like pattern
img_array = np.ones((600, 400, 3), dtype=np.uint8) * 200

# Add some colored regions to simulate different colors
img_array[100:300, 50:200] = [255, 150, 100]  # Face color
img_array[150:180, 80:120] = [100, 50, 50]    # Eye region
img_array[200:220, 80:120] = [100, 50, 50]    # Other eye
img_array[280:320, 100:150] = [200, 100, 100] # Mouth

img = Image.fromarray(img_array)
img.save('test_server/images/test_face.jpg')
print("Created test image: test_face.jpg")

# Create another test image with landscape colors
img_array2 = np.zeros((600, 800, 3), dtype=np.uint8)
img_array2[0:300] = [100, 150, 200]  # Blue sky
img_array2[300:] = [50, 120, 50]     # Green field
img = Image.fromarray(img_array2)
img.save('test_server/images/test_landscape.jpg')
print("Created test image: test_landscape.jpg")

# Create a grayscale test image
img_array3 = np.random.randint(100, 150, (600, 600, 3), dtype=np.uint8)
img = Image.fromarray(img_array3)
img.save('test_server/images/test_bw.jpg')
print("Created test image: test_bw.jpg")
