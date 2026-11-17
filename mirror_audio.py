"""
mirror_audio - play your PC's sound on two (or more) audio devices at once.  Windows only.

Windows sends all sound to your *default* playback device. This script records what
that device is playing (WASAPI loopback) and plays a live copy on the other devices
in your group, so e.g. USB-C earbuds and wired headphones both get the same audio.

    python mirror_audio.py                          # uses DEVICES below
    python mirror_audio.py --devices EarPods Realtek
    python mirror_audio.py --list                   # show device names
    python mirror_audio.py --volume 0.8 --latency 30

Whichever device in the group is the Windows default plays the original sound and the
rest get the copy. Change the default while it runs and it switches over by itself.
Stop with Ctrl+C.
"""

import argparse
import collections
import ctypes
import sys
import threading
import time

if sys.platform != "win32":
    sys.exit("mirror_audio only works on Windows (it uses WASAPI loopback).")

try:
    import numpy as np
    import pyaudiowpatch as pyaudio
except ImportError:
    sys.exit("Missing packages. Run:  python -m pip install -r requirements.txt")

# Part of the names Windows shows for your devices (run with --list to see them).
DEVICES = ("EarPods", "Realtek")

LATENCY_MS = 40       # cushion for the copy; raise it if you hear crackles
POLL_SECONDS = 0.5    # how often to check for device changes while mirroring
RETRY_SECONDS = 2     # how often to retry while waiting for a device


class Waiting(Exception):
    """Mirroring can't start right now (device missing, wrong default, ...)."""


# ------------------------------------------------------------------ logging

_last_status = None


def log(message):
    print(time.strftime("[%H:%M:%S] ") + message, flush=True)


def status(message):
    """Log a message only if it differs from the previous status (no spam while waiting)."""
    global _last_status
    if message != _last_status:
        log(message)
        _last_status = message


# ------------------------------------------------------------------ default device watcher

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


def _com_call(obj, index, *args, argtypes=()):
    """Call method number `index` of a COM interface pointer; returns the HRESULT."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    method = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtable[index])
    return method(obj, *args)


class DefaultDeviceWatcher:
    """Reports the ID of Windows' default playback device via the Core Audio API.

    PortAudio only reads the device list once at startup, so we ask Windows directly
    to notice when the user switches the default device while we're running.
    """

    def __init__(self):
        ole32 = self._ole32 = ctypes.windll.ole32
        ole32.CoInitialize(None)  # already-initialised results are fine
        clsid, iid = _GUID(), _GUID()
        ole32.CLSIDFromString("{BCDE0395-E52F-467C-8E3D-C4579291692E}", ctypes.byref(clsid))
        ole32.CLSIDFromString("{A95664D2-9614-4F35-A746-DE8DB63617E6}", ctypes.byref(iid))
        self._enumerator = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid),
                                    ctypes.byref(self._enumerator))
        if hr != 0:
            self._enumerator = None

    def current(self):
        if not self._enumerator:
            return None
        device = ctypes.c_void_p()
        # IMMDeviceEnumerator::GetDefaultAudioEndpoint(eRender, eMultimedia, &device)
        hr = _com_call(self._enumerator, 4, 0, 1, ctypes.byref(device),
                       argtypes=(ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)))
        if hr != 0 or not device:
            return None
        try:
            id_ptr = ctypes.c_void_p()
            # IMMDevice::GetId(&id)
            if _com_call(device, 5, ctypes.byref(id_ptr),
                         argtypes=(ctypes.POINTER(ctypes.c_void_p),)) != 0:
                return None
            device_id = ctypes.wstring_at(id_ptr.value)
            self._ole32.CoTaskMemFree(id_ptr)
            return device_id
        finally:
            _com_call(device, 2)  # IUnknown::Release


# ------------------------------------------------------------------ device lookup

def wasapi_outputs(p):
    api = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    outs = []
    for i in range(p.get_device_count()):
        d = p.get_device_info_by_index(i)
        if (d["hostApi"] == api["index"] and d["maxOutputChannels"] > 0
                and not d.get("isLoopbackDevice")):
            outs.append(d)
    if api["defaultOutputDevice"] < 0:
        return outs, None
    return outs, p.get_device_info_by_index(api["defaultOutputDevice"])


def matches(device, names):
    return any(n.lower() in device["name"].lower() for n in names)


def plan_routing(p, names):
    """Return (source, targets): the default device, and the other devices in the group."""
    outs, default = wasapi_outputs(p)
    group = []
    for name in names:
        found = [d for d in outs if name.lower() in d["name"].lower()]
        if not found:
            raise Waiting(f'Waiting for "{name}" to be plugged in...')
        if found[0]["index"] not in (d["index"] for d in group):
            group.append(found[0])
    if len(group) < 2:
        raise Waiting("Your --devices names all match the same device; give at least two.")
    if default is None or default["index"] not in (d["index"] for d in group):
        current = default["name"] if default else "nothing"
        raise Waiting(f'Default output is "{current}". Waiting until one of '
                      f'{", ".join(names)} is set as the default...')
    return default, [d for d in group if d["index"] != default["index"]]


def loopback_of(p, device):
    for lb in p.get_loopback_device_info_generator():
        if device["name"] in lb["name"]:
            return lb
    raise Waiting(f'Windows offers no loopback for "{device["name"]}".')


def list_devices(names):
    p = pyaudio.PyAudio()
    try:
        outs, default = wasapi_outputs(p)
        print("Playback devices   (* = Windows default,  + = in your --devices group)\n")
        for d in outs:
            mark = "*" if default and d["index"] == default["index"] else " "
            mark += "+" if matches(d, names) else " "
            print(f'  {mark} {d["name"]}   ({d["maxOutputChannels"]} ch, '
                  f'{int(d["defaultSampleRate"])} Hz)')
        print(f'\nCurrent group: {", ".join(names)}')
    finally:
        p.terminate()


# ------------------------------------------------------------------ audio plumbing

class FrameBuffer:
    """Thread-safe FIFO of audio frames that keeps latency bounded.

    Two sound cards never run at exactly the same speed, so the buffer slowly grows
    or drains. We trim it when it gets too full, and after it runs dry we wait for a
    small cushion before playing again, which avoids crackling.
    """

    def __init__(self, channels, latency_frames):
        self.channels = channels
        self.target = max(latency_frames, 1)
        self.chunks = collections.deque()
        self.frames = 0
        self.priming = True
        self.lock = threading.Lock()

    def write(self, x):
        with self.lock:
            self.chunks.append(x.copy())
            self.frames += len(x)

    def _drop(self, n):
        while n > 0 and self.chunks:
            c = self.chunks[0]
            if len(c) <= n:
                self.chunks.popleft()
                n -= len(c)
                self.frames -= len(c)
            else:
                self.chunks[0] = c[n:]
                self.frames -= n
                n = 0

    def read(self, n):
        out = np.zeros((n, self.channels), dtype=np.float32)
        with self.lock:
            if self.frames > 3 * self.target:          # drifted too far behind
                self._drop(self.frames - self.target)
            if self.priming:
                if self.frames < self.target:
                    return out                          # silence while filling up
                self.priming = False
            filled = 0
            while filled < n and self.chunks:
                c = self.chunks[0]
                take = min(len(c), n - filled)
                out[filled:filled + take] = c[:take]
                if take == len(c):
                    self.chunks.popleft()
                else:
                    self.chunks[0] = c[take:]
                filled += take
                self.frames -= take
            if filled < n:                              # ran dry: re-prime
                self.priming = True
        return out


class Resampler:
    """Linear-interpolation resampler, only used when two devices' rates differ."""

    def __init__(self, in_rate, out_rate, channels):
        self.step = in_rate / out_rate
        self.pos = 0.0
        self.last = np.zeros((1, channels), dtype=np.float32)

    def process(self, x):
        buf = np.vstack([self.last, x])
        end = len(buf) - 1
        positions = np.arange(self.pos, end, self.step)
        self.last = buf[-1:]
        if len(positions) == 0:
            self.pos -= end
            return np.zeros((0, x.shape[1]), dtype=np.float32)
        idx = positions.astype(np.int64)
        frac = (positions - idx)[:, None].astype(np.float32)
        self.pos = positions[-1] + self.step - end
        return (buf[idx] * (1 - frac) + buf[idx + 1] * frac).astype(np.float32)


def match_channels(x, channels):
    n = x.shape[1]
    if n == channels:
        return x
    if n == 1:
        return np.repeat(x, channels, axis=1)
    if channels == 1:
        return x.mean(axis=1, keepdims=True)
    out = np.zeros((len(x), channels), dtype=np.float32)
    k = min(n, channels)
    out[:, :k] = x[:, :k]
    return out


class Copy:
    """One output device receiving the mirrored sound."""

    def __init__(self, p, device, in_rate, in_ch, latency_ms, volume):
        self.name = device["name"]
        self.rate = int(device["defaultSampleRate"])
        self.channels = device["maxOutputChannels"]
        self.volume = volume
        self.buffer = FrameBuffer(self.channels, int(self.rate * latency_ms / 1000))
        self.resampler = Resampler(in_rate, self.rate, in_ch) if in_rate != self.rate else None
        self.stream = p.open(format=pyaudio.paFloat32, channels=self.channels, rate=self.rate,
                             output=True, output_device_index=device["index"],
                             frames_per_buffer=self.rate // 100, stream_callback=self._play)

    def feed(self, x):
        if self.resampler:
            x = self.resampler.process(x)
        self.buffer.write(match_channels(x, self.channels))

    def _play(self, in_data, frame_count, time_info, status_flags):
        out = self.buffer.read(frame_count)
        if self.volume != 1.0:
            out *= self.volume
        return out.tobytes(), pyaudio.paContinue


# ------------------------------------------------------------------ main loop

def close_quietly(stream):
    try:
        stream.stop_stream()
        stream.close()
    except Exception:
        pass


def mirror_once(args, watcher):
    """Mirror until something changes (default device switched, device lost)."""
    p = pyaudio.PyAudio()
    streams = []
    try:
        start_id = watcher.current()
        source, targets = plan_routing(p, args.devices)
        loop = loopback_of(p, source)
        in_rate, in_ch = int(loop["defaultSampleRate"]), loop["maxInputChannels"]

        copies = []
        for device in targets:
            copy = Copy(p, device, in_rate, in_ch, args.latency, args.volume)
            copies.append(copy)
            streams.append(copy.stream)

        def on_capture(in_data, frame_count, time_info, status_flags):
            x = np.frombuffer(in_data, dtype=np.float32).reshape(-1, in_ch)
            for copy in copies:
                copy.feed(x)
            return None, pyaudio.paContinue

        streams.append(p.open(format=pyaudio.paFloat32, channels=in_ch, rate=in_rate,
                              input=True, input_device_index=loop["index"],
                              frames_per_buffer=in_rate // 100, stream_callback=on_capture))

        status(f'Mirroring "{source["name"]}"  ->  '
               + ", ".join(f'"{c.name}"' for c in copies) + "   (Ctrl+C to stop)")
        while True:
            time.sleep(POLL_SECONDS)
            if not all(s.is_active() for s in streams):
                status("An audio device stopped responding, reconnecting...")
                return
            if watcher.current() != start_id:
                status("Default output changed, switching over...")
                return
    finally:
        for s in streams:
            close_quietly(s)
        p.terminate()


def main():
    ap = argparse.ArgumentParser(
        description="Play your PC's sound on two (or more) audio devices at once.")
    ap.add_argument("--devices", nargs="+", default=list(DEVICES), metavar="NAME",
                    help="part of each device's name; the default one is copied to the others "
                         f'(default: {" ".join(DEVICES)})')
    ap.add_argument("--volume", type=float, default=1.0,
                    help="volume of the copies, 0.0-1.0 (default 1.0)")
    ap.add_argument("--latency", type=int, default=LATENCY_MS, metavar="MS",
                    help=f"buffer size in ms (default {LATENCY_MS}); raise it if you hear crackles")
    ap.add_argument("--list", action="store_true", help="list playback devices and exit")
    args = ap.parse_args()

    if args.list:
        list_devices(args.devices)
        return

    watcher = DefaultDeviceWatcher()
    try:
        while True:
            try:
                mirror_once(args, watcher)
            except Waiting as w:
                status(str(w))
                time.sleep(RETRY_SECONDS)
            except OSError as e:
                status(f"Audio error ({e}), retrying...")
                time.sleep(RETRY_SECONDS)
    except KeyboardInterrupt:
        log("Stopped.")


if __name__ == "__main__":
    main()
