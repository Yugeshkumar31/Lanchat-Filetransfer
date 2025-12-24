"""
Networking: UDP discovery thread and TCP server / client for chat + file.
Uses threads and a shared incoming_queue to communicate events to the GUI.
"""
import socket, threading, time, json, os
from .protocol import DISCOVERY_PORT, DISCOVERY_INTERVAL, make_presence, parse_presence

CHUNK_SIZE = 64 * 1024

class DiscoveryThread(threading.Thread):
    def __init__(self, profiles_ref, incoming_queue, stop_event):
        super().__init__(daemon=True)
        self.profiles_ref = profiles_ref  # should be a callable or object exposing current profiles
        self.incoming_queue = incoming_queue
        self.stop_event = stop_event
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except Exception:
            pass
        try:
            # bind to all interfaces to receive broadcasts
            self.sock.bind(('', DISCOVERY_PORT))
        except Exception:
            # on some systems binding may fail if another process bound; it's ok
            pass

    def run(self):
        # Spawn broadcaster in its own loop
        threading.Thread(target=self._broadcaster_loop, daemon=True).start()
        while not self.stop_event.is_set():
            try:
                data, addr = self.sock.recvfrom(65536)
                parsed = parse_presence(data)
                if parsed and parsed.get("cmd") == "presence":
                    # Structure the incoming event
                    ev = {"type": "presence", "from": addr[0], "profiles": parsed.get("profiles", [])}
                    # push to incoming queue
                    self.incoming_queue.put(ev)
            except Exception:
                time.sleep(0.1)
                continue

    def _broadcaster_loop(self):
        while not self.stop_event.is_set():
            try:
                profiles = self.profiles_ref()
                payload = make_presence(profiles)
                # broadcast on IPv4 limited broadcast
                try:
                    self.sock.sendto(payload, ('<broadcast>', DISCOVERY_PORT))
                except Exception:
                    # try global broadcast
                    self.sock.sendto(payload, ('255.255.255.255', DISCOVERY_PORT))
            except Exception:
                pass
            time.sleep(DISCOVERY_INTERVAL)

class TCPServerThread(threading.Thread):
    def __init__(self, profile, incoming_queue, stop_event, recv_folder):
        """
        profile: dict with keys: name, port
        """
        super().__init__(daemon=True)
        self.profile = profile
        self.incoming_queue = incoming_queue
        self.stop_event = stop_event
        self.recv_folder = recv_folder
        self.sock = None

    def run(self):
        port = int(self.profile['port'])
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind(('0.0.0.0', port))
            self.sock.listen(10)
        except Exception as e:
            self.incoming_queue.put({"type":"server_error","profile":self.profile,"error":str(e)})
            return

        while not self.stop_event.is_set():
            try:
                self.sock.settimeout(1.0)
                conn, addr = self.sock.accept()
                threading.Thread(target=self.handle_conn, args=(conn,addr), daemon=True).start()
            except socket.timeout:
                continue
            except Exception:
                time.sleep(0.1)
                continue

    def handle_conn(self, conn, addr):
        """
        Protocol: header_json + newline, then optional raw payload bytes (for files).
        header_json like: {"type":"text","from":"Alice","content":"Hi"}
        or {"type":"file","from":"Alice","filename":"x.png","size":12345}
        """
        try:
            f = conn.makefile('rb')
            header_line = f.readline()
            if not header_line:
                conn.close()
                return
            header = json.loads(header_line.decode('utf-8').strip())
            if header.get("type") == "text":
                ev = {
                    "type":"message",
                    "profile": self.profile,
                    "from": header.get("from"),
                    "from_ip": addr[0],
                    "from_port": addr[1],
                    "content": header.get("content"),
                }
                self.incoming_queue.put(ev)
            elif header.get("type") == "file":
                fname = header.get("filename","received.bin")
                size = int(header.get("size",0))
                os.makedirs(self.recv_folder, exist_ok=True)
                out_path = os.path.join(self.recv_folder, fname)
                received = 0
                with open(out_path, 'wb') as wf:
                    while received < size:
                        chunk = f.read(min(64*1024, size - received))
                        if not chunk:
                            break
                        wf.write(chunk)
                        received += len(chunk)
                        # optional: can push progress events
                ev = {
                    "type":"file",
                    "profile": self.profile,
                    "from": header.get("from"),
                    "from_ip": addr[0],
                    "from_port": addr[1],
                    "filename": fname,
                    "size": size,
                    "path": out_path
                }
                self.incoming_queue.put(ev)
            conn.close()
        except Exception as e:
            try:
                conn.close()
            finally:
                self.incoming_queue.put({"type":"conn_error","error":str(e),"profile":self.profile})

def send_text(to_ip, to_port, from_name, content):
    header = json.dumps({"type":"text","from":from_name,"content":content}) + "\n"
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5)
    try:
        s.connect((to_ip, int(to_port)))
        s.sendall(header.encode('utf-8'))
    finally:
        s.close()

def send_file(to_ip, to_port, from_name, file_path, progress_callback=None):
    """
    Sends a file by first sending a JSON header line followed by raw bytes.
    progress_callback(bytes_sent, total_bytes) is optional.
    """
    fname = os.path.basename(file_path)
    total = os.path.getsize(file_path)
    header = json.dumps({"type":"file","from":from_name,"filename":fname,"size":total}) + "\n"
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(10)
    sent = 0
    try:
        s.connect((to_ip, int(to_port)))
        s.sendall(header.encode('utf-8'))
        with open(file_path, 'rb') as rf:
            while True:
                chunk = rf.read(CHUNK_SIZE)
                if not chunk:
                    break
                s.sendall(chunk)
                sent += len(chunk)
                if progress_callback:
                    progress_callback(sent, total)
    finally:
        s.close()