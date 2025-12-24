# Simple networking layer: UDP discovery + TCP chat/file transfer
import socket, threading, json, os, selectors, struct
from queue import Queue, Empty
from utils import make_message_json, ensure_dir

BROADCAST_PORT = 9999
BROADCAST_INTERVAL = 5.0  # seconds

class PeerDiscovery(threading.Thread):
    def __init__(self, username, tcp_port, on_peer, stop_event):
        super().__init__(daemon=True)
        self.username = username
        self.tcp_port = tcp_port
        self.on_peer = on_peer
        self.stop_event = stop_event
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except:
            pass
        self.sock.bind(('', BROADCAST_PORT))
    def run(self):
        # start sender thread
        threading.Thread(target=self._bcast_sender, daemon=True).start()
        while not self.stop_event.is_set():
            try:
                data, addr = self.sock.recvfrom(4096)
                try:
                    pkg = json.loads(data.decode('utf-8'))
                    if pkg.get('type') == 'presence':
                        ip = addr[0]
                        name = pkg.get('name')
                        port = int(pkg.get('port'))
                        if ip and port and not (ip == '127.0.0.1' and port == self.tcp_port):
                            self.on_peer(ip, port, name)
                except Exception as e:
                    continue
            except Exception as e:
                continue
    def _bcast_sender(self):
        bsock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        bsock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        while not self.stop_event.is_set():
            pkg = json.dumps({'type':'presence','name':self.username,'port':self.tcp_port})
            try:
                bsock.sendto(pkg.encode('utf-8'), ('<broadcast>', BROADCAST_PORT))
            except Exception:
                try:
                    bsock.sendto(pkg.encode('utf-8'), ('255.255.255.255', BROADCAST_PORT))
                except:
                    pass
            self.stop_event.wait(BROADCAST_INTERVAL)

class TCPServer(threading.Thread):
    def __init__(self, host, port, on_connection, stop_event):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.on_connection = on_connection
        self.stop_event = stop_event
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR,1)
        self.sock.bind((self.host, self.port))
        self.sock.listen(5)
    def run(self):
        while not self.stop_event.is_set():
            try:
                conn, addr = self.sock.accept()
                self.on_connection(conn, addr)
            except Exception as e:
                continue

class PeerClient(threading.Thread):
    def __init__(self, host, port, incoming_queue):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.incoming_queue = incoming_queue
        self.sock = None
        self.running = True
        self._connect()
    def _connect(self):
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((self.host, self.port))
            self.start()
        except Exception as e:
            # connection failed; the GUI can retry
            self.running = False
    def send_message(self, kind, payload):
        try:
            b = make_message_json(kind, payload)
            self.sock.sendall(b)
        except Exception as e:
            pass
    def send_file(self, filepath):
        try:
            fname = os.path.basename(filepath)
            size = os.path.getsize(filepath)
            header = json.dumps({'kind':'file','filename':fname,'size':size}) + '\n'
            self.sock.sendall(header.encode('utf-8'))
            with open(filepath,'rb') as f:
                while True:
                    chunk = f.read(8192)
                    if not chunk:
                        break
                    self.sock.sendall(chunk)
        except Exception as e:
            pass
    def run(self):
        # receive loop for echoing incoming messages to incoming_queue
        buf = b''
        try:
            while self.running:
                data = self.sock.recv(4096)
                if not data:
                    break
                buf += data
                while b'\n' in buf:
                    line, buf = buf.split(b'\n',1)
                    try:
                        pkg = json.loads(line.decode('utf-8'))
                        self.incoming_queue.put(pkg)
                    except:
                        # maybe stray data
                        continue
        except Exception:
            pass
        self.running = False

def handle_incoming_connection(conn, addr, incoming_queue, save_dir):
    # This function runs in a new thread for each accepted connection
    try:
        with conn:
            buf = b''
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                buf += data
                # detect header line
                if b'\n' in buf:
                    line, rest = buf.split(b'\n',1)
                    # try parse header as json
                    try:
                        header = json.loads(line.decode('utf-8'))
                        kind = header.get('kind')
                        if kind == 'file':
                            fname = header.get('filename','received.bin')
                            size = int(header.get('size',0))
                            ensure_dir(save_dir)
                            target = os.path.join(save_dir, fname)
                            # write rest + subsequent bytes until size reached
                            with open(target, 'wb') as f:
                                f.write(rest)
                                got = len(rest)
                                while got < size:
                                    chunk = conn.recv(min(8192, size-got))
                                    if not chunk:
                                        break
                                    f.write(chunk)
                                    got += len(chunk)
                            incoming_queue.put({'kind':'file-received','filename':fname,'size':size,'from':addr[0]})
                            buf = b''
                        else:
                            # treat as chat message style
                            incoming_queue.put(header)
                            buf = rest
                    except Exception as e:
                        # not a json header, discard until newline
                        buf = rest
    except Exception:
        pass
