"""Download the ONNX models the pipeline needs into models/.

YuNet  — face detection (bounding box + 5 landmarks)
SFace  — face recognition embeddings (128-D)

Both come from the official opencv_zoo repository.
"""
import os
import urllib.request

MODELS = {
    "face_detection_yunet_2023mar.onnx":
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "face_recognition_sface_2021dec.onnx":
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
}

def main() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    models_dir = os.path.join(root, "models")
    os.makedirs(models_dir, exist_ok=True)
    for name, url in MODELS.items():
        dest = os.path.join(models_dir, name)
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            print(f"already present: {name}")
            continue
        print(f"downloading {name} ...")
        urllib.request.urlretrieve(url, dest)
        print(f"  -> {dest} ({os.path.getsize(dest)/1e6:.1f} MB)")
    print("done.")

if __name__ == "__main__":
    main()
