"""
Smart Camera Modules.

This package contains the core components of the smart camera system:
- CameraStream: Video capture and frame distribution
- YoloProcessor: Object detection using YOLO models
- FastFrameProcessor: Keyframe extraction based on motion detection
- SensorServer: TCP server for receiving sensor data
- DataStore: Lightweight storage for sensor and detection history
"""
from .camera_stream import CameraStream
from .yolo_processor import YoloProcessor
from .frame_processor import FastFrameProcessor
from .sensor_server import SensorServer
from .data_store import DataStore

__all__ = [
    "CameraStream",
    "YoloProcessor",
    "FastFrameProcessor",
    "SensorServer",
    "DataStore",
]
