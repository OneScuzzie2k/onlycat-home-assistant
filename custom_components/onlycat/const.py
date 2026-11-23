"""Constants for OnlyCat."""

from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

DOMAIN = "onlycat"
ATTRIBUTION = ""
CONF_API_KEY = "api_key"
CONF_IGNORE_FLAP_MOTION_RULES = "ignore_flap_motion_rules"
CONF_IGNORE_MOTION_SENSOR_RULES = "ignore_motion_sensor_rules"
CONF_POLL_INTERVAL_HOURS = "poll_interval_hours"
CONF_ENABLE_DETAILED_METRICS = "enable_detailed_metrics"
CONF_ENABLE_DEBUG_LOGGING = "enable_debug_logging"
CONF_EVENT_RETRY_DELAYS_SECONDS = "event_retry_delays_seconds"
CONF_FINAL_EVENT_RETRY_DELAYS_SECONDS = "final_event_retry_delays_seconds"
SYNC_MONITOR_INTERVAL_SECONDS = 5 * 60
ONLYCAT_ACTIVITY_EVENT = "onlycat_activity"
ACTIVITY_STORE_VERSION = 2
ACTIVITY_DEDUP_MAX_ENTRIES = 512
ACTIVITY_JOURNAL_MAX_ENTRIES = 250
ACTIVITY_JOURNAL_MAX_AGE_DAYS = 7
ACTIVITY_JOURNAL_DEFAULT_PAGE_SIZE = 50
ACTIVITY_JOURNAL_MAX_PAGE_SIZE = 100
ACTIVITY_JOURNAL_WS_COMMAND = "onlycat/activity_journal"
ACTIVITY_IMAGE_URL = (
    "/api/onlycat/activity_image/{config_entry_id}/{device_id}/{event_id}"
)
ACTIVITY_IMAGE_SIGNED_URL_TTL_SECONDS = 5 * 60
ACTIVITY_VIDEO_URL = (
    "/api/onlycat/activity_video/{config_entry_id}/{device_id}/{event_id}"
)
ACTIVITY_VIDEO_STATUS_WS_COMMAND = "onlycat/activity_video_status"
ACTIVITY_VIDEO_SIGNED_URL_TTL_SECONDS = 5 * 60
RECONNECT_EVENT_RECONCILE_LIMIT = 50

ONLYCAT_API_STATUS_CODES = {"UNAUTHORIZED": 401}
