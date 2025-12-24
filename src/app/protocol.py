"""
Simple protocol helpers and constants.
"""
import json

DISCOVERY_PORT = 45454
DISCOVERY_INTERVAL = 5  # seconds

def make_presence(profiles):
    # profiles: list of dicts {"name":..., "port":...}
    return json.dumps({
        "cmd": "presence",
        "profiles": profiles
    }).encode('utf-8')

def parse_presence(data_bytes):
    try:
        return json.loads(data_bytes.decode('utf-8'))
    except Exception:
        return None