"""Constants for the Zmodo integration."""

DOMAIN = "zmodo"

# App-style login endpoint (no captcha required)
API_APP_LOGIN_PATH = "/user/user_login"
API_REFRESH_LOGIN_PATH = "/user/refresh_login"

# Bootstrap hosts tried in order (mirrors iOS app behaviour)
APP_MOP_HOSTS = [
    "https://11-app-mop.meshare.com",
    "https://12-app-mop.meshare.com",
    "https://11-app-mop.iotek.ai",
    "https://12-app-mop.iotek.ai",
]

DEVICE_LIST_PATH = "/device/device_list"
DEVICE_STORAGE_LIST_PATH = "/device/storage_list"
DEVICE_MODIFY_PATH = "/device/device_modify"
DEVICE_MUTE_PATH = "/device/set_mute"
ALARM_SEARCH_PATH = "/message/search"

# Notification mode endpoints (on app-mop hosts)
NOTIFICATION_GET_PATH = "/mode/user_config_get"
NOTIFICATION_SET_PATH = "/mode/user_config_set"

# Notification mode values
NOTIFICATION_MODE_ON = "0"
NOTIFICATION_MODE_OFF = "1"

# Login form constants (app-style, mirroring iOS app)
LOGIN_CID = "0"
LOGIN_PLATFORM = "2"
LOGIN_CLIENT = "1"          # iOS app sends client=1; web sends client=2
LOGIN_LANGUAGE = "en"
LOGIN_APP_VERSION = "5.0"   # app_version field sent by iOS app
LOGIN_CLIENT_VERSION = "8.0.6"  # client_version sent in login/refresh (app 8.0.6, Oct 2026)

# Config entry keys
CONF_TOKEN = "token"
CONF_USER_ID = "user_id"
CONF_LOGIN_CERT = "login_cert"          # long-lived refresh credential
CONF_CLIENT_UUID = "client_uuid"        # stable per-install UUID
CONF_APP_ADDRESSES = "app_addresses"
CONF_ALARM_ADDRESSES = "alarm_addresses"
CONF_MNG_ADDRESSES = "mng_addresses"

CONF_PLATFORM_USER_ID = "platform_user_id"

# Per-camera local LAN streaming (options). Maps serial -> {"host": ip}.
# When a host is set the live hub streams directly from the camera on port 8000
# (~1 s latency) instead of the cloud, and an HD entity (1080p mainstream, local
# only) appears alongside the SD one; the SD entity's cloud path is the fallback.
CONF_LOCAL_STREAMS = "local_streams"
OPT_LOCAL_HOST = "host"

# New platform API (user.zmodo.com)
ALERT_FETCH_LIMIT = 100          # newest N alerts per poll; 24h count is derived from these
ALERT_COUNT_WINDOW_SECONDS = 86400

# Legacy MeShare API back-off when its login fails (seconds)
LEGACY_RETRY_SECONDS = 600

# Live relay: cached still image lifetime (seconds)
SNAPSHOT_CACHE_SECONDS = 30

# Coordinator update interval (seconds)
UPDATE_INTERVAL = 5

# Alert fetch window (seconds): 24 hours
ALERT_WINDOW_SECONDS = 86400


