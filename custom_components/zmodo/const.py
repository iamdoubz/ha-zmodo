"""Constants for the Zmodo integration."""

DOMAIN = "zmodo"

# App-style login endpoint (no captcha required)
API_APP_LOGIN_PATH = "/user/user_login"
API_REFRESH_LOGIN_PATH = "/user/refresh_login"

# Bootstrap hosts tried in order (mirrors iOS app behaviour)
APP_MOP_HOSTS = [
    "https://11-app-mop.meshare.com",
    "https://12-app-mop.meshare.com",
]

DEVICE_LIST_PATH = "/device/device_list"
DEVICE_STORAGE_LIST_PATH = "/device/storage_list"
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
LOGIN_CLIENT_VERSION = "7.0.2"  # client_version sent in refresh

# Config entry keys
CONF_TOKEN = "token"
CONF_USER_ID = "user_id"
CONF_LOGIN_CERT = "login_cert"          # long-lived refresh credential
CONF_CLIENT_UUID = "client_uuid"        # stable per-install UUID
CONF_APP_ADDRESSES = "app_addresses"
CONF_ALARM_ADDRESSES = "alarm_addresses"
CONF_MNG_ADDRESSES = "mng_addresses"

# Coordinator update interval (seconds)
UPDATE_INTERVAL = 5

# Alert fetch window (seconds): 24 hours
ALERT_WINDOW_SECONDS = 86400

# Camera stream base URL
STREAM_BASE_URL = "https://flv.meshare.com/live"

# media_type values
STREAM_MEDIA_TYPE_SD = 1
STREAM_MEDIA_TYPE_HD = 2

