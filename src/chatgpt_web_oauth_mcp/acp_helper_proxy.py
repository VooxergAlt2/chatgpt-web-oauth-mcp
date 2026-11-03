"""Unprivileged stdio relay inside the authenticated runtime sandbox.

The socket endpoint is fixed by the trusted launcher. It accepts no commands,
environment, paths or grants from argv; stdin is the native harness protocol.
"""
import os
import socket
import sys
import threading


def main():
    if len(sys.argv) != 1:
        raise SystemExit(2)
    channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    channel.connect('/control/helper.sock')
    def upload():
        total = 0
        try:
            while True:
                raw = os.read(0, 65536)
                if not raw:
                    channel.shutdown(socket.SHUT_WR)
                    return
                total += len(raw)
                if total > 16777216:
                    channel.close()
                    return
                channel.sendall(raw)
        except OSError:
            return
    threading.Thread(target=upload, daemon=True).start()
    total = 0
    try:
        while True:
            raw = channel.recv(65536)
            if not raw:
                return
            total += len(raw)
            if total > 16777216:
                raise SystemExit(3)
            while raw:
                raw = raw[os.write(1, raw):]
    finally:
        channel.close()


if __name__ == '__main__':
    main()
