import os
import time
import requests
import logging
from typing import Optional
from config import S3_API_URL, MAX_RETRIES, RETRY_DELAY

class ImageUploader:
    def __init__(self):
        self.logger = logging.getLogger(__name__)

    def upload(self, filepath: str) -> Optional[str]:
        """Upload image file to S3-compatible API."""
        if not os.path.exists(filepath):
            self.logger.error(f"File does not exist: {filepath}")
            return None
            
        if not os.path.isfile(filepath):
            self.logger.error(f"Path is not a file: {filepath}")
            return None
            
        # Check file size (limit to 10MB)
        file_size = os.path.getsize(filepath)
        if file_size > 10 * 1024 * 1024:  # 10MB
            self.logger.error(f"File too large: {filepath} ({file_size} bytes)")
            return None
            
        attempts = 0
        while attempts < MAX_RETRIES:
            try:
                with open(filepath, "rb") as image_file:
                    files = {
                        "singleFile": (os.path.basename(filepath), image_file, "image/jpeg")
                    }
                    response = requests.post(S3_API_URL, files=files, timeout=30)

                if response.status_code == 200:
                    self.logger.info(f"Successfully uploaded: {filepath}")
                    try:
                        response_json = response.json()
                        location = response_json.get("Location")
                        if location:
                            self.logger.info(f"S3 Response: {response_json}")
                            # Don't remove file - keep for gallery display
                            # os.remove(filepath)
                            return location
                        else:
                            self.logger.error(f"No Location in response: {response_json}")
                    except ValueError as e:
                        self.logger.error(f"Invalid JSON response: {e}")
                        self.logger.error(f"Response content: {response.text}")
                else:
                    self.logger.error(f"Upload failed {filepath}: {response.status_code} - {response.text}")

            except requests.exceptions.RequestException as e:
                self.logger.error(f"Upload error for {filepath}: {e}")
            except Exception as e:
                self.logger.error(f"Unexpected error during upload of {filepath}: {e}")

            attempts += 1
            if attempts < MAX_RETRIES:
                self.logger.info(f"Retrying upload in {RETRY_DELAY} seconds... (attempt {attempts + 1}/{MAX_RETRIES})")
                time.sleep(RETRY_DELAY)

        self.logger.error(f"Giving up on {filepath} after {MAX_RETRIES} attempts.")
        return None
