"""
Smart Camera Modules.

This package contains the core components of the smart camera system:
- CameraStream: Video capture and frame distribution
- YoloProcessor: Object detection using YOLO models (torch or OpenCV DNN backend)
- FastFrameProcessor: Keyframe extraction based on motion detection
- SensorServer: UDP server for receiving sensor data
- SpeechRecognizer: Offline speech recognition (vosk + arecord)
- DataStore: Lightweight storage for sensor, detection and speech history
"""
from .camera_stream import CameraStream
from .yolo_processor import YoloProcessor
from .frame_processor import FastFrameProcessor
from .sensor_server import SensorServer
from .speech_recognizer import SpeechRecognizer
from .data_store import DataStore

__all__ = [
    "CameraStream",
    "YoloProcessor",
    "FastFrameProcessor",
    "SensorServer",
    "SpeechRecognizer",
    "DataStore",
]
