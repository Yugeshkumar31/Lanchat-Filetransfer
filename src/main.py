#!/usr/bin/env python3
import argparse
from gui import ChatWindow
from PySide6.QtWidgets import QApplication
import sys

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--name', required=False, default='Peer', help='Display name for this instance')
    parser.add_argument('--port', required=False, type=int, default=5001, help='TCP port for incoming connections')
    parser.add_argument('--save-dir', required=False, default='received_files', help='Directory to save incoming files')
    args = parser.parse_args()

    app = QApplication([])
    window = ChatWindow(username=args.name, tcp_port=args.port, save_dir=args.save_dir)
    window.show()
    sys.exit(app.exec())

if __name__ == '__main__':
    main()
