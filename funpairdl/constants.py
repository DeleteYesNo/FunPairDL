from pathlib import Path

APP_NAME = "FunPairDL"
APP_VERSION = "0.1.0"

# Default paths
DEFAULT_DOWNLOAD_DIR = Path("G:/Download/nakk7472")
CONFIG_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = CONFIG_DIR / "config.json"
QUEUE_FILE = CONFIG_DIR / "queue.json"
LOG_FILE = CONFIG_DIR / "funpairdl.log"

# Download settings
DEFAULT_SEGMENTS = 16
MIN_SEGMENT_SIZE = 1 * 1024 * 1024  # 1MB - don't segment smaller files
MAX_SEGMENTS = 16
SMALL_FILE_THRESHOLD = 10 * 1024 * 1024  # 10MB - use single segment

# API server
DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 9172

# Progress update interval
PROGRESS_UPDATE_INTERVAL = 0.2  # seconds

# Retry settings
MAX_RETRIES = 5
RETRY_BASE_DELAY = 2.0  # seconds

# Segment-level retry (round-based: only failed segments are retried each round)
SEGMENT_MAX_ROUNDS = 6       # total download rounds per item
SEGMENT_RETRY_DELAYS = [2, 5, 10, 20, 30]  # backoff (seconds) between rounds
SEGMENT_SOCK_READ = 90       # seconds — per-read timeout for segment HTTP streams

# Chunk size for reading/writing
CHUNK_SIZE = 256 * 1024  # 256KB — larger chunks reduce asyncio context switches

# Browser-like User-Agent for authenticated requests.
# Must match QWebEngine's UA to avoid Discourse session invalidation.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Known video hosts
VIDEO_HOSTS = [
    "pixeldrain.com",
    "mega.nz",
    "mega.co.nz",
    "iwara.tv",
    "rule34video.com",
    "rule34.xxx",
    "hanime1.me",
    "hmvmania.com",
]

# Known script extensions
SCRIPT_EXTENSIONS = [".funscript"]

# Known video extensions
VIDEO_EXTENSIONS = [
    ".mp4", ".mkv", ".avi", ".webm", ".mov",
    ".wmv", ".flv", ".m4v", ".ts", ".m3u8",
]
