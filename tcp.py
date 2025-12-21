#!/usr/bin/env python3
import socket
import threading
import time
from typing import Optional, Tuple

import pyvisa


HOST = "127.0.0.1"
PORT = 5001           # run one server per instrument if you want (5001,5002,5003)
CHUNK = 16384         # streaming chunk size
VISA_TIMEOUT_MS = 20000


# ---------------- VISA / instrument handling ----------------

rm = pyvisa.ResourceManager("@py")  # uses pyvisa-py backend (good on Pi)
inst_lock = threading.Lock()
inst = None
connected_info = None


def find_first_usb_instrument() -> Optional[str]:
    """Return a VISA resource string for the first USB instrument we see."""
    try:
        resources = rm.list_resources()
    except Exception:
        return None

    # Typical patterns: "USB0::0xF4EC::0xEE3A::SDS...::INSTR"
    usb = [r for r in resources if r.startswith("USB")]
    return usb[0] if usb else None


def connect_instrument_if_needed() -> None:
    global inst, connected_info
    if inst is not None:
        return

    res = find_first_usb_instrument()
    if not res:
        if connected_info != "none":
            print("[VISA] No USB instrument detected. Waiting…")
            connected_info = "none"
        return

    try:
        i = rm.open_resource(res)
        i.timeout = VISA_TIMEOUT_MS
        # Termination: for text SCPI reads you may want read_termination, but for raw we avoid it.
        # i.read_termination = '\n'  # optional, but raw reads ignore it anyway
        # i.write_termination = '\n' # optional

        # Quick ID
        i.write("*IDN?")
        idn = i.read().strip()

        inst = i
        info = f"{res} | {idn}"
        if info != connected_info:
            print(f"[VISA] Connected: {info}")
            connected_info = info

    except Exception as e:
        print(f"[VISA] Failed to connect: {e}")
        inst = None
        connected_info = None


def watcher_thread() -> None:
    """Keep instrument alive / reconnect on disconnect."""
    global inst, connected_info
    while True:
        with inst_lock:
            if inst is None:
                connect_instrument_if_needed()
            else:
                try:
                    # cheap health check
                    inst.write("*OPC?")
                    _ = inst.read()
                except Exception as e:
                    print(f"[VISA] Instrument gone: {e}")
                    try:
                        inst.close()
                    except Exception:
                        pass
                    inst = None
                    connected_info = None
        time.sleep(3)


# ---------------- SCPI reply reading (binary-safe) ----------------

def read_exact_raw(n: int) -> bytes:
    """
    Read exactly n bytes from VISA by repeatedly calling read_raw().
    This is conservative; many instruments give full blocks in one go,
    but we can't assume that.
    """
    buf = bytearray()
    while len(buf) < n:
        chunk = inst.read_raw()
        if not chunk:
            raise IOError("VISA read returned empty while expecting data")
        buf += chunk
    return bytes(buf)


def read_ieee4882_block(first: bytes) -> bytes:
    """
    Given that first begins with b'#', read an IEEE 488.2 definite-length block.
    Returns the payload bytes only (not the header).
    """
    # We may not have enough bytes yet to parse the header, so keep reading until we do.
    buf = bytearray(first)

    # Need at least 2 bytes: b'#' + one digit
    while len(buf) < 2:
        buf += inst.read_raw()

    if buf[0:1] != b"#":
        raise ValueError("Not an IEEE488.2 block")

    d = buf[1] - ord("0")  # number of digits that follow
    if d < 0 or d > 9:
        raise ValueError(f"Invalid IEEE488.2 length digit: {buf[1:2]!r}")

    # Special case: d == 0 means "indefinite length" block (rare). You usually avoid this.
    if d == 0:
        raise ValueError("Indefinite-length blocks (#0) not supported in this simple server")

    # Need 2 + d bytes to parse length digits
    while len(buf) < 2 + d:
        buf += inst.read_raw()

    length_digits = bytes(buf[2:2 + d])
    n = int(length_digits.decode("ascii"))

    # Now payload starts at offset 2 + d
    payload_start = 2 + d
    payload = bytearray(buf[payload_start:])

    # If we already have more than payload, keep only first n and ignore any extra terminator.
    while len(payload) < n:
        payload += inst.read_raw()

    return bytes(payload[:n])


def read_scpi_reply_raw() -> bytes:
    """
    Read a reply from the instrument in a binary-safe way.
    If it looks like an IEEE488.2 binary block, parse it and return payload.
    Otherwise return raw bytes received (typically ASCII ending in \\n).
    """
    first = inst.read_raw()
    if not first:
        return b""

    if first.startswith(b"#"):
        return read_ieee4882_block(first)

    # Non-block reply: usually ASCII terminated with newline.
    # We *could* loop until newline is seen; for safety, do that.
    buf = bytearray(first)
    # Avoid infinite loop: rely on VISA timeout if newline never comes.
    while b"\n" not in buf:
        buf += inst.read_raw()
    return bytes(buf)


# ---------------- TCP server protocol ----------------

def handle_client(conn: socket.socket, addr: Tuple[str, int]) -> None:
    print(f"[TCP] Client connected: {addr}")
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    rx_buf = bytearray()

    try:
        while True:
            data = conn.recv(4096)
            if not data:
                break
            rx_buf += data

            # Commands are newline-delimited
            while b"\n" in rx_buf:
                line, _, rest = rx_buf.partition(b"\n")
                rx_buf = bytearray(rest)

                cmd = line.decode("ascii", errors="ignore").strip()
                if not cmd:
                    continue

                with inst_lock:
                    if inst is None:
                        conn.sendall(b"ERR 503 No instrument connected\n")
                        continue

                    try:
                        if cmd.endswith("?"):
                            # Query: write then read reply raw
                            inst.write(cmd)
                            payload = read_scpi_reply_raw()

                            header = f"BIN {len(payload)}\n".encode("ascii")
                            conn.sendall(header)
                            # Stream payload
                            for i in range(0, len(payload), CHUNK):
                                conn.sendall(payload[i:i+CHUNK])
                        else:
                            inst.write(cmd)
                            conn.sendall(b"OK\n")

                    except Exception as e:
                        # Reset instrument on error
                        try:
                            inst.close()
                        except Exception:
                            pass
                        globals()["inst"] = None
                        globals()["connected_info"] = None
                        msg = f"ERR 500 {str(e)}\n".encode("utf-8", errors="replace")
                        conn.sendall(msg)

    finally:
        try:
            conn.close()
        except Exception:
            pass
        print(f"[TCP] Client disconnected: {addr}")


def main() -> None:
    threading.Thread(target=watcher_thread, daemon=True).start()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((HOST, PORT))
        s.listen(5)
        print(f"[TCP] Listening on {HOST}:{PORT}")

        while True:
            conn, addr = s.accept()
            t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
            t.start()


if __name__ == "__main__":
    main()
