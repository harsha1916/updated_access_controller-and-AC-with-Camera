import json
import threading
import pigpio
import time
import sys
import RPi.GPIO as GPIO
import firebase_admin
from firebase_admin import credentials, firestore
from flask import Flask, request, render_template, jsonify, session, redirect, url_for
import requests
import logging
import os
from datetime import datetime, timedelta
import google.api_core.exceptions
from queue import Queue
from dotenv import load_dotenv
import hashlib
import secrets

# NEW/UPDATED imports for camera capture & upload
import cv2
from concurrent.futures import ThreadPoolExecutor

# Use your config/uploader modules (RTSP cameras, retry configs, S3 API)
# (These come from your uploaded files.)
from config import RTSP_CAMERAS, MAX_RETRIES, RETRY_DELAY  # :contentReference[oaicite:3]{index=3}
from uploader import ImageUploader  # :contentReference[oaicite:4]{index=4}

# =========================
# Environment / Constants
# =========================
load_dotenv()

transaction_queue = Queue()
image_queue = Queue()  # for background S3 uploads (non-blocking)
IMAGES_DIR = os.environ.get("IMAGES_DIR", "images")
os.makedirs(IMAGES_DIR, exist_ok=True)

# Storage Management Configuration (Dynamic - based on available free space)
# Fallback values for when dynamic calculation fails
MAX_STORAGE_GB = int(os.environ.get("MAX_STORAGE_GB", "20"))  # Fallback maximum storage for images (20GB)
CLEANUP_THRESHOLD_GB = int(os.environ.get("CLEANUP_THRESHOLD_GB", "10"))  # Fallback amount to delete when limit reached (10GB)
STORAGE_CHECK_INTERVAL = int(os.environ.get("STORAGE_CHECK_INTERVAL", "300"))  # Check storage every 5 minutes

# GPIO Pins for Two Wiegand RFID Readers
D0_PIN_1 = int(os.environ.get('D0_PIN_1', 18))  # Wiegand Data 0 (Reader 1 - Green)
D1_PIN_1 = int(os.environ.get('D1_PIN_1', 23))  # Wiegand Data 1 (Reader 1 - White)
D0_PIN_2 = int(os.environ.get('D0_PIN_2', 19))  # Wiegand Data 0 (Reader 2 - Green)
D1_PIN_2 = int(os.environ.get('D1_PIN_2', 24))  # Wiegand Data 1 (Reader 2 - White)

# GPIO Pins for Two Relays
RELAY_1 = int(os.environ.get('RELAY_1', 25))  # Relay for Reader 1
RELAY_2 = int(os.environ.get('RELAY_2', 26))  # Relay for Reader 2

# File Paths
BASE_DIR = os.environ.get('BASE_DIR', '/home/maxpark')
USER_DATA_FILE = os.path.join(BASE_DIR, "users.json")
BLOCKED_USERS_FILE = os.path.join(BASE_DIR, "blocked_users.json")
TRANSACTION_CACHE_FILE = os.path.join(BASE_DIR, "transactions_cache.json")
DAILY_STATS_FILE = os.path.join(BASE_DIR, "daily_stats.json")
FIREBASE_CRED_FILE = os.environ.get('FIREBASE_CRED_FILE', "service.json")

# Ensure base directory exists
os.makedirs(BASE_DIR, exist_ok=True)

# Flask
app = Flask(__name__, static_folder='static')
app.secret_key = os.environ.get('SECRET_KEY', 'your-secret-key-change-this')

# Simple API key authentication
API_KEY = os.environ.get('API_KEY', 'your-api-key-change-this')

# Authentication configuration
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD_HASH = os.environ.get('ADMIN_PASSWORD_HASH', hashlib.sha256('admin123'.encode()).hexdigest())
SESSION_SECRET = os.environ.get('SESSION_SECRET', secrets.token_hex(32))

# Set session secret key
app.secret_key = SESSION_SECRET

# Store active sessions
active_sessions = {}

def cleanup_expired_sessions():
    """Remove expired sessions"""
    current_time = datetime.now()
    expired_tokens = []
    
    for token, session_data in active_sessions.items():
        if current_time > session_data['expires']:
            expired_tokens.append(token)
    
    for token in expired_tokens:
        del active_sessions[token]
        logging.info(f"Expired session removed: {token[:8]}...")

# Cleanup expired sessions every hour
def session_cleanup_worker():
    """Background worker to clean up expired sessions"""
    while True:
        try:
            cleanup_expired_sessions()
            time.sleep(3600)  # Check every hour
        except Exception as e:
            logging.error(f"Session cleanup error: {e}")
            time.sleep(60)  # Retry in 1 minute on error

def daily_stats_cleanup_worker():
    """Background worker to clean up old daily statistics"""
    while True:
        try:
            cleanup_old_daily_stats()
            time.sleep(86400)  # Check every 24 hours
        except Exception as e:
            logging.error(f"Daily stats cleanup error: {e}")
            time.sleep(3600)  # Retry in 1 hour on error

def hash_password(password):
    """Hash password using SHA-256"""
    return hashlib.sha256(password.encode()).hexdigest()

def generate_session_token():
    """Generate a secure session token"""
    return secrets.token_urlsafe(32)

def is_authenticated():
    """Check if user is authenticated"""
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        token = request.args.get('token')
    
    return token in active_sessions

def require_auth(f):
    """Decorator to require authentication"""
    def decorated_function(*args, **kwargs):
        if not is_authenticated():
            return jsonify({"status": "error", "message": "Authentication required"}), 401
        return f(*args, **kwargs)
    decorated_function.__name__ = f.__name__
    return decorated_function

def require_api_key(f):
    """Decorator to require API key for sensitive endpoints"""
    def decorated_function(*args, **kwargs):
        api_key = request.args.get('api_key') or request.headers.get('X-API-Key')
        if api_key != API_KEY:
            return jsonify({"status": "error", "message": "Invalid API key"}), 401
        return f(*args, **kwargs)
    decorated_function.__name__ = f.__name__
    return decorated_function

# Logging
LOG_FILE = os.environ.get('LOG_FILE', 'rfid_system.log')
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO')
logging.basicConfig(filename=LOG_FILE, level=getattr(logging, LOG_LEVEL), format="%(asctime)s - %(message)s")

# Firestore
db = None
try:
    cred = credentials.Certificate(FIREBASE_CRED_FILE)
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    logging.info("Firebase initialized successfully.")
except FileNotFoundError:
    logging.error(f"Firebase credentials file not found: {FIREBASE_CRED_FILE}")
except Exception as e:
    logging.error(f"Error initializing Firebase: {str(e)}")
    db = None  # Set to None when Firebase is unavailable

# GPIO Setup for Relays with error handling
try:
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(RELAY_1, GPIO.OUT)
    GPIO.setup(RELAY_2, GPIO.OUT)
    GPIO.output(RELAY_1, GPIO.HIGH)  # Default relay 1 closed
    GPIO.output(RELAY_2, GPIO.HIGH)  # Default relay 2 closed
    logging.info("GPIO relays initialized successfully.")
except Exception as e:
    logging.error(f"Error initializing GPIO relays: {str(e)}")
    # Continue without relay functionality

relay_status = 0

# pigpio
pi = None
try:
    pi = pigpio.pi()
    if not pi.connected:
        logging.warning("Unable to connect to pigpio daemon. RFID readers will be disabled.")
        pi = None
    else:
        print("pigpio connected")
        logging.info("Pigpio connected successfully.")
except Exception as e:
    logging.error(f"Error initializing pigpio: {str(e)}")
    pi = None

# =========================
# Utilities
# =========================
def is_internet_available():
    """Fast and reliable internet check using 204 endpoints."""
    retries = int(os.environ.get('INTERNET_CHECK_RETRIES', 3))
    timeout = int(os.environ.get('INTERNET_CHECK_TIMEOUT', 5))
    urls = [
        "http://clients3.google.com/generate_204",   # HTTP avoids cert/time issues at boot
        "https://www.gstatic.com/generate_204",
        "https://www.google.com/generate_204",
        "https://cloudflare.com/cdn-cgi/trace",
    ]
    for _ in range(retries):
        for u in urls:
            try:
                r = requests.get(u, timeout=timeout)
                if r.status_code in (200, 204):
                    return True
            except requests.RequestException:
                continue
        time.sleep(2)
    return False

def atomic_write_json(path, data):
    """Write JSON atomically to avoid corruption."""
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=4)
    os.replace(tmp, path)

def read_json_or_default(path, default):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        logging.error(f"Error reading {path}: {e}")
        return default

def _ts_to_epoch(ts):
    """Normalize Firestore/epoch timestamps to float epoch seconds."""
    try:
        if hasattr(ts, "timestamp"):
            return float(ts.timestamp())
        elif isinstance(ts, (int, float)):
            return float(ts)
    except Exception:
        pass
    return time.time()

# =========================
# Thread-safe stores + O(1) sets for fast lookups
# =========================
USERS_LOCK = threading.RLock()
BLOCKED_LOCK = threading.RLock()
ALLOWED_SET_LOCK = threading.RLock()
BLOCKED_SET_LOCK = threading.RLock()

users = read_json_or_default(USER_DATA_FILE, {})              # dict[str_card] -> user dict
blocked_users = read_json_or_default(BLOCKED_USERS_FILE, {})  # dict[str_card] -> bool

ALLOWED_SET = set()  # set[int]
BLOCKED_SET = set()  # set[int]

def _card_str_to_int(card_str: str):
    try:
        return int(card_str)
    except Exception:
        return None

def _rebuild_allowed_set_from_users_dict(u: dict):
    global ALLOWED_SET
    with ALLOWED_SET_LOCK:
        ALLOWED_SET = set()
        for k in u.keys():
            ci = _card_str_to_int(k)
            if ci is not None:
                ALLOWED_SET.add(ci)

def _rebuild_blocked_set_from_dict(b: dict):
    global BLOCKED_SET
    with BLOCKED_SET_LOCK:
        BLOCKED_SET = set()
        for k, v in b.items():
            if v:
                ci = _card_str_to_int(k)
                if ci is not None:
                    BLOCKED_SET.add(ci)

def load_local_users():
    """Load users from disk into memory and refresh the ALLOWED_SET."""
    global users
    with USERS_LOCK:
        users = read_json_or_default(USER_DATA_FILE, {})
        _rebuild_allowed_set_from_users_dict(users)
        return dict(users)

def save_local_users(new_users):
    """Persist users and refresh the ALLOWED_SET."""
    global users
    with USERS_LOCK:
        users = dict(new_users)
        atomic_write_json(USER_DATA_FILE, users)
        _rebuild_allowed_set_from_users_dict(users)

def load_blocked_users():
    """Load blocked users from disk into memory and refresh the BLOCKED_SET."""
    global blocked_users
    with BLOCKED_LOCK:
        blocked_users = read_json_or_default(BLOCKED_USERS_FILE, {})
        _rebuild_blocked_set_from_dict(blocked_users)
        return dict(blocked_users)

def save_blocked_users(new_blocked):
    """Persist blocked users and refresh the BLOCKED_SET."""
    global blocked_users
    with BLOCKED_LOCK:
        blocked_users = dict(new_blocked)
        atomic_write_json(BLOCKED_USERS_FILE, blocked_users)
        _rebuild_blocked_set_from_dict(blocked_users)

def cache_transaction(transaction):
    """Stores transactions locally when internet is unavailable."""
    txns = read_json_or_default(TRANSACTION_CACHE_FILE, [])
    txns.append(transaction)
    atomic_write_json(TRANSACTION_CACHE_FILE, txns)

def update_daily_stats(status):
    """Update daily statistics for access attempts."""
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        stats = read_json_or_default(DAILY_STATS_FILE, {})
        
        if today not in stats:
            stats[today] = {
                'date': today,
                'valid_entries': 0,
                'invalid_entries': 0,
                'blocked_entries': 0
            }
        
        if status == 'Access Granted':
            stats[today]['valid_entries'] += 1
        elif status == 'Access Denied':
            stats[today]['invalid_entries'] += 1
        elif status == 'Blocked':
            stats[today]['blocked_entries'] += 1
        
        atomic_write_json(DAILY_STATS_FILE, stats)
        
        # Clean up old stats (keep only 20 days)
        cleanup_old_daily_stats()
        
    except Exception as e:
        logging.error(f"Error updating daily stats: {e}")

def cleanup_old_daily_stats():
    """Remove daily statistics older than 20 days."""
    try:
        stats = read_json_or_default(DAILY_STATS_FILE, {})
        cutoff_date = datetime.now() - timedelta(days=20)
        cutoff_str = cutoff_date.strftime('%Y-%m-%d')
        
        old_dates = [date for date in stats.keys() if date < cutoff_str]
        for date in old_dates:
            del stats[date]
        
        if old_dates:
            atomic_write_json(DAILY_STATS_FILE, stats)
            logging.info(f"Cleaned up {len(old_dates)} old daily statistics")
            
    except Exception as e:
        logging.error(f"Error cleaning up daily stats: {e}")

def get_daily_stats():
    """Get daily statistics for the last 20 days."""
    try:
        stats = read_json_or_default(DAILY_STATS_FILE, {})
        
        # Generate last 20 days
        today = datetime.now()
        last_20_days = []
        
        for i in range(20):
            date = today - timedelta(days=i)
            date_str = date.strftime('%Y-%m-%d')
            
            if date_str in stats:
                last_20_days.append(stats[date_str])
            else:
                last_20_days.append({
                    'date': date_str,
                    'valid_entries': 0,
                    'invalid_entries': 0,
                    'blocked_entries': 0
                })
        
        # Sort by date (oldest first)
        last_20_days.sort(key=lambda x: x['date'])
        
        return last_20_days
        
    except Exception as e:
        logging.error(f"Error getting daily stats: {e}")
        return []

def sync_transactions():
    """Syncs offline transactions with Firebase when internet is restored."""
    if not os.path.exists(TRANSACTION_CACHE_FILE):
        return
    if not (is_internet_available() and db is not None):
        return
    try:
        txns = read_json_or_default(TRANSACTION_CACHE_FILE, [])
        if not txns:
            return

        batch_size = 10
        idx = 0
        synced = 0
        failed_txns = []
        
        while idx < len(txns):
            batch = txns[idx:idx + batch_size]
            for txn in batch:
                try:
                    db.collection("transactions").add(txn)
                    synced += 1
                    logging.info(f"Successfully synced transaction: {txn.get('card_number', 'unknown')}")
                except google.api_core.exceptions.DeadlineExceeded:
                    logging.warning(f"Firestore transaction timeout during sync for card: {txn.get('card_number', 'unknown')}")
                    failed_txns.append(txn)
                except Exception as e:
                    logging.error(f"Error syncing transaction {txn.get('card_number', 'unknown')}: {str(e)}")
                    failed_txns.append(txn)
            idx += batch_size
            time.sleep(1)
        
        # Only remove the cache file if ALL transactions were synced successfully
        if failed_txns:
            # Update cache file with only failed transactions
            atomic_write_json(TRANSACTION_CACHE_FILE, failed_txns)
            logging.warning(f"Synced {synced} transactions, {len(failed_txns)} failed and kept in cache")
        else:
            # All transactions synced successfully, remove cache file
            os.remove(TRANSACTION_CACHE_FILE)
            logging.info(f"All {synced} offline transactions synced successfully")
            
    except Exception as e:
        logging.error(f"Error syncing transactions: {str(e)}")

# =========================
# Rate Limiter (thread-safe, keyed by int card)
# =========================
class ScanRateLimiter:
    def __init__(self, delay_seconds=60):
        self.last_seen = {}
        self.delay = delay_seconds
        self._lock = threading.Lock()

    def should_process(self, card_int: int):
        now = time.time()
        with self._lock:
            last = self.last_seen.get(card_int, 0)
            if now - last >= self.delay:
                self.last_seen[card_int] = now
                return True
            return False

rate_limiter = ScanRateLimiter(delay_seconds=int(os.environ.get("SCAN_DELAY_SECONDS", "60")))

# =========================
# Camera capture manager (integrated; non-blocking)
# =========================
def _sanitize_card_number(card_number: str) -> str:
    s = str(card_number).strip()
    s = s.replace(" ", "_")
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    s = "".join(ch for ch in s if ch in allowed)
    return s[:50] if s else "unknown"

def get_disk_usage():
    """Get disk usage information for the images directory."""
    try:
        import shutil
        total, used, free = shutil.disk_usage(IMAGES_DIR)
        return {
            'total': total,
            'used': used,
            'free': free
        }
    except Exception as e:
        logging.error(f"Error getting disk usage: {e}")
        return None

def get_storage_usage():
    """Get current storage usage in bytes."""
    if not os.path.exists(IMAGES_DIR):
        return 0
    
    total_size = 0
    for filename in os.listdir(IMAGES_DIR):
        filepath = os.path.join(IMAGES_DIR, filename)
        if os.path.isfile(filepath):
            total_size += os.path.getsize(filepath)
    return total_size

def get_dynamic_storage_limits():
    """Calculate dynamic storage limits based on available free space."""
    disk_info = get_disk_usage()
    if not disk_info:
        # Fallback to fixed limits if disk info unavailable
        return MAX_STORAGE_GB, CLEANUP_THRESHOLD_GB
    
    free_space_gb = disk_info['free'] / (1024**3)
    
    # Allocate 60% of free space for images
    max_storage_gb = int(free_space_gb * 0.6)
    
    # Delete 30% of allocated space when limit reached
    cleanup_threshold_gb = int(max_storage_gb * 0.3)
    
    # Ensure minimum values
    max_storage_gb = max(max_storage_gb, 1)  # At least 1GB
    cleanup_threshold_gb = max(cleanup_threshold_gb, 0.5)  # At least 0.5GB
    
    return max_storage_gb, cleanup_threshold_gb

def cleanup_old_images():
    """Automatically clean up old images when storage limit is reached."""
    try:
        current_usage = get_storage_usage()
        
        # Get dynamic storage limits based on available free space
        max_storage_gb, cleanup_threshold_gb = get_dynamic_storage_limits()
        max_bytes = max_storage_gb * 1024 * 1024 * 1024  # Convert GB to bytes
        cleanup_bytes = cleanup_threshold_gb * 1024 * 1024 * 1024  # Convert GB to bytes
        
        if current_usage < max_bytes:
            return
        
        logging.info(f"Storage limit reached ({current_usage / (1024**3):.2f}GB). Starting cleanup...")
        
        # Get all image files with their timestamps
        image_files = []
        for filename in os.listdir(IMAGES_DIR):
            if filename.lower().endswith(('.jpg', '.jpeg')):
                filepath = os.path.join(IMAGES_DIR, filename)
                if os.path.isfile(filepath):
                    # Extract timestamp from filename
                    name_without_ext = os.path.splitext(filename)[0]
                    parts = name_without_ext.split('_')
                    
                    if len(parts) >= 3:
                        # New format: card_reader_timestamp
                        try:
                            timestamp = int(parts[2])
                        except ValueError:
                            timestamp = int(os.path.getmtime(filepath))
                    elif len(parts) >= 2:
                        # Old format: card_timestamp
                        try:
                            timestamp = int(parts[-1])
                        except ValueError:
                            timestamp = int(os.path.getmtime(filepath))
                    else:
                        timestamp = int(os.path.getmtime(filepath))
                    
                    file_size = os.path.getsize(filepath)
                    image_files.append((filepath, filename, timestamp, file_size))
        
        # Sort by timestamp (oldest first)
        image_files.sort(key=lambda x: x[2])
        
        # Delete oldest images until we've freed up enough space
        deleted_size = 0
        deleted_count = 0
        
        for filepath, filename, timestamp, file_size in image_files:
            if deleted_size >= cleanup_bytes:
                break
                
            try:
                os.remove(filepath)
                # Also remove upload sidecar if exists
                sidecar_path = filepath + ".uploaded.json"
                if os.path.exists(sidecar_path):
                    os.remove(sidecar_path)
                
                deleted_size += file_size
                deleted_count += 1
                logging.info(f"Deleted old image: {filename}")
                
            except Exception as e:
                logging.error(f"Error deleting {filename}: {e}")
        
        new_usage = get_storage_usage()
        logging.info(f"Cleanup completed. Deleted {deleted_count} images ({deleted_size / (1024**3):.2f}GB). "
                    f"New usage: {new_usage / (1024**3):.2f}GB")
        
    except Exception as e:
        logging.error(f"Error during storage cleanup: {e}")

def storage_monitor_worker():
    """Background worker to monitor storage usage."""
    while True:
        try:
            cleanup_old_images()
            time.sleep(STORAGE_CHECK_INTERVAL)
        except Exception as e:
            logging.error(f"Error in storage monitor worker: {e}")
            time.sleep(60)  # Wait 1 minute before retrying

def _rtsp_capture_single(rtsp_url: str, filepath: str) -> bool:
    """Open RTSP, grab one frame, save JPEG. Retries using MAX_RETRIES/RETRY_DELAY."""
    retries = 0
    while retries < MAX_RETRIES:
        cap = None
        try:
            cap = cv2.VideoCapture(rtsp_url)
            if not cap.isOpened():
                logging.warning(f"RTSP not open. Retry {retries+1}/{MAX_RETRIES} ...")
                retries += 1
                time.sleep(RETRY_DELAY)
                continue
            ret, frame = cap.read()
            if not ret or frame is None:
                logging.warning("Failed to read frame. Retrying ...")
                retries += 1
                time.sleep(RETRY_DELAY)
                continue
            ok = cv2.imwrite(filepath, frame)
            if ok:
                return True
            logging.error(f"Failed to save image to {filepath}")
            retries += 1
            time.sleep(RETRY_DELAY)
        except Exception as e:
            logging.error(f"Capture error: {e}")
            retries += 1
            time.sleep(RETRY_DELAY)
        finally:
            if cap is not None:
                cap.release()
    return False

def _mark_uploaded(filepath: str, location: str):
    meta = {
        "uploaded_at": int(time.time()),
        "s3_location": location
    }
    try:
        with open(filepath + ".uploaded.json", "w") as f:
            json.dump(meta, f, indent=2)
    except Exception as e:
        logging.error(f"Failed to write upload sidecar for {filepath}: {e}")

def _has_uploaded_sidecar(filepath: str) -> bool:
    return os.path.exists(filepath + ".uploaded.json")

# Thread pool for camera so scans don't block
CAMERA_WORKERS = int(os.environ.get("CAMERA_WORKERS", "2"))
camera_executor = ThreadPoolExecutor(max_workers=CAMERA_WORKERS)

def capture_for_reader_async(reader_id: int, card_int: int):
    """
    Non-blocking: pick camera based on reader, save image as CARD_TIMESTAMP.jpg
    Later upload happens from a background worker that scans the images directory.
    """
    try:
        # Check if camera is enabled for this reader
        camera_enabled_key = "camera_1_enabled" if reader_id == 1 else "camera_2_enabled"
        camera_enabled = os.getenv(camera_enabled_key, "true").lower() == "true"
        
        if not camera_enabled:
            logging.info(f"Camera {reader_id} is disabled, skipping image capture for card {card_int}")
            return

        card_str = str(card_int)
        safe = _sanitize_card_number(card_str)
        ts = int(time.time())
        filename = f"{safe}_r{reader_id}_{ts}.jpg"  # format: card_reader_timestamp
        filepath = os.path.join(IMAGES_DIR, filename)

        camera_key = "camera_1" if reader_id == 1 else "camera_2"
        rtsp_url = RTSP_CAMERAS.get(camera_key)  # from your config.py  :contentReference[oaicite:5]{index=5}
        if not rtsp_url:
            logging.error(f"No RTSP URL configured for {camera_key}")
            return

        ok = _rtsp_capture_single(rtsp_url, filepath)
        if ok:
            logging.info(f"[CAPTURE] {camera_key}: saved {filepath}")
            # Do NOT upload here; queue or let the sync loop find it later.
            # Optionally enqueue now to speed up online uploads:
            image_queue.put(filepath)
        else:
            logging.error(f"[CAPTURE] {camera_key}: failed to capture image for card {card_str}")
    except Exception as e:
        logging.error(f"capture_for_reader_async error: {e}")

# =========================
# Wiegand Decoder
# =========================
class WiegandDecoder:
    def __init__(self, pi, d0, d1, callback, timeout_ms=25):
        self.pi = pi
        self.d0 = d0
        self.d1 = d1
        self.callback = callback
        self.timeout_ms = timeout_ms

        self.value = 0
        self.bits = 0
        self.last_tick = None

        pi.set_mode(d0, pigpio.INPUT)
        pi.set_mode(d1, pigpio.INPUT)
        pi.set_pull_up_down(d0, pigpio.PUD_UP)
        pi.set_pull_up_down(d1, pigpio.PUD_UP)

        self.cb0 = pi.callback(d0, pigpio.FALLING_EDGE, self._handle_d0)
        self.cb1 = pi.callback(d1, pigpio.FALLING_EDGE, self._handle_d1)

    def _handle_d0(self, gpio, level, tick):
        self._process_bit(0, tick)

    def _handle_d1(self, gpio, level, tick):
        self._process_bit(1, tick)

    def _process_bit(self, bit, tick):
        if self.last_tick is not None and pigpio.tickDiff(self.last_tick, tick) > self.timeout_ms * 1000:
            self.value = 0
            self.bits = 0

        self.value = (self.value << 1) | bit
        self.bits += 1
        self.last_tick = tick

        if self.bits == 26:
            self.callback(self.bits, self.value)
            self.value = 0
            self.bits = 0

    def cancel(self):
        try:
            if hasattr(self, "cb0") and self.cb0:
                self.cb0.cancel()
        except Exception:
            pass
        try:
            if hasattr(self, "cb1") and self.cb1:
                self.cb1.cancel()
        except Exception:
            pass

# =========================
# Flask Routes
# =========================
@app.route("/")
def home():
    return redirect(url_for('login'))

@app.route("/login")
def login():
    return render_template("login.html")

@app.route("/login", methods=["POST"])
def login_post():
    """Handle login authentication"""
    try:
        data = request.get_json()
        username = data.get('username')
        password = data.get('password')
        
        if not username or not password:
            return jsonify({"status": "error", "message": "Username and password required"}), 400
        
        # Check credentials
        if username == ADMIN_USERNAME and hash_password(password) == ADMIN_PASSWORD_HASH:
            # Generate session token
            token = generate_session_token()
            active_sessions[token] = {
                'username': username,
                'login_time': datetime.now(),
                'expires': datetime.now() + timedelta(hours=24)
            }
            
            logging.info(f"User {username} logged in successfully")
            return jsonify({
                "status": "success", 
                "message": "Login successful",
                "token": token
            })
        else:
            logging.warning(f"Failed login attempt for username: {username}")
            return jsonify({"status": "error", "message": "Invalid username or password"}), 401
            
    except Exception as e:
        logging.error(f"Login error: {e}")
        return jsonify({"status": "error", "message": "Login failed"}), 500

@app.route("/logout", methods=["POST"])
def logout():
    """Handle logout"""
    try:
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if token in active_sessions:
            username = active_sessions[token]['username']
            del active_sessions[token]
            logging.info(f"User {username} logged out")
            return jsonify({"status": "success", "message": "Logged out successfully"})
        else:
            return jsonify({"status": "error", "message": "Invalid session"}), 401
    except Exception as e:
        logging.error(f"Logout error: {e}")
        return jsonify({"status": "error", "message": "Logout failed"}), 500

@app.route("/dashboard")
def dashboard():
    """Main dashboard - requires authentication"""
    return render_template("index.html")

@app.route("/change_password", methods=["POST"])
def change_password():
    """Change admin password"""
    global ADMIN_PASSWORD_HASH
    try:
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if token not in active_sessions:
            return jsonify({"status": "error", "message": "Authentication required"}), 401
        
        data = request.get_json()
        current_password = data.get('current_password')
        new_password = data.get('new_password')
        
        if not current_password or not new_password:
            return jsonify({"status": "error", "message": "Current and new password required"}), 400
        
        # Verify current password
        if hash_password(current_password) != ADMIN_PASSWORD_HASH:
            return jsonify({"status": "error", "message": "Current password is incorrect"}), 401
        
        # Update password hash
        new_password_hash = hash_password(new_password)
        
        # Update environment variable (this would need to be persisted to .env file)
        
        ADMIN_PASSWORD_HASH = new_password_hash
        
        # Update .env file
        env_file = ".env"
        env_vars = {}
        
        if os.path.exists(env_file):
            with open(env_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, value = line.split('=', 1)
                        env_vars[key] = value
        
        env_vars['ADMIN_PASSWORD_HASH'] = new_password_hash
        
        with open(env_file, 'w') as f:
            for key, value in env_vars.items():
                f.write(f"{key}={value}\n")
        
        logging.info(f"Password changed for user: {active_sessions[token]['username']}")
        return jsonify({"status": "success", "message": "Password changed successfully"})
        
    except Exception as e:
        logging.error(f"Password change error: {e}")
        return jsonify({"status": "error", "message": "Password change failed"}), 500

@app.route("/status")
def system_status():
    """Get system status information"""
    try:
        status = {
            "system": "online",
            "timestamp": datetime.now().isoformat(),
            "components": {
                "firebase": db is not None,
                "pigpio": pi is not None and pi.connected if pi else False,
                "rfid_readers": wiegand1 is not None and wiegand2 is not None,
                "gpio": True,
                "internet": is_internet_available()
            },
            "files": {
                "users_file": os.path.exists(USER_DATA_FILE),
                "blocked_users_file": os.path.exists(BLOCKED_USERS_FILE),
                "transaction_cache": os.path.exists(TRANSACTION_CACHE_FILE)
            }
        }
        if status["files"]["transaction_cache"]:
            try:
                cached_transactions = read_json_or_default(TRANSACTION_CACHE_FILE, [])
                status["cached_transactions_count"] = len(cached_transactions)
            except Exception:
                status["cached_transactions_count"] = 0
        else:
            status["cached_transactions_count"] = 0

        return jsonify(status)
    except Exception as e:
        return jsonify({"system": "error", "error": str(e), "timestamp": datetime.now().isoformat()}), 500

# --- Users ---
@app.route("/add_user", methods=["GET"])
@require_api_key
def add_user():
    try:
        card_number = request.args.get("card_number")
        user_id = request.args.get("id")
        name = request.args.get("name")

        if not card_number or not user_id or not name:
            return jsonify({"status": "error", "message": "Missing required parameters: card_number, id, name"}), 400
        if not card_number.isdigit():
            return jsonify({"status": "error", "message": "Card number must be numeric"}), 400

        user_data = {
            "id": user_id,
            "ref_id": request.args.get("ref_id", ""),
            "name": name,
            "card_number": card_number
        }

        curr = load_local_users()
        curr[card_number] = user_data
        save_local_users(curr)  # updates dict + ALLOWED_SET

        logging.info(f"User added locally: {name} (Card: {card_number})")
        return jsonify({"status": "success", "message": "User added successfully."})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Error: {str(e)}"}), 500

@app.route("/delete_user", methods=["GET"])
@require_api_key
def delete_user():
    try:
        card_number = request.args.get("card_number")
        if not card_number:
            return jsonify({"status": "error", "message": "Missing card_number"}), 400

        curr = load_local_users()
        if card_number in curr:
            user_name = curr[card_number].get("name", "Unknown")
            del curr[card_number]
            save_local_users(curr)  # updates dict + ALLOWED_SET
            logging.info(f"User deleted locally: {user_name} (Card: {card_number})")
            return jsonify({"status": "success", "message": "User deleted successfully."})
        else:
            return jsonify({"status": "error", "message": "User not found."}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": f"Error: {str(e)}"}), 500

@app.route("/search_user", methods=["GET"])
def search_user():
    try:
        user_id = request.args.get("id")
        all_users = load_local_users()
        results = [user for user in all_users.values() if user.get("id") == user_id]
        if results:
            return jsonify({"status": "success", "users": results}), 200
        else:
            return jsonify({"status": "error", "message": "User not found"}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": f"Error: {str(e)}"}), 500

# --- Relay ---
@app.route("/relay", methods=["GET"])
@require_api_key
def relay():
    try:
        action = request.args.get("action")
        relay_num = request.args.get("relay")
        if relay_num not in ["1", "2"]:
            return jsonify({"status": "error", "message": "Invalid relay number"}), 400
        relay_num = int(relay_num)
        relay_gpio = RELAY_1 if relay_num == 1 else RELAY_2

        if action in ["open_hold", "close_hold", "normal_rfid", "normal"]:
            operate_relay(action, relay_gpio)
            return jsonify({"status": "success", "message": f"Relay action '{action}' executed!"})
        return jsonify({"status": "error", "message": "Invalid action"}), 400
    except Exception as e:
        logging.error(f"Error in relay control API : {str(e)}")
        return jsonify({"status": "error", "message": f"Error: {str(e)}"}), 500

# --- Transactions ---
@app.route("/get_transactions", methods=["GET"])
def get_transactions():
    """Fetch the latest RFID access transactions from Firebase or local cache."""
    try:
        transactions = []
        if db is not None and is_internet_available():
            try:
                docs_iter = db.collection("transactions") \
                              .order_by("timestamp", direction=firestore.Query.DESCENDING) \
                              .limit(10).stream()
                docs = list(docs_iter)
            except google.api_core.exceptions.DeadlineExceeded:
                logging.warning("Firestore transaction timeout ....")
                docs = []
            except Exception as e:
                logging.error(f"Firestore get_transactions error: {e}")
                docs = []

            for doc in docs:
                tx = doc.to_dict() or {}
                transactions.append({
                    "card_number": tx.get("card_number", "N/A"),
                    "name": tx.get("name", "Unknown"),
                    "status": tx.get("status", "Unknown"),
                    "timestamp": _ts_to_epoch(tx.get("timestamp", None)),
                    "reader": tx.get("reader", "Unknown")
                })

            if transactions:
                return jsonify(transactions)

        # Offline (or no DB): serve cached if available
        cached = read_json_or_default(TRANSACTION_CACHE_FILE, [])
        if cached:
            return jsonify(cached[-10:])
        return jsonify([{"message": "No recent transactions"}])
    except Exception as e:
        return jsonify({"status": "error", "message": f"Error fetching transactions: {str(e)}"}), 500

# --- Image Management ---
@app.route("/get_images", methods=["GET"])
def get_images():
    """Get list of captured images with upload status (limited to 100 for display)."""
    try:
        images = []
        uploaded_count = 0
        pending_count = 0
        failed_count = 0
        
        if not os.path.exists(IMAGES_DIR):
            return jsonify({
                "images": [],
                "total": 0,
                "uploaded": 0,
                "pending": 0,
                "failed": 0
            })
        
        # Get all image files
        image_files = []
        for filename in os.listdir(IMAGES_DIR):
            if filename.lower().endswith('.jpg') or filename.lower().endswith('.jpeg'):
                filepath = os.path.join(IMAGES_DIR, filename)
                if os.path.isfile(filepath):
                    image_files.append((filename, filepath))
        
        # Sort by modification time (newest first)
        image_files.sort(key=lambda x: os.path.getmtime(x[1]), reverse=True)
        
        # Limit to 100 for display
        display_limit = 100
        display_files = image_files[:display_limit]
        
        for filename, filepath in display_files:
            # Extract card number and timestamp from filename
            # Format: CARD_TIMESTAMP.jpg
            try:
                name_without_ext = os.path.splitext(filename)[0]
                parts = name_without_ext.split('_')
                if len(parts) >= 2:
                    card_number = parts[0]
                    timestamp = int(parts[-1])  # Last part should be timestamp
                else:
                    card_number = "unknown"
                    timestamp = int(os.path.getmtime(filepath))
            except (ValueError, IndexError):
                card_number = "unknown"
                timestamp = int(os.path.getmtime(filepath))
            
            # Check upload status
            uploaded_sidecar = filepath + ".uploaded.json"
            uploaded = None
            s3_location = None
            
            if os.path.exists(uploaded_sidecar):
                try:
                    with open(uploaded_sidecar, 'r') as f:
                        upload_data = json.load(f)
                        uploaded = True
                        s3_location = upload_data.get('s3_location', '')
                        uploaded_count += 1
                except Exception as e:
                    logging.error(f"Error reading upload sidecar for {filename}: {e}")
                    uploaded = False
                    failed_count += 1
            else:
                uploaded = False
                pending_count += 1
            
            images.append({
                "filename": filename,
                "card_number": card_number,
                "timestamp": timestamp,
                "uploaded": uploaded,
                "s3_location": s3_location,
                "file_size": os.path.getsize(filepath)
            })
        
        # Count total images (not just displayed ones)
        total_images = len(image_files)
        
        return jsonify({
            "images": images,
            "total": total_images,
            "uploaded": uploaded_count,
            "pending": pending_count,
            "failed": failed_count,
            "display_limit": display_limit
        })
        
    except Exception as e:
        logging.error(f"Error fetching images: {e}")
        return jsonify({"status": "error", "message": f"Error fetching images: {str(e)}"}), 500

@app.route("/serve_image/<filename>")
def serve_image(filename):
    """Serve image files from the images directory."""
    try:
        # Security check - only allow jpg/jpeg files
        if not (filename.lower().endswith('.jpg') or filename.lower().endswith('.jpeg')):
            logging.warning(f"Invalid file type requested: {filename}")
            return "Invalid file type", 400
        
        # Prevent directory traversal
        if '..' in filename or '/' in filename or '\\' in filename:
            logging.warning(f"Invalid filename with path traversal: {filename}")
            return "Invalid filename", 400
        
        filepath = os.path.join(IMAGES_DIR, filename)
        logging.info(f"Serving image: {filename} from {filepath}")
        
        if not os.path.exists(filepath):
            logging.warning(f"Image not found: {filepath}")
            return "Image not found", 404
        
        from flask import send_file
        return send_file(filepath, mimetype='image/jpeg')
        
    except Exception as e:
        logging.error(f"Error serving image {filename}: {e}")
        return "Error serving image", 500

@app.route("/static/<filename>")
def serve_static(filename):
    """Serve static files from templates directory (for company images)."""
    try:
        # Security check - only allow specific image files
        allowed_extensions = ['.png', '.jpg', '.jpeg', '.gif', '.svg']
        if not any(filename.lower().endswith(ext) for ext in allowed_extensions):
            return "Invalid file type", 400
        
        # Prevent directory traversal
        if '..' in filename or '/' in filename or '\\' in filename:
            return "Invalid filename", 400
        
        # Serve from templates directory for company images
        templates_dir = os.path.join(os.path.dirname(__file__), "templates")
        filepath = os.path.join(templates_dir, filename)
        
        if not os.path.exists(filepath):
            return "File not found", 404
        
        from flask import send_file
        return send_file(filepath)
        
    except Exception as e:
        logging.error(f"Error serving static file {filename}: {e}")
        return "Error serving file", 500

@app.route("/delete_image/<filename>", methods=["DELETE"])
@require_api_key
def delete_image(filename):
    """Delete an image file and its upload sidecar."""
    try:
        # Security check - only allow jpg/jpeg files
        if not (filename.lower().endswith('.jpg') or filename.lower().endswith('.jpeg')):
            return jsonify({"status": "error", "message": "Invalid file type"}), 400
        
        # Prevent directory traversal
        if '..' in filename or '/' in filename or '\\' in filename:
            return jsonify({"status": "error", "message": "Invalid filename"}), 400
        
        filepath = os.path.join(IMAGES_DIR, filename)
        sidecar_path = filepath + ".uploaded.json"
        
        deleted_files = []
        
        if os.path.exists(filepath):
            os.remove(filepath)
            deleted_files.append(filename)
        
        if os.path.exists(sidecar_path):
            os.remove(sidecar_path)
            deleted_files.append(filename + ".uploaded.json")
        
        if deleted_files:
            return jsonify({
                "status": "success", 
                "message": f"Deleted {len(deleted_files)} file(s)",
                "deleted_files": deleted_files
            })
        else:
            return jsonify({"status": "error", "message": "File not found"}), 404
            
    except Exception as e:
        logging.error(f"Error deleting image {filename}: {e}")
        return jsonify({"status": "error", "message": f"Error deleting image: {str(e)}"}), 500

# --- User Management ---
@app.route("/get_users", methods=["GET"])
def get_users():
    """Get list of all users with blocked status."""
    try:
        users_data = load_local_users()
        blocked_data = load_blocked_users()
        users_list = []
        
        for card_number, user_data in users_data.items():
            users_list.append({
                "card_number": card_number,
                "id": user_data.get("id", ""),
                "name": user_data.get("name", ""),
                "ref_id": user_data.get("ref_id", ""),
                "blocked": blocked_data.get(card_number, False)
            })
        
        # Sort by name for better display
        users_list.sort(key=lambda x: x["name"].lower())
        
        return jsonify(users_list)
        
    except Exception as e:
        logging.error(f"Error fetching users: {e}")
        return jsonify({"status": "error", "message": f"Error fetching users: {str(e)}"}), 500

# --- Configuration Management ---
@app.route("/get_config", methods=["GET"])
def get_config():
    """Get current system configuration."""
    try:
        config = {
            "camera_username": os.getenv("CAMERA_USERNAME", "admin"),
            "camera_password": os.getenv("CAMERA_PASSWORD", "admin"),
            "camera_1_ip": os.getenv("CAMERA_1_IP", "192.168.1.201"),
            "camera_2_ip": os.getenv("CAMERA_2_IP", "192.168.1.202"),
            "camera_1_enabled": os.getenv("CAMERA_1_ENABLED", "true").lower() == "true",
            "camera_2_enabled": os.getenv("CAMERA_2_ENABLED", "true").lower() == "true",
            "s3_api_url": os.getenv("S3_API_URL", "https://api.easyparkai.com/api/Common/Upload?modulename=anpr"),
            "max_retries": int(os.getenv("MAX_RETRIES", "5")),
            "retry_delay": int(os.getenv("RETRY_DELAY", "5")),
            "bind_ip": os.getenv("BIND_IP", "192.168.1.33"),
            "bind_port": int(os.getenv("BIND_PORT", "9000")),
            "api_key": os.getenv("API_KEY", "your-api-key-change-this"),
            "scan_delay_seconds": int(os.getenv("SCAN_DELAY_SECONDS", "60"))
        }
        
        return jsonify(config)
        
    except Exception as e:
        logging.error(f"Error fetching configuration: {e}")
        return jsonify({"status": "error", "message": f"Error fetching configuration: {str(e)}"}), 500

@app.route("/update_config", methods=["POST"])
@require_api_key
def update_config():
    """Update system configuration."""
    try:
        config_data = request.get_json()
        
        if not config_data:
            return jsonify({"status": "error", "message": "No configuration data provided"}), 400
        
        # Create or update .env file
        env_file = ".env"
        env_vars = {}
        
        # Read existing .env file if it exists
        if os.path.exists(env_file):
            with open(env_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, value = line.split('=', 1)
                        env_vars[key] = value
        
        # Update with new values
        config_mapping = {
            "camera_username": "CAMERA_USERNAME",
            "camera_password": "CAMERA_PASSWORD", 
            "camera_1_ip": "CAMERA_1_IP",
            "camera_2_ip": "CAMERA_2_IP",
            "camera_1_enabled": "CAMERA_1_ENABLED",
            "camera_2_enabled": "CAMERA_2_ENABLED",
            "s3_api_url": "S3_API_URL",
            "max_retries": "MAX_RETRIES",
            "retry_delay": "RETRY_DELAY",
            "bind_ip": "BIND_IP",
            "bind_port": "BIND_PORT",
            "api_key": "API_KEY",
            "scan_delay_seconds": "SCAN_DELAY_SECONDS"
        }
        
        for key, env_key in config_mapping.items():
            if key in config_data:
                env_vars[env_key] = str(config_data[key])
                # Update rate limiter dynamically if scan_delay_seconds is changed
                if key == "scan_delay_seconds":
                    new_delay = int(config_data[key])
                    rate_limiter.delay = new_delay
                    logging.info(f"Rate limiter delay updated to {new_delay} seconds")
        
        # Write updated .env file
        with open(env_file, 'w') as f:
            for key, value in env_vars.items():
                f.write(f"{key}={value}\n")
        
        logging.info("Configuration updated successfully")
        return jsonify({"status": "success", "message": "Configuration updated successfully"})
        
    except Exception as e:
        logging.error(f"Error updating configuration: {e}")
        return jsonify({"status": "error", "message": f"Error updating configuration: {str(e)}"}), 500

# --- Storage & Analytics ---
@app.route("/get_storage_stats", methods=["GET"])
def get_storage_stats():
    """Get storage statistics and daily analytics."""
    try:
        import shutil
        
        # Get disk usage
        total, used, free = shutil.disk_usage(BASE_DIR)
        
        # Calculate image storage
        images_size = 0
        total_images = 0
        if os.path.exists(IMAGES_DIR):
            for filename in os.listdir(IMAGES_DIR):
                if filename.lower().endswith(('.jpg', '.jpeg')):
                    filepath = os.path.join(IMAGES_DIR, filename)
                    if os.path.isfile(filepath):
                        images_size += os.path.getsize(filepath)
                        total_images += 1
        
        # Calculate system files size
        system_files_size = 0
        system_files = [
            USER_DATA_FILE,
            BLOCKED_USERS_FILE,
            TRANSACTION_CACHE_FILE,
            DAILY_STATS_FILE,
            LOG_FILE
        ]
        
        for file_path in system_files:
            if os.path.exists(file_path):
                system_files_size += os.path.getsize(file_path)
        
        # Get daily statistics
        daily_stats = get_daily_stats()
        
        return jsonify({
            "total_images": total_images,
            "images_size": images_size,
            "system_files_size": system_files_size,
            "free_space": free,
            "total_space": total,
            "used_space": used,
            "daily_stats": daily_stats
        })
        
    except Exception as e:
        logging.error(f"Error getting storage stats: {e}")
        return jsonify({"status": "error", "message": f"Error getting storage stats: {str(e)}"}), 500

@app.route("/cleanup_old_images", methods=["POST"])
@require_auth
def cleanup_old_images():
    """Clean up old images based on days to keep."""
    try:
        data = request.get_json()
        days_to_keep = data.get('days_to_keep', 30)
        
        if not os.path.exists(IMAGES_DIR):
            return jsonify({"status": "success", "deleted_count": 0, "message": "No images directory found"})
        
        cutoff_time = time.time() - (days_to_keep * 24 * 60 * 60)
        deleted_count = 0
        
        for filename in os.listdir(IMAGES_DIR):
            if filename.lower().endswith(('.jpg', '.jpeg')):
                filepath = os.path.join(IMAGES_DIR, filename)
                if os.path.isfile(filepath):
                    file_mtime = os.path.getmtime(filepath)
                    if file_mtime < cutoff_time:
                        try:
                            os.remove(filepath)
                            # Also remove upload sidecar if exists
                            sidecar_path = filepath + ".uploaded.json"
                            if os.path.exists(sidecar_path):
                                os.remove(sidecar_path)
                            deleted_count += 1
                        except Exception as e:
                            logging.error(f"Error deleting {filepath}: {e}")
        
        logging.info(f"Cleaned up {deleted_count} old images")
        return jsonify({
            "status": "success",
            "deleted_count": deleted_count,
            "message": f"Cleaned up {deleted_count} old images"
        })
        
    except Exception as e:
        logging.error(f"Error cleaning up old images: {e}")
        return jsonify({"status": "error", "message": f"Error cleaning up images: {str(e)}"}), 500

@app.route("/cleanup_old_stats", methods=["POST"])
@require_auth
def cleanup_old_stats():
    """Clean up statistics older than 20 days."""
    try:
        stats = read_json_or_default(DAILY_STATS_FILE, {})
        cutoff_date = datetime.now() - timedelta(days=20)
        cutoff_str = cutoff_date.strftime('%Y-%m-%d')
        
        old_dates = [date for date in stats.keys() if date < cutoff_str]
        deleted_count = len(old_dates)
        
        for date in old_dates:
            del stats[date]
        
        if old_dates:
            atomic_write_json(DAILY_STATS_FILE, stats)
        
        logging.info(f"Cleaned up {deleted_count} old daily statistics")
        return jsonify({
            "status": "success",
            "deleted_count": deleted_count,
            "message": f"Cleaned up {deleted_count} old statistics"
        })
        
    except Exception as e:
        logging.error(f"Error cleaning up old stats: {e}")
        return jsonify({"status": "error", "message": f"Error cleaning up stats: {str(e)}"}), 500

@app.route("/clear_all_stats", methods=["POST"])
@require_auth
def clear_all_stats():
    """Clear all daily statistics."""
    try:
        if os.path.exists(DAILY_STATS_FILE):
            os.remove(DAILY_STATS_FILE)
        
        logging.info("Cleared all daily statistics")
        return jsonify({
            "status": "success",
            "message": "All statistics cleared"
        })
        
    except Exception as e:
        logging.error(f"Error clearing all stats: {e}")
        return jsonify({"status": "error", "message": f"Error clearing stats: {str(e)}"}), 500

# --- Transaction Sync Management ---
@app.route("/sync_transactions", methods=["POST"])
@require_api_key
def manual_sync_transactions():
    """Manually trigger transaction sync."""
    try:
        if not is_internet_available():
            return jsonify({"status": "error", "message": "No internet connection"}), 400
        
        if db is None:
            return jsonify({"status": "error", "message": "Firebase not available"}), 400
        
        # Check cache file status
        if not os.path.exists(TRANSACTION_CACHE_FILE):
            return jsonify({"status": "success", "message": "No cached transactions to sync"})
        
        cached_txns = read_json_or_default(TRANSACTION_CACHE_FILE, [])
        if not cached_txns:
            return jsonify({"status": "success", "message": "No cached transactions to sync"})
        
        # Trigger sync
        sync_transactions()
        
        # Check remaining transactions
        remaining_txns = read_json_or_default(TRANSACTION_CACHE_FILE, [])
        
        if remaining_txns:
            return jsonify({
                "status": "partial", 
                "message": f"Synced some transactions, {len(remaining_txns)} still pending",
                "remaining_count": len(remaining_txns)
            })
        else:
            return jsonify({
                "status": "success", 
                "message": f"All {len(cached_txns)} transactions synced successfully"
            })
            
    except Exception as e:
        logging.error(f"Error in manual sync: {e}")
        return jsonify({"status": "error", "message": f"Error syncing transactions: {str(e)}"}), 500

@app.route("/transaction_cache_status", methods=["GET"])
def transaction_cache_status():
    """Get status of cached transactions."""
    try:
        if not os.path.exists(TRANSACTION_CACHE_FILE):
            return jsonify({
                "status": "success",
                "cached_count": 0,
                "message": "No cached transactions"
            })
        
        cached_txns = read_json_or_default(TRANSACTION_CACHE_FILE, [])
        return jsonify({
            "status": "success",
            "cached_count": len(cached_txns),
            "message": f"{len(cached_txns)} transactions cached"
        })
        
    except Exception as e:
        logging.error(f"Error checking cache status: {e}")
        return jsonify({"status": "error", "message": f"Error checking cache: {str(e)}"}), 500

# --- Offline Images Management ---
@app.route("/get_offline_images", methods=["GET"])
def get_offline_images():
    """Get all offline images with reader information."""
    try:
        images = []
        
        if not os.path.exists(IMAGES_DIR):
            return jsonify({"images": []})
        
        for filename in os.listdir(IMAGES_DIR):
            if filename.lower().endswith(('.jpg', '.jpeg')):
                filepath = os.path.join(IMAGES_DIR, filename)
                if os.path.isfile(filepath):
                    try:
                        # Extract card number, reader, and timestamp from filename
                        name_without_ext = os.path.splitext(filename)[0]
                        parts = name_without_ext.split('_')
                        
                        if len(parts) >= 3:
                            # New format: card_reader_timestamp
                            card_number = parts[0]
                            reader_str = parts[1]
                            timestamp = int(parts[2])
                            
                            # Extract reader number from "r1" or "r2"
                            if reader_str.startswith('r'):
                                reader = int(reader_str[1:])
                            else:
                                reader = 1  # fallback
                        elif len(parts) >= 2:
                            # Old format: card_timestamp (backward compatibility)
                            card_number = parts[0]
                            timestamp = int(parts[-1])
                            reader = 1  # default to reader 1 for old format
                        else:
                            card_number = "unknown"
                            timestamp = int(os.path.getmtime(filepath))
                            reader = 1
                        
                        # Check upload status
                        uploaded_sidecar = filepath + ".uploaded.json"
                        uploaded = None
                        s3_location = None
                        
                        if os.path.exists(uploaded_sidecar):
                            try:
                                with open(uploaded_sidecar, 'r') as f:
                                    upload_data = json.load(f)
                                    uploaded = True
                                    s3_location = upload_data.get('s3_location', '')
                            except Exception as e:
                                logging.error(f"Error reading upload sidecar for {filename}: {e}")
                                uploaded = False
                        else:
                            uploaded = False
                        
                        images.append({
                            "filename": filename,
                            "card_number": card_number,
                            "timestamp": timestamp,
                            "reader": reader,
                            "uploaded": uploaded,
                            "s3_location": s3_location,
                            "file_size": os.path.getsize(filepath)
                        })
                        
                    except Exception as e:
                        logging.error(f"Error processing image {filename}: {e}")
                        continue
        
        # Sort by timestamp (newest first)
        images.sort(key=lambda x: x['timestamp'], reverse=True)
        
        return jsonify({"images": images})
        
    except Exception as e:
        logging.error(f"Error fetching offline images: {e}")
        return jsonify({"status": "error", "message": f"Error fetching offline images: {str(e)}"}), 500

@app.route("/clear_all_offline_images", methods=["POST"])
@require_api_key
def clear_all_offline_images():
    """Clear all offline images."""
    try:
        if not os.path.exists(IMAGES_DIR):
            return jsonify({"status": "success", "deleted_count": 0, "message": "No images directory found"})
        
        deleted_count = 0
        
        for filename in os.listdir(IMAGES_DIR):
            if filename.lower().endswith(('.jpg', '.jpeg')):
                filepath = os.path.join(IMAGES_DIR, filename)
                if os.path.isfile(filepath):
                    try:
                        os.remove(filepath)
                        # Also remove upload sidecar if exists
                        sidecar_path = filepath + ".uploaded.json"
                        if os.path.exists(sidecar_path):
                            os.remove(sidecar_path)
                        deleted_count += 1
                    except Exception as e:
                        logging.error(f"Error deleting {filepath}: {e}")
        
        logging.info(f"Cleared {deleted_count} offline images")
        return jsonify({
            "status": "success",
            "deleted_count": deleted_count,
            "message": f"Cleared {deleted_count} offline images"
        })
        
    except Exception as e:
        logging.error(f"Error clearing offline images: {e}")
        return jsonify({"status": "error", "message": f"Error clearing images: {str(e)}"}), 500

@app.route("/get_storage_info", methods=["GET"])
def get_storage_info():
    """Get storage information for images."""
    try:
        current_usage = get_storage_usage()
        
        # Get dynamic storage limits based on available free space
        max_storage_gb, cleanup_threshold_gb = get_dynamic_storage_limits()
        max_bytes = max_storage_gb * 1024 * 1024 * 1024
        cleanup_bytes = cleanup_threshold_gb * 1024 * 1024 * 1024
        
        # Get disk usage information
        disk_info = get_disk_usage()
        disk_total_gb = disk_info['total'] / (1024**3) if disk_info else 0
        disk_free_gb = disk_info['free'] / (1024**3) if disk_info else 0
        disk_used_gb = disk_info['used'] / (1024**3) if disk_info else 0
        
        usage_gb = current_usage / (1024**3)
        usage_percentage = (current_usage / max_bytes) * 100 if max_bytes > 0 else 0
        
        return jsonify({
            "current_usage_gb": round(usage_gb, 2),
            "max_storage_gb": max_storage_gb,
            "cleanup_threshold_gb": cleanup_threshold_gb,
            "usage_percentage": round(usage_percentage, 1),
            "current_usage_bytes": current_usage,
            "max_storage_bytes": max_bytes,
            "cleanup_threshold_bytes": cleanup_bytes,
            "disk_total_gb": round(disk_total_gb, 2),
            "disk_free_gb": round(disk_free_gb, 2),
            "disk_used_gb": round(disk_used_gb, 2),
            "allocation_percentage": 60,  # 60% of free space allocated to images
            "cleanup_percentage": 30      # 30% of allocated space cleaned up
        })
        
    except Exception as e:
        logging.error(f"Error getting storage info: {e}")
        return jsonify({"status": "error", "message": f"Error getting storage info: {str(e)}"}), 500

@app.route("/trigger_storage_cleanup", methods=["POST"])
@require_api_key
def trigger_storage_cleanup():
    """Manually trigger storage cleanup."""
    try:
        cleanup_old_images()
        current_usage = get_storage_usage()
        max_storage_gb, cleanup_threshold_gb = get_dynamic_storage_limits()
        
        return jsonify({
            "status": "success",
            "message": "Storage cleanup completed",
            "current_usage_gb": round(current_usage / (1024**3), 2),
            "max_storage_gb": max_storage_gb,
            "cleanup_threshold_gb": cleanup_threshold_gb
        })
        
    except Exception as e:
        logging.error(f"Error triggering storage cleanup: {e}")
        return jsonify({"status": "error", "message": f"Error triggering cleanup: {str(e)}"}), 500

@app.route("/system_reset", methods=["POST"])
@require_api_key
def system_reset():
    """Restart the entire application."""
    try:
        logging.info("System reset requested by user")
        
        # Schedule restart after a short delay to allow response
        def delayed_restart():
            time.sleep(2)  # Give time for response to be sent
            logging.info("Restarting application...")
            
            try:
                # Try to restart using subprocess
                import subprocess
                import sys
                
                # Get the current script path
                script_path = os.path.abspath(__file__)
                python_executable = sys.executable
                script_dir = os.path.dirname(script_path)
                
                # Try using the restart script first
                restart_script = os.path.join(script_dir, 'restart_rfid.py')
                if os.path.exists(restart_script):
                    logging.info("Using restart script for graceful restart")
                    subprocess.Popen([python_executable, restart_script], 
                                   cwd=script_dir,
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
                    time.sleep(2)
                    os._exit(0)
                else:
                    # Fallback to direct restart
                    logging.info("Using direct restart method")
                    subprocess.Popen([python_executable, script_path], 
                                   cwd=script_dir,
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
                    time.sleep(1)
                    os._exit(0)
                
            except Exception as restart_error:
                logging.error(f"Error during restart: {restart_error}")
                # Fallback to simple exit
                os._exit(0)
        
        # Start restart in background thread
        threading.Thread(target=delayed_restart, daemon=True).start()
        
        return jsonify({
            "status": "success",
            "message": "System restart initiated. The application will restart in a few seconds."
        })
        
    except Exception as e:
        logging.error(f"Error during system reset: {e}")
        return jsonify({"status": "error", "message": f"Error during reset: {str(e)}"}), 500

# --- System Health Check ---
@app.route("/health_check", methods=["GET"])
def health_check():
    """Check system health including cameras, internet, and Firebase."""
    try:
        health_status = {
            "internet": False,
            "camera_1": False,
            "camera_2": False,
            "firebase": False
        }
        
        # Check internet connectivity
        health_status["internet"] = is_internet_available()
        
        # Check Firebase connection
        health_status["firebase"] = db is not None and is_internet_available()
        
        # Check camera connectivity (only if enabled)
        camera_1_enabled = os.getenv("CAMERA_1_ENABLED", "true").lower() == "true"
        camera_2_enabled = os.getenv("CAMERA_2_ENABLED", "true").lower() == "true"
        
        health_status["camera_1"] = check_camera_health("camera_1") if camera_1_enabled else None
        health_status["camera_2"] = check_camera_health("camera_2") if camera_2_enabled else None
        
        return jsonify(health_status)
        
    except Exception as e:
        logging.error(f"Error checking system health: {e}")
        return jsonify({
            "internet": False,
            "camera_1": False,
            "camera_2": False,
            "firebase": False,
            "error": str(e)
        }), 500

def check_camera_health(camera_key):
    """Check if a specific camera is accessible."""
    try:
        rtsp_url = RTSP_CAMERAS.get(camera_key)
        if not rtsp_url:
            return False
        
        # Try to open the camera stream
        cap = cv2.VideoCapture(rtsp_url)
        if cap.isOpened():
            # Try to read a frame
            ret, frame = cap.read()
            cap.release()
            return ret and frame is not None
        else:
            return False
            
    except Exception as e:
        logging.error(f"Error checking camera {camera_key}: {e}")
        return False

# --- Block/Unblock ---
@app.route("/block_user", methods=["GET"])
@require_api_key
def block_user():
    try:
        card_number = request.args.get("card_number")
        if not card_number:
            return jsonify({"status": "error", "message": "Missing card_number"}), 400

        curr = load_blocked_users()
        curr[card_number] = True
        save_blocked_users(curr)  # updates dict + BLOCKED_SET

        logging.info(f"User blocked locally: Card {card_number}")
        return jsonify({"status": "success", "message": f"User {card_number} blocked successfully."})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Error blocking user: {str(e)}"}), 500

@app.route("/unblock_user", methods=["GET"])
@require_api_key
def unblock_user():
    try:
        card_number = request.args.get("card_number")
        if not card_number:
            return jsonify({"status": "error", "message": "Missing card_number"}), 400

        curr = load_blocked_users()
        if card_number in curr:
            curr.pop(card_number, None)
            save_blocked_users(curr)  # updates dict + BLOCKED_SET

            logging.info(f"User unblocked locally: Card {card_number}")
            return jsonify({"status": "success", "message": f"User {card_number} unblocked successfully."})
        else:
            return jsonify({"status": "error", "message": "User is not blocked."}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": f"Error unblocking user: {str(e)}"}), 500

# =========================
# Firestore listeners (attach once)
# =========================
_listeners = {"users": False, "blocked": False}

def sync_users_from_firebase():
    """Real-time Firebase updates -> keep users.json and in-memory users fresh."""
    global _listeners
    if db is None or _listeners["users"] or not is_internet_available():
        return
    try:
        users_ref = db.collection("users")

        def on_snapshot(col_snapshot, changes, read_time):
            try:
                local = load_local_users()
                changed = False
                for change in changes:
                    doc = change.document.to_dict() or {}
                    if "card_number" not in doc:
                        doc["card_number"] = change.document.id
                    card_number = doc["card_number"]

                    if change.type.name in ("ADDED", "MODIFIED"):
                        local[card_number] = doc
                        changed = True
                        logging.info(f"User {doc.get('name', 'Unknown')} (Card: {card_number}) added/updated.")
                    elif change.type.name == "REMOVED":
                        if card_number in local:
                            local.pop(card_number, None)
                            changed = True
                            logging.info(f"User with Card {card_number} removed.")

                if changed:
                    save_local_users(local)  # refresh ALLOWED_SET
            except Exception as e:
                logging.error(f"Error in Firebase user snapshot callback: {str(e)}")

        users_ref.on_snapshot(on_snapshot)
        _listeners["users"] = True
        logging.info("Listening for real-time Firebase user updates (attached once).")
    except google.api_core.exceptions.DeadlineExceeded:
        logging.warning("Firestore transaction timeout during user sync setup")
    except Exception as e:
        logging.error(f"Error setting up real-time Firebase sync: {str(e)}")

def sync_blocked_users_from_firebase():
    """Real-time Firebase updates to blocked flag -> update local blocked_users."""
    global _listeners
    if db is None or _listeners["blocked"] or not is_internet_available():
        return
    try:
        blocked_users_ref = db.collection("users")

        def on_snapshot(col_snapshot, changes, read_time):
            try:
                local = load_blocked_users()
                changed = False
                for change in changes:
                    doc = change.document.to_dict() or {}
                    card_number = change.document.id
                    if "blocked" in doc:
                        if doc["blocked"]:
                            if not local.get(card_number):
                                local[card_number] = True
                                changed = True
                                logging.info(f"User {card_number} blocked via Firebase.")
                        else:
                            if local.pop(card_number, None) is not None:
                                changed = True
                                logging.info(f"User {card_number} unblocked via Firebase.")
                if changed:
                    save_blocked_users(local)  # refresh BLOCKED_SET
            except Exception as e:
                logging.error(f"Error in Firebase blocked snapshot callback: {str(e)}")

        blocked_users_ref.on_snapshot(on_snapshot)
        _listeners["blocked"] = True
        logging.info("Listening for real-time Firebase blocked user updates (attached once).")
    except google.api_core.exceptions.DeadlineExceeded:
        logging.warning("Firestore transaction timeout during blocked user sync")
    except Exception as e:
        logging.error(f"Error setting up Firebase blocked user sync: {str(e)}")

# =========================
# Access handling
# =========================
recent_transactions = []
wiegand1 = None
wiegand2 = None

def operate_relay(action, relay):
    global relay_status
    try:
        if not hasattr(GPIO, 'output'):
            logging.warning("GPIO not available. Relay operation skipped.")
            return

        if action == "open_hold":
            GPIO.output(relay, GPIO.LOW)
            relay_status = 1   # OPEN HOLD
            logging.info(f"Relay {relay} opened (hold)")
        elif action == "close_hold":
            GPIO.output(relay, GPIO.HIGH)
            relay_status = 2   # CLOSE HOLD
            logging.info(f"Relay {relay} closed (hold)")
        elif action == "normal":
            relay_status = 0
            logging.info(f"Relay {relay} set to normal mode")
        elif action == "normal_rfid":
            relay_status = 0
            GPIO.output(relay, GPIO.LOW)
            time.sleep(1)  # NOTE: runs in separate thread (see below)
            GPIO.output(relay, GPIO.HIGH)
            logging.info(f"Relay {relay} pulsed (normal RFID)")
        else:
            logging.warning(f"Invalid relay action received: {action}")
    except Exception as e:
        logging.error(f"Error setting relay {relay}: {str(e)}")

def handle_access(bits, value, reader_id):
    """Handle Wiegand 26-bit read -> O(1) set lookups, immediate local decisions, async image capture."""
    try:
        global relay_status
        if bits != 26:
            logging.warning(f"Invalid Wiegand bits received: {bits} from reader {reader_id}")
            return

        # 26-bit with parity removal (bits 1..24) -> int
        card_int = int(f"{value:026b}"[1:25], 2)

        if not rate_limiter.should_process(card_int):
            logging.info(f"Duplicate scan ignored: {card_int}")
            return

        print(f"Scanned Card from Reader {reader_id}: {card_int}")
        timestamp = int(time.time())
        relay = RELAY_1 if reader_id == 1 else RELAY_2

        # O(1) lookups using sets
        with BLOCKED_SET_LOCK:
            is_blocked = card_int in BLOCKED_SET
        with ALLOWED_SET_LOCK:
            is_allowed = card_int in ALLOWED_SET

        if is_blocked:
            status = "Blocked"
            name = "Blocked User"
        elif is_allowed:
            with USERS_LOCK:
                u = users.get(str(card_int))
                name = u.get("name", "Unknown") if u else "Unknown"
            status = "Access Granted"
            if relay_status == 0:
                # Offload relay pulse to avoid blocking pigpio callback thread
                threading.Thread(target=operate_relay, args=("normal_rfid", relay), daemon=True).start()
        else:
            status = "Access Denied"
            name = "Unknown"

        # === NON-BLOCKING CAMERA CAPTURE ===
        # Capture image in the background; name format: CARD_TIMESTAMP.jpg
        camera_executor.submit(capture_for_reader_async, reader_id, card_int)

        transaction = {
            "card_number": str(card_int),
            "name": name,
            "status": status,
            "timestamp": timestamp,
            "reader": reader_id
        }

        # Update daily statistics
        update_daily_stats(status)

        try:
            transaction_queue.put(transaction)
        except Exception as e:
            logging.error(f"Queue error for card {card_int}: {str(e)}")

        recent_transactions.append(transaction)
        if len(recent_transactions) > 10:
            recent_transactions.pop(0)

    except Exception as e:
        logging.error(f"Unexpected error in handle_access for reader {reader_id}: {str(e)}")

def transaction_uploader():
    """Background worker to upload/cache transactions."""
    while True:
        transaction = transaction_queue.get()
        try:
            if is_internet_available() and db is not None:
                try:
                    db.collection("transactions").add(transaction)
                    logging.info(f"Transaction uploaded: {transaction}")
                except Exception as e:
                    logging.error(f"Error uploading transaction: {str(e)}")
                    cache_transaction(transaction)
            else:
                logging.warning("No internet/Firebase unavailable. Transaction cached.")
                cache_transaction(transaction)
        finally:
            transaction_queue.task_done()

def image_uploader_worker():
    """
    Background worker to upload images to S3 with NO impact on scan latency.
    Uses ImageUploader (your module). Writes *.uploaded.json sidecar on success.
    """
    uploader = ImageUploader()  # :contentReference[oaicite:6]{index=6}
    while True:
        filepath = image_queue.get()
        try:
            if not os.path.exists(filepath):
                image_queue.task_done()
                continue

            if not is_internet_available():
                # Requeue later by simply skipping; sync_loop will enqueue again when online
                time.sleep(5)
                image_queue.task_done()
                continue

            if _has_uploaded_sidecar(filepath):
                # already uploaded
                image_queue.task_done()
                continue

            location = uploader.upload(filepath)
            if location:
                _mark_uploaded(filepath, location)
                logging.info(f"[UPLOAD] OK: {filepath} -> {location}")
            else:
                logging.warning(f"[UPLOAD] Failed: {filepath} (will retry later)")

        except Exception as e:
            logging.error(f"[UPLOAD] Worker error: {e}")
        finally:
            image_queue.task_done()

def enqueue_pending_images(limit=50):
    """
    Scan IMAGES_DIR for .jpg without .uploaded.json and enqueue for upload.
    Called from sync loop only when online.
    """
    try:
        count = 0
        for name in os.listdir(IMAGES_DIR):
            if not name.lower().endswith(".jpg"):
                continue
            fp = os.path.join(IMAGES_DIR, name)
            if not _has_uploaded_sidecar(fp):
                image_queue.put(fp)
                count += 1
                if count >= limit:
                    break
        if count:
            logging.info(f"[UPLOAD] Enqueued {count} pending images for upload")
    except Exception as e:
        logging.error(f"enqueue_pending_images error: {e}")

def check_relay_status():
    """Monitor relay control from Firebase (polled)."""
    if db is None:
        return
    try:
        doc = db.collection("relay_control").document("status").get()
        if doc.exists:
            data = doc.to_dict() or {}
            action = data.get("action", "normal")
            relay = data.get("relay", None)
            if relay == "RELAY_1":
                relay_gpio = RELAY_1
            elif relay == "RELAY_2":
                relay_gpio = RELAY_2
            else:
                logging.warning(f"Invalid relay identifier: {relay}")
                return
            if action in ["open_hold", "close_hold", "normal_rfid", "normal"]:
                operate_relay(action, relay_gpio)
    except google.api_core.exceptions.DeadlineExceeded:
        logging.warning("Firestore transaction timeout during relay status check")
    except Exception as e:
        logging.error(f"Error fetching relay status from Firebase: {str(e)}")

def check_user_status():
    """Monitor user control from Firebase."""
    if db is None:
        return
    try:
        doc = db.collection("user_control").document("status").get()
        if doc.exists:
            action = doc.to_dict().get("action", "normal")
            if action == "updated":
                try:
                    sync_users_from_firebase()
                    db.collection("user_control").document("status").update({"action": "normal"})
                    logging.info("User data updated from Firebase")
                except Exception as e:
                    logging.error(f"Error updating user data: {str(e)}")
    except google.api_core.exceptions.DeadlineExceeded:
        logging.warning("Firestore transaction timeout during user status check")
    except Exception as e:
        logging.error(f"Error checking user status: {str(e)}")

def sync_loop():
    """Background loop: attach listeners once, poll controls, sync offline txns, and handle image uploads."""
    # Attach listeners once when online
    sync_users_from_firebase()
    sync_blocked_users_from_firebase()
    while True:
        try:
            if is_internet_available():
                try:
                    check_relay_status()
                    check_user_status()
                    sync_transactions()
                    enqueue_pending_images(limit=50)  # opportunistic image uploads
                except Exception as e:
                    logging.error(f"Error in Firebase/image sync operations: {str(e)}")
            else:
                logging.debug("No internet connection. Skipping Firebase & image upload sync.")
            sync_interval = int(os.environ.get('SYNC_INTERVAL', 60))
            time.sleep(sync_interval)
        except Exception as e:
            logging.error(f"Error in sync loop: {str(e)}")
            time.sleep(5)

def restart_program():
    logging.error("Critical failure! Restarting the program...")
    python = sys.executable
    os.execl(python, python, *sys.argv)

def cleanup():
    """Cleanup function for graceful shutdown"""
    logging.info("Starting cleanup...")
    try:
        # Cleanup Wiegand readers
        if wiegand1 is not None:
            try:
                wiegand1.cancel()
                logging.info("Wiegand reader 1 stopped")
            except Exception as e:
                logging.error(f"Error stopping wiegand1: {str(e)}")

        if wiegand2 is not None:
            try:
                wiegand2.cancel()
                logging.info("Wiegand reader 2 stopped")
            except Exception as e:
                logging.error(f"Error stopping wiegand2: {str(e)}")

        # Cleanup pigpio
        if pi is not None:
            try:
                pi.stop()
                logging.info("Pigpio stopped")
            except Exception as e:
                logging.error(f"Error stopping pigpio: {str(e)}")

        # Cleanup GPIO
        try:
            GPIO.cleanup()
            logging.info("GPIO cleanup completed")
        except Exception as e:
            logging.error(f"Error during GPIO cleanup: {str(e)}")

    except Exception as e:
        logging.error(f"Error during cleanup: {str(e)}")

# =========================
# Startup
# =========================
if pi is not None:
    try:
        print("Readers initialised successfully")
        print(pi)
        wiegand1 = WiegandDecoder(pi, D0_PIN_1, D1_PIN_1, lambda b, v: handle_access(b, v, 1))
        wiegand2 = WiegandDecoder(pi, D0_PIN_2, D1_PIN_2, lambda b, v: handle_access(b, v, 2))
        # Initialize in-memory stores + sets at boot
        load_local_users()
        load_blocked_users()
        print("Readers initialised successfully")
        logging.info("RFID readers initialized successfully.")
    except Exception as e:
        logging.error(f"Error initializing RFID readers: {str(e)}")
        wiegand1 = None
        wiegand2 = None
else:
    logging.warning("Pigpio not available. RFID readers will be disabled.")

# Background threads
threading.Thread(target=sync_loop, daemon=True).start()
threading.Thread(target=transaction_uploader, daemon=True).start()
threading.Thread(target=image_uploader_worker, daemon=True).start()
threading.Thread(target=session_cleanup_worker, daemon=True).start()
threading.Thread(target=daily_stats_cleanup_worker, daemon=True).start()
threading.Thread(target=storage_monitor_worker, daemon=True).start()

# Flask serve
try:
    print("Waiting for RFID card scans...")
    flask_host = os.environ.get('FLASK_HOST', '0.0.0.0')
    flask_port = int(os.environ.get('FLASK_PORT', 5001))
    flask_debug = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    app.run(host=flask_host, port=flask_port, debug=flask_debug)
except KeyboardInterrupt:
    print("\nStopping Wiegand readers...")
    cleanup()
except Exception as e:
    logging.error(f"Unexpected error: {str(e)}")
    cleanup()
finally:
    cleanup()
