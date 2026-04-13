"""Real-time vehicle detection on NYC DOT camera feeds using YOLOv8s ONNX.

Uses the 'small' variant (22M params, 43MB) for better nighttime detection
at the 352x240 resolution of DOT cameras. Images are preprocessed with
autocontrast + brightness/contrast/sharpness enhancement for low-light
conditions.
"""

import io
import time
from typing import Optional
import httpx
import numpy as np

_session = None
_input_name = None

# COCO vehicle class IDs
VEHICLE_CLASSES = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}
CONFIDENCE_THRESHOLD = 0.2  # Lower threshold for nighttime — small model is more precise


def _get_session():
    global _session, _input_name
    if _session is None:
        import onnxruntime as ort
        _session = ort.InferenceSession("models/yolov8s.onnx")
        _input_name = _session.get_inputs()[0].name
    return _session, _input_name


def _enhance_for_detection(img):
    """Enhance a low-light traffic camera image for better vehicle detection."""
    from PIL import ImageEnhance, ImageOps

    avg_brightness = np.array(img).mean()

    if avg_brightness < 100:
        # Nighttime: aggressive enhancement
        img = ImageOps.autocontrast(img, cutoff=2)
        img = ImageEnhance.Brightness(img).enhance(1.5)
        img = ImageEnhance.Contrast(img).enhance(1.4)
        img = ImageEnhance.Sharpness(img).enhance(1.2)
    elif avg_brightness < 140:
        # Dusk/dawn: moderate enhancement
        img = ImageOps.autocontrast(img, cutoff=1)
        img = ImageEnhance.Brightness(img).enhance(1.2)
        img = ImageEnhance.Contrast(img).enhance(1.2)
    # Daytime: no enhancement needed

    return img


def detect_vehicles(image_url: str) -> Optional[dict]:
    """Download a camera image and detect vehicles using YOLOv8s.

    Applies nighttime enhancement automatically based on image brightness.
    Returns: {"total": int, "vehicles": {"car": N, ...}, "inference_ms": float, "nightmode": bool}
    """
    try:
        from PIL import Image

        t0 = time.time()
        resp = httpx.get(image_url, timeout=10)
        if resp.status_code != 200:
            return None

        img = Image.open(io.BytesIO(resp.content)).convert("RGB")
        avg_brightness = np.array(img).mean()
        nightmode = avg_brightness < 100

        # Enhance for detection
        img_enhanced = _enhance_for_detection(img)

        img_resized = img_enhanced.resize((640, 640))
        img_array = np.array(img_resized).astype(np.float32) / 255.0
        img_tensor = np.expand_dims(np.transpose(img_array, (2, 0, 1)), axis=0)

        session, input_name = _get_session()
        outputs = session.run(None, {input_name: img_tensor})
        preds = outputs[0][0].T

        vehicles = {}
        for det in preds:
            class_id = int(np.argmax(det[4:]))
            conf = float(det[4:][class_id])
            if conf >= CONFIDENCE_THRESHOLD and class_id in VEHICLE_CLASSES:
                vtype = VEHICLE_CLASSES[class_id]
                vehicles[vtype] = vehicles.get(vtype, 0) + 1

        total = sum(vehicles.values())
        elapsed_ms = (time.time() - t0) * 1000

        return {
            "total": total,
            "vehicles": vehicles,
            "inference_ms": round(elapsed_ms, 1),
            "nightmode": nightmode,
            "brightness": round(avg_brightness, 1),
        }
    except Exception as e:
        print(f"[vision] Detection error: {e}")
        return None


def scan_cameras(camera_urls: list[tuple[str, str, str, float, float]]) -> list[dict]:
    """Scan multiple cameras for vehicle counts.

    Input: list of (camera_id, camera_name, image_url, lat, lng)
    Returns: list of detection results sorted by vehicle count (emptiest first).
    """
    results = []
    for cam_id, cam_name, image_url, lat, lng in camera_urls:
        detection = detect_vehicles(image_url)
        if detection is None:
            continue

        total = detection["total"]
        if total == 0:
            status = "empty"
        elif total <= 2:
            status = "light"
        elif total <= 5:
            status = "moderate"
        else:
            status = "heavy"

        results.append({
            "camera_id": cam_id,
            "camera_name": cam_name,
            "latitude": lat,
            "longitude": lng,
            "image_url": image_url,
            "vehicles_detected": total,
            "vehicle_breakdown": detection["vehicles"],
            "traffic_status": status,
            "parking_likelihood": "high" if total <= 2 else "medium" if total <= 5 else "low",
            "inference_ms": detection["inference_ms"],
        })

    results.sort(key=lambda r: r["vehicles_detected"])
    return results
