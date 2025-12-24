"""
PySide6 GUI for the LAN Chat + File Transfer App.
This GUI polls a thread-safe incoming_queue for network events and updates widgets.
"""
import os, queue, threading, time
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QPushButton,
                               QListWidget, QTextEdit, QLineEdit, QLabel, QVBoxLayout,
                               QHBoxLayout, QFileDialog, QMessageBox, QSplitter)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from .network import DiscoveryThread, TCPServerThread, send_text, send_file
from .utils import get_local_ip

RECV_FOLDER = os.path.join(os.path.expanduser("~"), "LANChat_Received")

class ChatMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LAN Chat + File Transfer")
        self.resize(1000, 640)

        # Shared event queue
        self.incoming_queue = queue.Queue()

        # Profiles data
        self.profiles = []  # list of dicts {name, port, server_thread, stop_event}
        self.profile_lock = threading.Lock()
        self.current_profile = None

        # Discovered peers: mapping (ip,port) -> {"name":..., "last_seen":ts}
        self.peers = {}

        self._build_ui()

        # Start discovery thread (shares a callable to get current profiles)
        self.discovery_stop = threading.Event()
        self.discovery = DiscoveryThread(self._profiles_snapshot, self.incoming_queue, self.discovery_stop)
        self.discovery.start()

        # Timer to poll incoming queue
        self.timer = QTimer()
        self.timer.timeout.connect(self._poll_queue)
        self.timer.start(200)

    def _build_ui(self):
        # Left: profiles
        left = QWidget()
        l_layout = QVBoxLayout(left)
        l_layout.addWidget(QLabel("Profiles"))
        self.profile_list = QListWidget()
        l_layout.addWidget(self.profile_list)
        form_h = QHBoxLayout()
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Profile name")
        self.port_input = QLineEdit()
        self.port_input.setPlaceholderText("Port (e.g., 60001)")
        form_h.addWidget(self.name_input)
        form_h.addWidget(self.port_input)
        l_layout.addLayout(form_h)
        btn_h = QHBoxLayout()
        self.create_btn = QPushButton("Create Profile")
        self.create_btn.clicked.connect(self.create_profile)
        btn_h.addWidget(self.create_btn)
        self.remove_btn = QPushButton("Remove Profile")
        self.remove_btn.clicked.connect(self.remove_profile)
        btn_h.addWidget(self.remove_btn)
        l_layout.addLayout(btn_h)

        # Middle: peers
        middle = QWidget()
        m_layout = QVBoxLayout(middle)
        m_layout.addWidget(QLabel("Discovered Peers"))
        self.peer_list = QListWidget()
        m_layout.addWidget(self.peer_list)
        self.refresh_btn = QPushButton("Refresh Peers")
        self.refresh_btn.clicked.connect(self.refresh_peers)
        m_layout.addWidget(self.refresh_btn)

        # Right: chat
        right = QWidget()
        r_layout = QVBoxLayout(right)
        r_layout.addWidget(QLabel("Chat"))
        self.chat_view = QTextEdit()
        self.chat_view.setReadOnly(True)
        self.chat_view.setAcceptDrops(True)
        self.chat_view.installEventFilter(self)
        r_layout.addWidget(self.chat_view)
        send_h = QHBoxLayout()
        self.msg_input = QLineEdit()
        send_h.addWidget(self.msg_input)
        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self._on_send)
        send_h.addWidget(self.send_btn)
        self.attach_btn = QPushButton("Attach & Send")
        self.attach_btn.clicked.connect(self._on_attach)
        send_h.addWidget(self.attach_btn)
        r_layout.addLayout(send_h)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(middle)
        splitter.addWidget(right)
        splitter.setStretchFactor(2,1)
        self.setCentralWidget(splitter)

        # Click handlers
        self.profile_list.itemClicked.connect(self._on_profile_selected)
        self.peer_list.itemClicked.connect(self._on_peer_selected)

    def _profiles_snapshot(self):
        """
        Return list of profiles (name,port) for discovery to broadcast.
        """
        with self.profile_lock:
            return [{"name":p["name"], "port":p["port"]} for p in self.profiles]

    def create_profile(self):
        name = self.name_input.text().strip()
        port_text = self.port_input.text().strip()
        if not name or not port_text:
            QMessageBox.warning(self, "Missing", "Please enter profile name and port.")
            return
        try:
            port = int(port_text)
        except ValueError:
            QMessageBox.warning(self, "Invalid", "Port must be a number.")
            return
        # spawn TCP server for this profile
        stop_event = threading.Event()
        profile = {"name": name, "port": port, "stop": stop_event, "server": None}
        server = TCPServerThread(profile, self.incoming_queue, stop_event, RECV_FOLDER)
        profile["server"] = server
        server.start()
        with self.profile_lock:
            self.profiles.append(profile)
        self.profile_list.addItem(f"{name} : {port}")
        self.name_input.clear()
        self.port_input.clear()
        self._log(f"Created profile {name} on port {port}")

    def remove_profile(self):
        sel = self.profile_list.currentItem()
        if not sel:
            return
        text = sel.text()
        # find profile
        name, port = [s.strip() for s in text.split(":",1)]
        with self.profile_lock:
            to_remove = None
            for p in self.profiles:
                if p["name"]==name and str(p["port"])==port:
                    to_remove = p
                    break
            if to_remove:
                to_remove["stop"].set()
                try:
                    # close server socket by connecting to it
                    import socket
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.connect(("127.0.0.1", int(to_remove["port"])))
                    s.close()
                except Exception:
                    pass
                self.profiles.remove(to_remove)
        self.profile_list.takeItem(self.profile_list.row(sel))
        self._log(f"Removed profile {name}:{port}")

    def refresh_peers(self):
        self.peer_list.clear()
        for k,v in list(self.peers.items()):
            ip, port = k
            self.peer_list.addItem(f"{v.get('name')} @ {ip}:{port}")

    def _on_profile_selected(self, item):
        # select profile for sending messages
        text = item.text()
        name, port = [s.strip() for s in text.split(":",1)]
        with self.profile_lock:
            for p in self.profiles:
                if p["name"]==name and str(p["port"])==port:
                    self.current_profile = p
                    self._log(f"Selected profile: {name}:{port}")
                    return

    def _on_peer_selected(self, item):
        # no extra actions for now
        pass

    def _on_send(self):
        text = self.msg_input.text().strip()
        if not text:
            return
        sel = self.peer_list.currentItem()
        if not sel:
            QMessageBox.warning(self, "Select peer", "Choose a peer to send to.")
            return
        if not self.current_profile:
            QMessageBox.warning(self, "Select profile", "Choose which local profile will send the message.")
            return
        # parse selected peer
        txt = sel.text()
        try:
            # expected "Name @ ip:port"
            parts = txt.split("@")
            ipport = parts[1].strip()
            ip, port = ipport.split(":")
        except Exception:
            QMessageBox.warning(self, "Parse error", "Cannot parse selected peer address.")
            return
        # perform send in background
        threading.Thread(target=self._do_send_text, args=(ip.strip(), int(port.strip()), self.current_profile, text), daemon=True).start()
        self.chat_view.append(f"[me:{self.current_profile['name']}] {text}")
        self.msg_input.clear()

    def _do_send_text(self, ip, port, profile, text):
        try:
            send_text(ip, port, profile["name"], text)
        except Exception as e:
            self._log(f"Send failed: {e}")

    def _on_attach(self):
        sel = self.peer_list.currentItem()
        if not sel:
            QMessageBox.warning(self, "Select peer", "Choose a peer to send to.")
            return
        if not self.current_profile:
            QMessageBox.warning(self, "Select profile", "Choose which local profile will send the file.")
            return
        fname, _ = QFileDialog.getOpenFileName(self, "Choose file to send")
        if not fname:
            return
        txt = sel.text()
        parts = txt.split("@")
        ipport = parts[1].strip()
        ip, port = ipport.split(":")
        # start sending in background
        threading.Thread(target=self._do_send_file, args=(ip.strip(), int(port.strip()), self.current_profile, fname), daemon=True).start()
        self.chat_view.append(f"[file sent from {self.current_profile['name']}] {os.path.basename(fname)}")

    def _do_send_file(self, ip, port, profile, file_path):
        def progress(sent, total):
            # could update a GUI progress bar via queue
            pass
        try:
            send_file(ip, port, profile["name"], file_path, progress_callback=progress)
        except Exception as e:
            self._log(f"File send failed: {e}")

    def eventFilter(self, obj, event):
        # simple drag-and-drop: if files dropped onto chat_view, send them
        if obj == self.chat_view and event.type() == event.Drop:
            mime = event.mimeData()
            if mime.hasUrls():
                for url in mime.urls():
                    path = url.toLocalFile()
                    sel = self.peer_list.currentItem()
                    if sel and self.current_profile:
                        txt = sel.text()
                        parts = txt.split("@")
                        ipport = parts[1].strip()
                        ip, port = ipport.split(":")
                        threading.Thread(target=self._do_send_file, args=(ip.strip(), int(port.strip()), self.current_profile, path), daemon=True).start()
                        self.chat_view.append(f"[file sent from {self.current_profile['name']}] {os.path.basename(path)}")
                return True
        return super().eventFilter(obj, event)

    def _poll_queue(self):
        # handle incoming events from network threads
        while True:
            try:
                ev = self.incoming_queue.get_nowait()
            except queue.Empty:
                break
            try:
                if ev["type"] == "presence":
                    # update peers list
                    from_ip = ev["from"]
                    for p in ev["profiles"]:
                        key = (from_ip, str(p["port"]))
                        self.peers[key] = {"name": p.get("name","?"), "last_seen": time.time()}
                    self.refresh_peers()
                elif ev["type"] == "message":
                    self.chat_view.append(f"[{ev.get('from')}@{ev.get('from_ip')}] {ev.get('content')}")
                elif ev["type"] == "file":
                    # show file received notification and path
                    msg = f"File received from {ev.get('from')}: {ev.get('filename')} -> saved to {ev.get('path')}"
                    self.chat_view.append(msg)
                elif ev["type"] == "server_error":
                    self._log(f"Server error for {ev.get('profile')}: {ev.get('error')}")
                elif ev["type"] == "conn_error":
                    self._log(f"Connection error: {ev.get('error')}")
            except Exception as e:
                print("Error handling event:", e)

    def _log(self, msg):
        self.chat_view.append(f"[system] {msg}")

    def closeEvent(self, event):
        # cleanup threads
        self.discovery_stop.set()
        with self.profile_lock:
            for p in list(self.profiles):
                p["stop"].set()
        super().closeEvent(event)