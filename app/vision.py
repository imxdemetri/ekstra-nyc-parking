"""Real-time vehicle detection on NYC DOT camera feeds using YOLOv8n ONNX."""

import io
import time
from typing import Optional
import httpx
import numpy as np

_session = None
_input_name = None

# COCO vehicle class IDs
VEHICLE_CLASSES = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}
CONFIDENCE_THRESHOLD = 0.3


def _get_session():
    global _session, _input_name
    if _session is None:
        import onnxruntime as ort
        _session = ort.InferenceSession("models/yolov8n.onnx")
        _input_name = _session.get_inputs()[0].name
    return _session, _input_name


def detect_vehicles(image_url: str) -> Optional[dict]:
    """Download a camera image and detect vehicles using YOLOv8n.

    Returns: {"total": int, "vehicles": {"car": N, "bus": N, ...}, "inference_ms": float}
    """
    try:
        from PIL import Image

        t0 = time.time()
        resp = httpx.get(image_url, timeout=10)
        if resp.status_code != 200:
            return None

        img = Image.open(io.BytesIO(resp.content)).convert("RGB")
        img_resized = img.resize((640, 640))
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
