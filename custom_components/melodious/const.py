"""Constants for the Melodious integration."""

DOMAIN = "melodious"

CONF_HA_URL = "ha_url"
CONF_WEBHOOK_ID = "webhook_id"
CONF_ENTITIES = "entities"
CONF_DEVICES = "devices"

DEFAULT_PORT = 8080

# Coalesce state changes before re-sending the device set to the hub.
PUSH_DELAY_SECONDS = 5
# How long to wait before trying the hub again after it couldn't take a set.
RETRY_SECONDS = 60

# Coalesce live state updates and send a heartbeat within the hub's 90 s stale window.
STATE_DEBOUNCE_SECONDS = 2
STATE_HEARTBEAT_SECONDS = 30
