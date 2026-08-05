"""gateway/api/led_control.py — Uno Q RGB status LED, extracted from
recv_verify.py (Phase 8b2 task 1).

Pure sysfs I/O with no dependency on recv_verify's shared state, so unlike
the other gateway.api modules this one needs no lazy `import recv_verify`
back-reference.
"""


def _write_led(channel: str, value: int) -> None:
    """Write brightness to a Linux sysfs LED channel.
    Silently does nothing if the path does not exist (laptop mode).
    On Arduino Uno Q: /sys/class/leds/{channel}/brightness exists.
    """
    try:
        with open(f"/sys/class/leds/{channel}/brightness", "w") as fh:
            fh.write(str(int(value)))
    except (FileNotFoundError, PermissionError, OSError):
        pass  # not on Uno Q hardware — silent no-op


def led_set_status(worst_state: str) -> None:
    """Reflect gateway AI state on the Uno Q's Linux-controlled RGB LED.
    LED D27301 channels: red:user, green:user, blue:user
    Called once per second with the worst satellite state across the fleet.
    States:
      OK      -> green solid  (all satellites healthy)
      WARN    -> amber        (at least one satellite in warning)
      FAULT   -> red solid    (bearing fault confirmed)
      TRIPPED -> magenta      (interlock tripped — future use)
    """
    colours = {
        "OK":      (0,   255, 0),
        "WARN":    (255, 128, 0),
        "FAULT":   (255, 0,   0),
        "TRIPPED": (255, 0,   128),
    }
    r, g, b = colours.get(worst_state, (0, 0, 0))
    _write_led("red:user",   r)
    _write_led("green:user", g)
    _write_led("blue:user",  b)
