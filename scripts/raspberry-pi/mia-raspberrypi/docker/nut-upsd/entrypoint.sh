#!/bin/sh
# Start the USB HID driver, then upsd in the foreground. If the driver dies (station
# unplugged, USB reset) the loop restarts it; if upsd dies the container exits and
# Docker restarts it.
set -u
mkdir -p /run/nut && chown nut:nut /run/nut && chmod 770 /run/nut

start_driver() {
  upsdrvctl -u root start >/dev/null 2>&1
}

(
  while true; do
    # no pgrep in the slim image: the driver's own pid file says whether it is alive
    if ! kill -0 "$(cat /run/nut/usbhid-ups-ups.pid 2>/dev/null)" 2>/dev/null; then
      echo "nut-upsd: starting usbhid-ups"
      start_driver || echo "nut-upsd: driver did not start (UPS unplugged?) - retry in 10 s"
    fi
    sleep 10
  done
) &

exec upsd -F -u nut
