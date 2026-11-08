"""Constants for the Melodious integration."""

DOMAIN = "melodious"

CONF_HA_URL = "ha_url"
CONF_WEBHOOK_ID = "webhook_id"
CONF_ENTITIES = "entities"
CONF_DEVICES = "devices"
CONF_MIRROR_DEVICES = "mirror_devices"
CONF_THEATER_LINKS = "theater_links"

DEFAULT_PORT = 8080

# Coalesce state changes before re-sending the device set to the hub.
PUSH_DELAY_SECONDS = 5
# How long to wait before trying the hub again after it couldn't take a set.
RETRY_SECONDS = 60
# The hub refuses a set for a reason it gives (an activity still uses an input
# or command the set drops), so the same set is not sent again before this.
REFUSED_RETRY_SECONDS = 600

# Push live state as soon as it changes, then at most this often; the hub
# takes one push per second. A heartbeat keeps it inside the 90 s stale window.
STATE_DEBOUNCE_SECONDS = 1.2
STATE_HEARTBEAT_SECONDS = 30
