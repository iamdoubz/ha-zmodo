# Porting the local (LAN) stream to another Zmodo camera model

The optional **local stream** feature talks to a camera directly on your LAN
over TCP port 8000, bypassing the Zmodo cloud. It drops live-view latency from
~30 s (cloud) to ~1 s and unlocks a local HD (1080p) stream. It was reverse
engineered on the **Ambarella S2L generation** (model `SD-H2001`, `product_id`
142, Sony IMX323). The login credentials, frame format and "start video"
opcodes it relies on appear to be firmware-wide for that generation, so **other
cameras of the same generation often work with no code changes at all** — they
just need to be confirmed.

This guide is for owners of a **different Zmodo/MeShare model** who want to check
whether the local stream works on their camera and, if it needs tweaks, gather
what's required to open a pull request. You only ever touch *your own* camera.

> ⚠️ **Do this on your own equipment only.** Extracting firmware and probing a
> camera you don't own may be illegal and is not what this guide is for.

---

## 0. Safety, privacy, and ground rules

The firmware on these cameras is hostile by modern standards (anonymous FTP with
full filesystem read, an empty-password root account). Treat the device
accordingly, and protect the secrets you'll encounter.

- **Isolate the camera.** Put it on an IoT VLAN with no inbound internet while
  you work. Allow only your workstation onto that VLAN.
- **Never write to the camera.** Everything here is read-only: FTP downloads and
  a single video request. Do **not** send unknown opcodes to a live camera —
  destructive ones exist in the firmware (device-ID reset, flash upgrade,
  analytics reset). Static analysis (Ghidra) is how you learn what an opcode
  does *before* anyone sends it.
- **Never commit secrets.** The camera's filesystem and any packet captures
  contain, at minimum: the Wi-Fi PSK (`wpa_supplicant.conf`), the per-device
  `aes_key`, and the `deviceID`. **None of these may appear in a PR, an issue,
  a log paste, or a committed file.** They are unique to your device and leak
  your network/credentials. A `.gitignore` entry does not make them safe to
  share — just don't attach them.
- **What is safe to share:** your model string and `product_id`, the opcode
  numbers, `ffprobe` output (resolution/codec), and the firmware *symbol
  names/addresses* you find. The protocol constants (magic, built-in login
  creds, opcodes) are already public in this repo's `local_stream.py`.

---

## 1. Prerequisites

On your workstation (Linux/macOS/WSL/Windows):

- Python 3.11+ with the `cryptography` package (`pip install cryptography`).
- `ffmpeg`/`ffprobe` (to confirm a sample decodes and read its resolution).
- `nmap` (to see what the camera exposes).
- An FTP client that can mirror (`lftp` or `wget`).
- Later, only if the quick probe fails: **Ghidra** (free, from the NSA) with a
  **JDK 21+**.

Find the camera's IP from your router's DHCP table, then check what's open:

```sh
nmap -p 21,554,8000,8554 <camera-ip>
```

You want to see **`21/tcp open`** (anonymous FTP, for firmware extraction) and
**`8000/tcp open`** (the local stream). If `8000` is closed, this camera doesn't
speak the local protocol at all and local streaming isn't possible on it. If
`21` is closed, you can still try the stream probe, but firmware extraction for
reverse-engineering will need another route (see the last section).

---

## 2. The fast path — run the probe first

Before any reverse engineering, just try the existing protocol. From a checkout
of this repo:

```sh
python tools/probe_local_stream.py <camera-ip>
```

It logs in with the firmware's built-in credentials, asks the camera for its own
AES key, then tries each known start-video opcode. For every opcode that
streams, it saves a few seconds of raw H.264 (`<ip>_<opcode>_<label>.h264`) and
prints the resolution via `ffprobe`. It writes nothing to the camera.

**Three possible outcomes:**

| Probe result | Meaning | Go to |
|---|---|---|
| One or more opcodes stream video | Your model already works with the current code | **§3** |
| Login OK, key OK, but **no opcode streams** | Your model uses different start-video opcodes | **§4 + §5** |
| **Login not acknowledged**, or connect refused | Different credentials, or not this protocol | **§4 + §5** |

---

## 3. If the probe worked: opening a "confirmed model" PR

If at least one opcode produced a clean, decodable stream, the existing
integration will almost certainly stream from your camera once you set its LAN
IP in the integration's **Configure** dialog. Try that end-to-end in Home
Assistant first.

Then open a PR that adds your model to the known-working set. Include:

1. **Model string and `product_id`.** Find them on the camera's HA device page,
   or in the integration's camera entity attributes (`product_id`), or from the
   cloud `/device` list. (The physical model is often printed on the device.)
2. **The probe's summary line and `ffprobe` output** for each working opcode
   (resolution + codec). Copy the text; **do not** attach the `.h264` samples or
   any capture — they can contain decryptable video keyed by your `aes_key`.
3. **Which opcodes worked** (e.g. "HD `0x5000` → 1920×1080, SD substream
   `0x90a2` → 640×480"). If the same opcodes as the S2L generation work, say so;
   the code needs no change beyond documenting your model as supported.
4. A line confirming live view worked in Home Assistant at both qualities.

If your model streams on **different** opcodes than the defaults in
`custom_components/zmodo/local_stream.py` (`OP_START_HD`, `OP_START_SD_SUB`,
`OP_START_SD`), that's a real code change: the opcode constants are currently
generation-wide, so per-model selection would need a small `product_id → opcodes`
map. Note the differing opcodes in the PR and we'll wire that in.

---

## 4. Extracting the firmware (anonymous FTP)

You need the firmware only if the probe failed and you have to find the right
opcodes/credentials in the binary. The camera exposes its filesystem read-only
over anonymous FTP (BusyBox `ftpd`, port 21). Log in as user `anonymous` with
any/empty password.

Two parts matter:

- **The base rootfs** (`/`) — Ambarella SDK, BusyBox, stock tools.
- **The application partition** — a jffs2 volume mounted at runtime at
  **`/tmp/app`**, with device config/keys at **`/tmp/config`**. `/tmp` is a
  tmpfs that only exists once the camera has booted (it always has, if it's
  running). **This is the important part** — it holds the main application
  binary you'll analyze.

Mirror both with `lftp` (into *separate, empty* directories — treat the download
as untrusted data):

```sh
mkdir -p fw/root fw/tmp
lftp -e "set ftp:passive-mode on; mirror -c / fw/root; bye"       ftp://anonymous:@<camera-ip>
lftp -e "set ftp:passive-mode on; mirror -c /tmp fw/tmp; bye"     ftp://anonymous:@<camera-ip>
```

(`wget -m "ftp://anonymous:@<camera-ip>/"` works too, but `lftp mirror` handles
the `/tmp` tmpfs more reliably.)

**Find the main application binary.** On the S2L generation it's
`/tmp/app/AppAmba33` — a stripped 32-bit ARM ELF (~1.7 MB) that handles
encode, cloud, P2P, and the port-8000 server. On another model the name may
differ (`AppAmba*`, `ipc_app`, etc.). Locate it by size/type:

```sh
find fw/tmp -type f -exec file {} \; | grep -i "ELF 32-bit.*ARM"
```

Pick the largest ARM ELF that mentions networking/encode strings:

```sh
strings -n 8 fw/tmp/app/<binary> | grep -iE "nopoll|p2p|8000|rtsp|aes|Listener|login" | head
```

> **Handle the dump as untrusted.** Keep your analysis scripts in a *different*
> directory from `fw/`, pass paths as arguments, and run any Python that reads
> the dump with `python -I` so it can't import planted modules from the dump's
> folder.

---

## 5. Reverse-engineering the protocol (Ghidra)

Goal: recover the four things the local client needs — the **frame header**, the
**login opcode + credentials**, the **"get key" opcode**, and the **start-video
opcodes** — and confirm they differ from this repo's defaults.

Load the binary in Ghidra as **ARM little-endian** (`ARM:LE:32` — accept the
auto-detected language), and run auto-analysis with the defaults. It's stripped,
so you work from **string cross-references**.

> **Tip (Windows/corporate AV):** some security products block Ghidra's native
> `decompile.exe` from running out of `C:\temp` or other watched folders. If the
> decompiler errors with `CreateProcess error=5 / Access is denied`, run Ghidra
> from your user profile directory instead.

### 5.1 Find the port-8000 server and its dispatcher

Search for the listener strings (**Search → For Strings**), e.g.
`Listener created`, `Local port: %hu`, `Bind to local port`, or the literal
`8000` / constant `0x1F40`. Follow their cross-references into the accept loop.
From there find the function that reads a frame header and switches on an opcode
— a large `switch`/comparison ladder over 16-bit values. That's the dispatcher
(on the S2L build it's `onPacketProcesser`). The opcode values it compares
against are the protocol's command set.

### 5.2 Recover the frame header

In the dispatcher (or the read routine just above it), confirm the on-the-wire
header. On the S2L generation it is 12 bytes, little-endian:

```
0x00  uint32  magic   = 0xAAAA5555   (bytes 55 55 AA AA)
0x04  int32   length  = payload bytes after this header
0x08  byte    (unused/seq)
0x09  byte    channel (send 0)
0x0A  uint16  opcode
0x0C  payload...
```

If your model uses a different magic or header layout, note the exact bytes.

### 5.3 Login opcode and credentials

Find the handler that validates a username/password (look for `strcmp`/`memcmp`
against embedded strings, near strings like `login`, or a credential-check
routine). Recover:

- the **login opcode** (S2L: `0x9009`),
- the **credential layout** (S2L: `username[16] + password[16]`, null-padded),
- the **built-in credentials** (S2L: user `ZMDNVRLOGIN`, pass `Zmodo963852`).

Some firmware retries with a transformed credential if the raw one fails; if you
see a transform routine keyed by a constant string, note it.

### 5.4 "Get key" and "start video" opcodes

- **Get key:** the handler that returns the device's 32-char ASCII `aes_key`
  (S2L: `0x9636`). With this, the client needs only the camera's IP.
- **Start video:** the handler that begins the live stream (S2L:
  `RequestStartVideoLiving`). It maps opcodes to stream types — on the S2L build
  `0x9002` = 640×480 mainstream, `0x90a2` = 640×480 substream, `0x5000` =
  1920×1080 HD. The request is **header-only** (`length` = 0). The camera then
  streams raw `00dc`/`01dc` media frames on the same socket.

### 5.5 Confirm the media format

The video frames use a 32-byte header; keyframes (`00dc`) have their first 256
bytes encrypted with **AES-256-CBC, zero IV**, using the **32 ASCII characters
of the `aes_key` as the key** (not the decoded 16 bytes); P-frames are
cleartext. This is already implemented in `custom_components/zmodo/stream.py`
(`parse_frame`, `decrypt_keyframe`). If `ffprobe` decodes your probe sample
cleanly, this format matches and you can reuse it unchanged.

---

## 6. Reference: the known (S2L generation) protocol

Everything your model's values can be diffed against lives in
[`custom_components/zmodo/local_stream.py`](../custom_components/zmodo/local_stream.py):

| Thing | S2L value |
|---|---|
| Frame magic | `0xAAAA5555` |
| Login opcode | `0x9009` (user `ZMDNVRLOGIN`, pass `Zmodo963852`, 16-byte fields) |
| Get-key opcode | `0x9636` (returns 32-char ASCII AES key) |
| Start HD (1080p) | `0x5000` |
| Start SD substream (480p) | `0x90a2` |
| Start SD mainstream (480p) | `0x9002` |
| Media header | 32 bytes; `00dc` keyframe / `01dc` P-frame |
| Keyframe crypto | AES-256-CBC, zero IV, first 256 bytes, ASCII-key |

HD mainstream (`0x5000`) and the SD substream (`0x90a2`) can run on two
concurrent connections — that's how the integration serves HD and SD entities
at the same time.

---

## 7. What to put in the pull request

**Include:**
- Model string + `product_id`.
- Probe summary and `ffprobe` output (text).
- The opcodes/credentials/header your model uses, and how they differ from §6
  (cite the Ghidra symbol names/addresses you found).
- Confirmation that live view worked in Home Assistant.
- Any code change needed (e.g. a `product_id → opcodes` entry) if your model's
  opcodes differ from the defaults.

**Never include:**
- `aes_key`, `deviceID`, `wpa_supplicant.conf`, or any `/tmp/config` file.
- Packet captures (`.pcap`/`.pcapng`) or the raw `.h264` samples — they can
  contain video decryptable with your key.
- The firmware image or any large binary from the device.

---

## 8. If anonymous FTP is closed

Newer or differently-provisioned units may not expose port 21. Firmware
extraction then needs a route this guide doesn't cover, such as a vendor firmware
image (if the model's OTA/update file can be obtained and unpacked with
`binwalk`), or a hardware flash/UART dump. Those are advanced, model-specific,
and carry a real risk of bricking the device — only attempt them if you know
what you're doing, and still never commit the secrets they contain.
