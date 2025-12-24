import socket

def get_local_ip():
    """
    Returns local LAN IP by connecting to a public DNS and reading socket name.
    Works without sending data.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # doesn't actually send packets
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip