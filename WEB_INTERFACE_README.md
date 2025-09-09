# RFID Access Control System - Web Interface

## Overview

The web interface provides a comprehensive dashboard for monitoring RFID access control activities, including scanned tags and captured images.

## Features

### 📊 Dashboard Statistics
- **Total Scans**: Count of all RFID card scans
- **Access Granted**: Number of successful access attempts
- **Access Denied**: Number of denied access attempts
- **Images Captured**: Total number of images stored locally

### 📱 Recent Scans Display
- Real-time display of the latest 10 RFID scans
- Color-coded status indicators:
  - 🟢 **Green**: Access Granted
  - 🔴 **Red**: Access Denied
  - 🟡 **Yellow**: Blocked User
- Shows card number, user name, reader ID, and timestamp
- Auto-refreshes every 5 seconds

### 📸 Image Gallery
- Displays up to **100 most recent images** (bucket limit for viewing)
- Shows all locally stored images with upload status:
  - ✅ **Uploaded**: Successfully uploaded to S3
  - ⏳ **Pending**: Waiting to be uploaded
  - ❌ **Failed**: Upload failed (will retry)
- Click on any image to view full-size in modal
- Image details include:
  - Card number
  - Capture timestamp
  - Upload status
  - S3 location (if uploaded)

### 🔄 Real-time Updates
- Auto-refresh every 5 seconds
- Manual refresh button (floating action button)
- Connection status indicator
- Offline/online detection

## API Endpoints

### GET `/`
- **Description**: Main dashboard interface
- **Response**: HTML page with full interface

### GET `/get_transactions`
- **Description**: Fetch recent RFID transactions
- **Response**: JSON array of transaction objects
- **Limit**: 10 most recent transactions

### GET `/get_images`
- **Description**: Get list of captured images with upload status
- **Response**: JSON object with:
  ```json
  {
    "images": [...],           // Array of image objects (max 100)
    "total": 150,              // Total images stored
    "uploaded": 120,           // Successfully uploaded
    "pending": 25,             // Pending upload
    "failed": 5,               // Failed uploads
    "display_limit": 100       // Display limit
  }
  ```

### GET `/image/<filename>`
- **Description**: Serve image files
- **Parameters**: `filename` - Image filename (jpg/jpeg only)
- **Response**: Image file or error

### DELETE `/delete_image/<filename>`
- **Description**: Delete image and its upload metadata
- **Parameters**: `filename` - Image filename
- **Authentication**: Requires API key
- **Response**: JSON status response

## Image Storage Details

### File Naming Convention
Images are saved with the format: `{CARD_NUMBER}_{TIMESTAMP}.jpg`

Example: `123456789_1703123456.jpg`

### Upload Status Tracking
- Each image has a corresponding `.uploaded.json` sidecar file
- Contains upload metadata:
  ```json
  {
    "uploaded_at": 1703123456,
    "s3_location": "https://s3.amazonaws.com/bucket/path/image.jpg"
  }
  ```

### Storage Limits
- **Display Limit**: 100 images shown in interface
- **Storage Limit**: No limit - all images are stored locally
- **Auto-cleanup**: Manual deletion via API (future enhancement)

## Keyboard Shortcuts

- **Ctrl+R** or **F5**: Refresh data
- **Ctrl+A**: Toggle auto-refresh
- **Escape**: Close modals

## Browser Compatibility

- Modern browsers with ES6+ support
- Bootstrap 5.1.3
- Font Awesome 6.0.0
- Responsive design for mobile devices

## Security Features

- File type validation (jpg/jpeg only)
- Directory traversal protection
- API key authentication for sensitive operations
- Input sanitization

## Usage

1. **Access the Interface**: Navigate to `http://your-server:5001/`
2. **Monitor Scans**: View real-time RFID access attempts
3. **Browse Images**: Click on images to view full-size
4. **Check Status**: Monitor upload status of captured images
5. **Download Images**: Click on images in modal to download

## Configuration

The interface uses the same configuration as the main application:
- `IMAGES_DIR`: Directory for storing images (default: "images")
- `API_KEY`: Authentication key for sensitive operations
- `FLASK_HOST`: Server host (default: 0.0.0.0)
- `FLASK_PORT`: Server port (default: 5001)

## Troubleshooting

### Images Not Loading
- Check if `IMAGES_DIR` exists and is readable
- Verify image files are in correct format (jpg/jpeg)
- Check file permissions

### Upload Status Issues
- Verify `.uploaded.json` sidecar files exist
- Check S3 API configuration
- Review application logs for upload errors

### Performance Issues
- Reduce display limit if needed
- Check available disk space
- Monitor memory usage with large image collections
