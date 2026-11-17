# Mirror Audio

Play your Windows PC's sound on **two (or more) audio devices at the same time**, for
example USB-C earbuds and a pair of wired headphones in the 3.5 mm jack.

You don't need a virtual audio cable, Stereo Mix or a driver. It's one Python script.

## How it works

Windows sends all sound to your *default* playback device. `mirror_audio.py` records
what that device is playing (WASAPI loopback) and plays a live copy on the other
devices in your group.

```
apps ──► default device (e.g. EarPods) ──► your ears
                │
                └─ loopback ──► mirror_audio.py ──► other device(s) (e.g. Realtek jack)
```

- If you switch the Windows default to another device in the group while the script
  runs, it swaps the routing by itself.
- If a device is unplugged, it waits and starts again once the device is back.
- It handles devices with different sample rates or channel counts.
- The copy plays roughly 30–60 ms after the original. Two people listening won't notice.

## Setup

Requires Windows 10/11 and Python 3.8+.

```bash
git clone https://github.com/Devarshi07/mirror-audio.git
cd mirror-audio
python -m pip install -r requirements.txt
```

## Usage

First, find the names Windows uses for your devices:

```bash
python mirror_audio.py --list
```

```
Playback devices   (* = Windows default,  + = in your --devices group)

   + Speakers (Realtek(R) Audio)   (2 ch, 48000 Hz)
     GS27QCA (NVIDIA High Definition Audio)   (2 ch, 48000 Hz)
  *+ Headset (EarPods)   (2 ch, 48000 Hz)
```

Then start mirroring. You can give part of each name, and any number of devices:

```bash
python mirror_audio.py --devices EarPods Realtek
```

To skip typing `--devices` every time, edit the `DEVICES` line at the top of
`mirror_audio.py`. Then you can run it with no arguments, or double-click
**`Mirror Audio.bat`**. Press Ctrl+C (or close the window) to stop.

### Options

| Option | Default | What it does |
| --- | --- | --- |
| `--devices NAME ...` | `EarPods Realtek` | The devices to keep in sync. Whichever one is the Windows default plays the original and the others get the copy. |
| `--volume X` | `1.0` | Volume of the copies (0.0–1.0). You can also use the "Python" entry in the Windows Volume Mixer. |
| `--latency MS` | `40` | Buffer size for the copies. Lower it to reduce the delay, raise it if you hear crackling. |
| `--list` | | Lists your playback devices and exits. |

## Troubleshooting

- **"Default output is … Waiting until one of … is set as the default":** open Windows
  Sound settings and set one of your group's devices as the output device.
- **"Waiting for … to be plugged in":** the name doesn't match any device. Run `--list`
  and check the spelling.
- **Crackling on the copy:** try `--latency 80`.
- **No sound on the copy while a game or audio app uses exclusive mode:** Windows can't
  loop back audio that an app is playing in exclusive mode. Turn off "Allow
  applications to take exclusive control" in the default device's properties.
