# pi5-visa-server

A small Flask server that turns a Raspberry Pi 5 into a network front end for one USB lab instrument: SCPI commands arrive over HTTP and reach the instrument through USBTMC.

> **Status: archived.** Written in December 2025 and tested once. It hasn't been maintained or re-tested since; the known limits are listed below.

Despite the name, it doesn't use a VISA library: [python-usbtmc](https://pypi.org/project/python-usbtmc/) talks to the instrument directly over USB.

## What it does

- **`POST /scpi`** sends the request body to the instrument as one SCPI command. A command that ends in `?` is a query, and the instrument's reply is the response. Any other command is written, and the response is `OK`.
- **`GET /status`** returns JSON: whether an instrument is connected and, if it is, its `*IDN?` string.
- **A background thread** opens the first USBTMC instrument it finds, checks the link every 5 s with `*OPC?`, and reconnects after the instrument is unplugged and plugged back in.

| Response | When |
|---|---|
| `400 Empty command` | the request body is empty |
| `503 No instrument connected` | no instrument is open |
| `500 Error talking to instrument: …` | the instrument call failed; the server drops the link, and the background thread reconnects |

## Running it

On the Pi:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install flask python-usbtmc pyusb
python server.py
```

The server listens on `127.0.0.1:5000` only, so the network can't reach it directly. From another computer, open an SSH tunnel and send commands through it:

```bash
ssh -L 5000:127.0.0.1:5000 <user>@<pi-address>

# in a second terminal
curl http://127.0.0.1:5000/status
curl -X POST -H "Content-Type: text/plain" --data '*IDN?' http://127.0.0.1:5000/scpi
```

The `Content-Type` header matters: without it, curl sends the body as form data, which Flask doesn't pass on as raw data, and the server answers `Empty command`.

## Known limits

- **One instrument:** the first USBTMC device found is used; there's no choice by serial number or VID/PID.
- **The timeout is 2,000 s, not 2 s.** The code sets `inst.timeout = 2000`, but python-usbtmc counts seconds. An instrument that stops answering holds its request, and every request queued behind it, for over half an hour.
- **Queries are recognized only by a trailing `?`.** A query with arguments after the `?` (for example `MEAS:VOLT:DC? 10,0.001`) is written as a command, and its reply is never read.
- **The health check runs outside the lock** that guards requests, so its `*OPC?` can interleave with a request on the same USB link.
- **No authentication:** the SSH tunnel is the access control. Binding the server to `0.0.0.0` would open the instrument to anyone on the network.
- **USB permissions:** on Linux, the user running the server needs access to the instrument's USB device, for example through a udev rule.
