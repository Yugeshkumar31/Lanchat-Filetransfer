import json, os, time

def make_message_json(kind, payload):
    return json.dumps({'kind': kind, 'time': time.time(), 'payload': payload}, separators=(',',':')).encode('utf-8') + b'\n'

def ensure_dir(d):
    os.makedirs(d, exist_ok=True)
