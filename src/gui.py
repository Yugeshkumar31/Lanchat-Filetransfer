from PySide6 import QtCore, QtGui, QtWidgets
import sys, os, threading
from network import PeerDiscovery, TCPServer, PeerClient, handle_incoming_connection
from queue import Queue, Empty
from utils import ensure_dir, make_message_json
import json, time

class WorkerSignals(QtCore.QObject):
    peer_discovered = QtCore.Signal(str,int,str)   # ip,port,name
    message_received = QtCore.Signal(dict)
    file_received = QtCore.Signal(dict)

class ChatWindow(QtWidgets.QWidget):
    def __init__(self, username='Peer', tcp_port=5001, save_dir='received_files'):
        super().__init__()
        self.setWindowTitle(f'LAN Chat - {username}')
        self.setMinimumSize(900,600)
        self.username = username
        self.tcp_port = tcp_port
        self.save_dir = save_dir
        ensure_dir(self.save_dir)

        # networking
        self.stop_event = threading.Event()
        self.incoming_queue = Queue()
        self.peers = {}  # (ip,port) -> name
        self.clients = {}  # (ip,port) -> PeerClient

        self.signals = WorkerSignals()
        self.signals.peer_discovered.connect(self._on_peer_discovered)
        self.signals.message_received.connect(self._on_message_received)
        self.signals.file_received.connect(self._on_file_received)

        # start discovery & server
        self.discovery = PeerDiscovery(self.username, self.tcp_port, lambda ip,port,name: self.signals.peer_discovered.emit(ip,port,name), self.stop_event)
        self.discovery.start()
        self.server = TCPServer('0.0.0.0', self.tcp_port, lambda conn,addr: threading.Thread(target=handle_incoming_connection, args=(conn,addr,self.incoming_queue,self.save_dir), daemon=True).start(), self.stop_event)
        self.server.start()

        # Thread: monitor incoming_queue and emit signals
        threading.Thread(target=self._incoming_monitor, daemon=True).start()

        self._build_ui()
        self._apply_styles()

    def closeEvent(self, event):
        self.stop_event.set()
        event.accept()

    def _incoming_monitor(self):
        while not self.stop_event.is_set():
            try:
                pkg = self.incoming_queue.get(timeout=0.5)
                kind = pkg.get('kind')
                if kind == 'file-received':
                    self.signals.file_received.emit(pkg)
                else:
                    self.signals.message_received.emit(pkg)
            except Empty:
                continue

    def _build_ui(self):
        layout = QtWidgets.QHBoxLayout(self)
        # left: peers and controls
        left = QtWidgets.QFrame()
        left.setFixedWidth(260)
        left_layout = QtWidgets.QVBoxLayout(left)
        left_layout.setContentsMargins(8,8,8,8)
        lbl = QtWidgets.QLabel('Peers on LAN')
        lbl.setObjectName('sectionTitle')
        left_layout.addWidget(lbl)
        self.peer_list = QtWidgets.QListWidget()
        self.peer_list.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        left_layout.addWidget(self.peer_list)
        # controls
        btn_refresh = QtWidgets.QPushButton('Refresh (broadcast now)')
        btn_refresh.clicked.connect(lambda: None)
        left_layout.addWidget(btn_refresh)
        left_layout.addStretch()

        # center: chat area with drag-drop
        center = QtWidgets.QFrame()
        center_layout = QtWidgets.QVBoxLayout(center)
        self.chat_view = QtWidgets.QTextEdit()
        self.chat_view.setReadOnly(True)
        self.chat_view.setAcceptDrops(False)
        center_layout.addWidget(self.chat_view)
        # input row
        row = QtWidgets.QHBoxLayout()
        self.input_line = QtWidgets.QLineEdit()
        self.input_line.returnPressed.connect(self.send_message)
        row.addWidget(self.input_line)
        send_btn = QtWidgets.QPushButton('Send')
        send_btn.clicked.connect(self.send_message)
        row.addWidget(send_btn)
        attach_btn = QtWidgets.QPushButton('📎')
        attach_btn.clicked.connect(self.select_and_send_file)
        row.addWidget(attach_btn)
        center_layout.addLayout(row)

        # right: status and progress
        right = QtWidgets.QFrame()
        right.setFixedWidth(260)
        right_layout = QtWidgets.QVBoxLayout(right)
        right_layout.setContentsMargins(8,8,8,8)
        info = QtWidgets.QLabel('Status')
        info.setObjectName('sectionTitle')
        right_layout.addWidget(info)
        self.status_area = QtWidgets.QTextEdit()
        self.status_area.setReadOnly(True)
        self.status_area.setFixedHeight(220)
        right_layout.addWidget(self.status_area)
        right_layout.addWidget(QtWidgets.QLabel('Transfers'))
        self.transfers = QtWidgets.QListWidget()
        right_layout.addWidget(self.transfers)
        right_layout.addStretch()

        layout.addWidget(left)
        layout.addWidget(center,1)
        layout.addWidget(right)

        # enable drag/drop on overall widget
        self.setAcceptDrops(True)

        # double click peer to connect
        self.peer_list.itemDoubleClicked.connect(self.connect_to_selected_peer)

    def _apply_styles(self):
        qss = open(os.path.join(os.path.dirname(__file__),'styles.qss')).read()
        self.setStyleSheet(qss)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
    def dropEvent(self, event):
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if os.path.isfile(path):
                self.send_file_to_selected(path)

    def _on_peer_discovered(self, ip, port, name):
        key = (ip,port)
        if key not in self.peers:
            self.peers[key] = name
            self.peer_list.addItem(f'{name} — {ip}:{port}')
            self.status_area.append(f'Discovered {name} @ {ip}:{port}')

    def connect_to_selected_peer(self, item):
        txt = item.text()
        # parse ip and port
        try:
            parts = txt.split('—')[-1].strip()
            host, port = parts.split(':')
            host = host.strip()
            port = int(port.strip())
            key = (host,port)
            if key in self.clients:
                self.status_area.append('Already connected.')
                return
            client = PeerClient(host, port, self.incoming_queue)
            self.clients[key] = client
            self.status_area.append(f'Connected to {host}:{port}')
        except Exception as e:
            self.status_area.append('Failed to connect: '+str(e))

    def send_message(self):
        txt = self.input_line.text().strip()
        if not txt:
            return
        sel = self.peer_list.currentItem()
        if not sel:
            self.status_area.append('Select a peer to send to (double-click to connect).')
            return
        parts = sel.text().split('—')[-1].strip()
        host,port = parts.split(':')
        key = (host.strip(),int(port.strip()))
        client = self.clients.get(key)
        if not client:
            # attempt to connect automatically
            client = PeerClient(host.strip(), int(port.strip()), self.incoming_queue)
            self.clients[key] = client
        payload = {'from':self.username,'text':txt}
        client.send_message('chat', payload)
        self._append_chat_line(self.username, txt)
        self.input_line.clear()

    def _append_chat_line(self, who, text):
        t = time.strftime('%H:%M:%S', time.localtime())
        self.chat_view.append(f'<b>{who}</b> <span style="color:gray">[{t}]</span>: {QtWidgets.QTextDocument().toPlainText() if False else text}')

    def _on_message_received(self, pkg):
        payload = pkg.get('payload',{})
        who = payload.get('from','?')
        text = payload.get('text','')
        self._append_chat_line(who, text)

    def select_and_send_file(self):
        sel = self.peer_list.currentItem()
        if not sel:
            self.status_area.append('Select a peer to send to (double-click to connect).')
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, 'Select file to send')
        if path:
            self.send_file_to_selected(path)

    def send_file_to_selected(self, path):
        sel = self.peer_list.currentItem()
        if not sel:
            self.status_area.append('Select a peer to send to (double-click to connect).')
            return
        parts = sel.text().split('—')[-1].strip()
        host,port = parts.split(':')
        key = (host.strip(),int(port.strip()))
        client = self.clients.get(key)
        if not client:
            client = PeerClient(host.strip(), int(port.strip()), self.incoming_queue)
            self.clients[key] = client
            # small wait could be necessary for connection -- in production you'd have better connect management
            QtCore.QTimer.singleShot(400, lambda: client.send_file(path))
        else:
            QtCore.QTimer.singleShot(0, lambda: client.send_file(path))
        self.transfers.addItem(f'Sending: {os.path.basename(path)} -> {host}:{port}')
        self.status_area.append(f'Started sending {path} to {host}:{port}')

    def _on_file_received(self, info):
        fname = info.get('filename')
        size = info.get('size')
        fr = info.get('from')
        self.transfers.addItem(f'Received: {fname} ({size} bytes) from {fr}')
        self.status_area.append(f'File saved to {os.path.join(self.save_dir, fname)}')


