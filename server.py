from flask import Flask, request, jsonify
import usbtmc
import threading
import time

app = Flask(__name__)

instrument = None
instrument_lock = threading.Lock()

# State flags just for pretty logging
no_instrument_announced = False
connected_info = None
list_devices_warned = False


def get_usbtmc_devices():
    """
    Return a list of USBTMC devices.

    Uses usbtmc.list_devices() if available. If not, returns [] and
    prints a one-time warning so you know you need a newer python-usbtmc
    or to go back to manual VID/PID.
    """
    global list_devices_warned

    if hasattr(usbtmc, "list_devices"):
        try:
            return list(usbtmc.list_devices())
        except Exception as e:
            if not list_devices_warned:
                print(f"[USBTMC] Error while listing devices: {e}")
                list_devices_warned = True
            return []
    else:
        if not list_devices_warned:
            print("[USBTMC] This python-usbtmc has no list_devices(). "
                  "Automatic detection is not available; please update "
                  "python-usbtmc or use manual VID/PID.")
            list_devices_warned = True
        return []


def try_connect_instrument():
    """
    Try to open the first USBTMC instrument we see.

    - If none found: print a single 'no instrument' message.
    - If found and successfully opened: print one 'connected' message
      with IDN + VID/PID.
    """
    global instrument, no_instrument_announced, connected_info

    with instrument_lock:
        if instrument is not None:
            return  # already connected

        devices = get_usbtmc_devices()
        if not devices:
            # No device at all: only say it once until something appears
            if not no_instrument_announced:
                print("[USBTMC] No USBTMC instrument detected. "
                      "Waiting for a device to be connected...")
                no_instrument_announced = True
            return

        # Pick the first USBTMC device (you can later extend this to choose
        # by product/serial etc.)
        dev = devices[0]

        try:
            inst = usbtmc.Instrument(dev)
            inst.timeout = 2000

            idn = inst.ask("*IDN?").strip()

            info = (f"{idn} "
                    f"(VID=0x{dev.idVendor:04X}, "
                    f"PID=0x{dev.idProduct:04X}, "
                    f"bus={getattr(dev, 'bus', '?')}, "
                    f"addr={getattr(dev, 'address', '?')})")

            instrument = inst
            no_instrument_announced = False  # reset

            if info != connected_info:
                print(f"[USBTMC] Connected to instrument: {info}")
                connected_info = info

        except Exception as e:
            # Failed to connect to this device; we’ll just wait and retry
            if not no_instrument_announced:
                print(f"[USBTMC] Found a USBTMC device but could not "
                      f"connect yet: {e}")
                no_instrument_announced = True
            instrument = None


def instrument_watcher():
    """
    Background thread: periodically try to connect / check link.

    Only prints when the state changes (connected → gone or
    no device → found).
    """
    global instrument, connected_info

    while True:
        if instrument is None:
            # Not connected yet -> try to connect
            try_connect_instrument()
        else:
            # Connected -> cheap health check
            try:
                instrument.ask("*OPC?")
            except Exception as e:
                print(f"[USBTMC] Instrument seems gone: {e}")
                with instrument_lock:
                    instrument = None
                connected_info = None  # allow new 'connected' message
        time.sleep(5)


# Start background watcher thread
watcher_thread = threading.Thread(target=instrument_watcher, daemon=True)
watcher_thread.start()


@app.route("/scpi", methods=["POST"])
def scpi():
    """
    Send a raw SCPI command in the request body.

    If it ends with '?', we return the instrument's reply.
    Otherwise we just write it and return 'OK'.
    """
    global instrument
    cmd = request.data.decode().strip()
    if not cmd:
        return "Empty command", 400

    with instrument_lock:
        if instrument is None:
            return "No instrument connected", 503

        try:
            if cmd.endswith("?"):
                resp = instrument.ask(cmd)
                # usbtmc usually includes its own newline; just return as-is
                return resp
            else:
                instrument.write(cmd)
                return "OK"
        except Exception as e:
            print(f"[USBTMC] Error during SCPI '{cmd}': {e}")
            instrument = None
            return f"Error talking to instrument: {e}", 500


@app.route("/status", methods=["GET"])
def status():
    """Quick status + IDN check."""
    global instrument
    with instrument_lock:
        if instrument is None:
            return jsonify({"connected": False})

        try:
            idn = instrument.ask("*IDN?")
            return jsonify({"connected": True, "idn": idn.strip()})
        except Exception as e:
            instrument = None
            return jsonify({"connected": False, "error": str(e)})


if __name__ == "__main__":
    # Listen only on localhost; you’ll use SSH port forwarding from your Mac
    app.run(host="127.0.0.1", port=5000)
