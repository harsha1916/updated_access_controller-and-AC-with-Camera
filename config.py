import os
from typing import Dict

# Camera credentials and URLs - use environment variables for security
CAMERA_USERNAME = os.getenv("CAMERA_USERNAME", "admin")
CAMERA_PASSWORD = os.getenv("CAMERA_PASSWORD", "admin")
CAMERA_1_IP = os.getenv("CAMERA_1_IP", "192.168.1.201")
CAMERA_2_IP = os.getenv("CAMERA_2_IP", "192.168.1.202")

RTSP_CAMERAS = {
    "camera_1": f"rtsp://{CAMERA_USERNAME}:{CAMERA_PASSWORD}@{CAMERA_1_IP}:554/avstream/channel=1/stream=0.sdp",
    "camera_2": f"rtsp://{CAMERA_USERNAME}:{CAMERA_PASSWORD}@{CAMERA_2_IP}:554/avstream/channel=1/stream=0.sdp"
}

# API Configuration
S3_API_URL = os.getenv("S3_API_URL", "https://api.easyparkai.com/api/Common/Upload?modulename=anpr")

# Retry Configuration
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "5"))
RETRY_DELAY = int(os.getenv("RETRY_DELAY", "5"))

# Server Configuration
BIND_IP = os.getenv("BIND_IP", "192.168.1.33")
BIND_PORT = int(os.getenv("BIND_PORT", "9000"))

# GPIO Configuration for Raspberry Pi
GPIO_CAMERA_1_PIN = int(os.getenv("GPIO_CAMERA_1_PIN", "18"))  # GPIO pin for camera 1 trigger
GPIO_CAMERA_2_PIN = int(os.getenv("GPIO_CAMERA_2_PIN", "19"))  # GPIO pin for camera 2 trigger
GPIO_ENABLED = os.getenv("GPIO_ENABLED", "false").lower() == "true"  # Enable GPIO functionality
