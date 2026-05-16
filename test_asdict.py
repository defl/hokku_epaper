from dataclasses import dataclass, asdict
import json

@dataclass(frozen=True)
class BoundingBox:
    x: float
    y: float
    w: float
    h: float

@dataclass
class Observations:
    is_bw: bool = None
    face_bboxes: tuple = None

# Test 1: Create observation with BoundingBox
bbox = BoundingBox(x=0.1, y=0.2, w=0.3, h=0.4)
obs = Observations(is_bw=False, face_bboxes=(bbox,))

print("Original observation:", obs)

# Test 2: Try asdict()
obs_dict = asdict(obs)
print("\nasdict() result:", obs_dict)
print("face_bboxes type:", type(obs_dict['face_bboxes'][0]))
print("face_bboxes[0]:", obs_dict['face_bboxes'][0])

# Test 3: Try json.dumps()
try:
    json_str = json.dumps(obs_dict)
    print("\njson.dumps() succeeded!")
except TypeError as e:
    print(f"\njson.dumps() FAILED: {e}")

    # Test 4: Manual fix
    obs_dict_fixed = asdict(obs)
    if obs_dict_fixed.get('face_bboxes'):
        obs_dict_fixed['face_bboxes'] = [asdict(b) for b in obs.face_bboxes]
    print("\nAfter manual fix:")
    json_result = json.dumps(obs_dict_fixed)
    print("json.dumps() succeeded!")
    print("Result:", json_result)
