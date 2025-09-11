import os
from typing import Dict

# Camera credentials and URLs - use environment variables for security
CAMERA_USERNAME = os.getenv("CAMERA_USERNAME", "admin")
CAMERA_PASSWORD = os.getenv("CAMERA_PASSWORD", "admin")
CAMERA_1_IP = os.getenv("CAMERA_1_IP", "192.168.1.201")
CAMERA_2_IP = os.getenv("CAMERA_2_IP", "192.168.1.202")

# Function to get current RTSP URLs (reads from environment each time)
def get_rtsp_cameras():
    """Get current RTSP camera URLs, reading from environment variables each time."""
    # Reload environment variables
    from dotenv import load_dotenv
    load_dotenv()
    
    # Get current values
    camera_username = os.getenv("CAMERA_USERNAME", "admin")
    camera_password = os.getenv("CAMERA_PASSWORD", "admin")
    camera_1_ip = os.getenv("CAMERA_1_IP", "192.168.1.201")
    camera_2_ip = os.getenv("CAMERA_2_IP", "192.168.1.202")
    camera_1_rtsp = os.getenv("CAMERA_1_RTSP", "")
    camera_2_rtsp = os.getenv("CAMERA_2_RTSP", "")
    
    # Use custom RTSP URLs if provided, otherwise generate from IP/credentials
    return {
        "camera_1": camera_1_rtsp if camera_1_rtsp else f"rtsp://{camera_username}:{camera_password}@{camera_1_ip}:554/avstream/channel=1/stream=0.sdp",
        "camera_2": camera_2_rtsp if camera_2_rtsp else f"rtsp://{camera_username}:{camera_password}@{camera_2_ip}:554/avstream/channel=1/stream=0.sdp"
    }

# For backward compatibility, create a property-like access
class RTSPCameras:
    def __getitem__(self, key):
        return get_rtsp_cameras()[key]
    
    def get(self, key, default=None):
        return get_rtsp_cameras().get(key, default)

# Create instance for backward compatibility
RTSP_CAMERAS = RTSPCameras()

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
