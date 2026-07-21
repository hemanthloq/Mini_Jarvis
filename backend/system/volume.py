"""Master volume + brightness control via pycaw / screen-brightness-control."""
import logging

from comtypes import CoInitialize
from pycaw.pycaw import AudioUtilities

log = logging.getLogger("jarvis.volume")


def _endpoint():
    CoInitialize()  # safe to call repeatedly; needed on non-main threads
    return AudioUtilities.GetSpeakers().EndpointVolume


def get_volume() -> int:
    return round(_endpoint().GetMasterVolumeLevelScalar() * 100)


def set_volume(percent: int) -> str:
    percent = max(0, min(100, percent))
    ep = _endpoint()
    ep.SetMasterVolumeLevelScalar(percent / 100.0, None)
    ep.SetMute(0, None)
    return f"Volume {percent} percent."


def volume_up(step: int = 10) -> str:
    return set_volume(get_volume() + step)


def volume_down(step: int = 10) -> str:
    return set_volume(get_volume() - step)


def mute() -> str:
    _endpoint().SetMute(1, None)
    return "Muted."


def unmute() -> str:
    _endpoint().SetMute(0, None)
    return "Unmuted."


def set_brightness(percent: int) -> str:
    import screen_brightness_control as sbc
    percent = max(0, min(100, percent))
    sbc.set_brightness(percent)
    return f"Brightness {percent} percent."


def brightness_step(delta: int) -> str:
    import screen_brightness_control as sbc
    current = sbc.get_brightness(display=0)[0]
    return set_brightness(current + delta)
