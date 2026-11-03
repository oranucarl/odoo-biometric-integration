#!/usr/bin/env python3
"""
Capture a fingerprint image from a DigitalPersona U.are.U 4000B / 4500 reader
on macOS (or Linux) using libusb, and save it as a PNG.

Protocol ported from libfprint's uru4000 driver
(libfprint/drivers/uru4000.c, LGPL-2.1, Daniel Drake & Timo Teräs).

Usage:
    python uru4500_capture.py [output.png] [--raw] [--timeout SECONDS]
"""

import argparse
import os
import random
import struct
import sys
import time

import usb.core
import usb.util
import usb.backend.libusb1
from PIL import Image, ImageOps

VENDOR_ID = 0x05BA
PRODUCT_ID = 0x000A

EP_INTR = 0x81
EP_DATA = 0x82
USB_RQ = 0x04
CTRL_IN = 0xC0   # vendor | device | in
CTRL_OUT = 0x40  # vendor | device | out
CTRL_TIMEOUT = 5000
IRQ_LENGTH = 64

IMAGE_HEIGHT = 290
IMAGE_WIDTH = 384
HEADER_LEN = 64
IMAGE_BUF_LEN = HEADER_LEN + IMAGE_HEIGHT * IMAGE_WIDTH
ENC_THRESHOLD = 5000

IRQDATA_SCANPWR_ON = 0x56AA
IRQDATA_FINGER_ON = 0x0101
IRQDATA_FINGER_OFF = 0x0200
IRQDATA_DEATH = 0x0800

REG_HWSTAT = 0x07
REG_SCRAMBLE_DATA_INDEX = 0x33
REG_SCRAMBLE_DATA_KEY = 0x34
REG_MODE = 0x4E
REG_DEVICE_INFO = 0xF0

MODE_AWAIT_FINGER_ON = 0x10
MODE_AWAIT_FINGER_OFF = 0x12
MODE_CAPTURE = 0x20
MODE_OFF = 0x70

BLOCKF_CHANGE_KEY = 0x80
BLOCKF_NO_KEY_UPDATE = 0x04
BLOCKF_ENCRYPTED = 0x02
BLOCKF_NOT_PRESENT = 0x01

MASK32 = 0xFFFFFFFF


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- device I/O

class Reader:
    def __init__(self):
        self.dev = find_device()
        self.interface = None

    def open(self):
        try:
            self.dev.set_configuration()
        except usb.core.USBError:
            pass  # already configured
        cfg = self.dev.get_active_configuration()
        for intf in cfg:
            if (intf.bInterfaceClass, intf.bInterfaceSubClass, intf.bInterfaceProtocol) == (255, 255, 255):
                self.interface = intf.bInterfaceNumber
                break
        if self.interface is None:
            raise RuntimeError("Could not find the fingerprint interface on the device")
        try:
            if self.dev.is_kernel_driver_active(self.interface):
                self.dev.detach_kernel_driver(self.interface)
        except (NotImplementedError, usb.core.USBError):
            pass
        usb.util.claim_interface(self.dev, self.interface)

    def close(self):
        try:
            self.write_reg(REG_MODE, MODE_OFF)
        except usb.core.USBError:
            pass
        if self.interface is not None:
            usb.util.release_interface(self.dev, self.interface)
        usb.util.dispose_resources(self.dev)

    def read_regs(self, reg, n):
        return bytes(self.dev.ctrl_transfer(CTRL_IN, USB_RQ, reg, 0, n, CTRL_TIMEOUT))

    def read_reg(self, reg):
        return self.read_regs(reg, 1)[0]

    def write_regs(self, reg, data):
        written = self.dev.ctrl_transfer(CTRL_OUT, USB_RQ, reg, 0, bytes(data), CTRL_TIMEOUT)
        if written != len(data):
            raise RuntimeError(f"Short register write at 0x{reg:x}")

    def write_reg(self, reg, value):
        self.write_regs(reg, [value])

    def read_irq(self, timeout_ms):
        """Return the 16-bit interrupt type, or None on timeout."""
        try:
            data = bytes(self.dev.read(EP_INTR, IRQ_LENGTH, timeout_ms))
        except usb.core.USBTimeoutError:
            return None
        irq = struct.unpack(">H", data[:2])[0]
        if irq == IRQDATA_DEATH:
            log("warning: device sent the 0x0800 'failure' interrupt; next scan may be bad")
        return irq

    def wait_irq(self, wanted, timeout_s):
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = int((deadline - time.monotonic()) * 1000)
            if remaining <= 0:
                return False
            irq = self.read_irq(remaining)
            if irq == wanted:
                return True

    # ------------------------------------------------------------ init

    def power_up(self):
        for attempt in range(3):
            hwstat = self.read_reg(REG_HWSTAT)

            # Device in a confused power state: reboot its power.
            if (hwstat & 0x84) == 0x84:
                self.write_reg(REG_HWSTAT, hwstat & 0x0F)
                for _ in range(100):
                    hwstat = self.read_reg(REG_HWSTAT)
                    if hwstat & 0x01:
                        break
                    time.sleep(0.01)
                else:
                    raise RuntimeError("Could not reboot device power")

            # Power down first, if it is up.
            if (hwstat & 0x80) == 0:
                self.write_reg(REG_HWSTAT, hwstat | 0x80)

            # Power up and wait for the low-power bit to clear.
            target = hwstat & 0x0F
            for _ in range(100):
                self.write_reg(REG_HWSTAT, target)
                if (self.read_reg(REG_HWSTAT) & 0x80) == 0:
                    break
                time.sleep(0.01)
            else:
                raise RuntimeError("Could not power the device up")

            if self.wait_irq(IRQDATA_SCANPWR_ON, 1.0):
                info = self.read_regs(REG_DEVICE_INFO, 16)
                log(f"Reader powered up (versions {info[10]:02x}{info[11]:02x} / {info[4]:02x}{info[5]:02x})")
                return
            log(f"Power-up interrupt did not arrive, retrying ({attempt + 1}/3)")
        raise RuntimeError("Power-up timed out 3 times")

    # ------------------------------------------------------------ capture

    def capture(self, finger_timeout_s):
        self.write_reg(REG_MODE, MODE_AWAIT_FINGER_ON)
        log("Place your finger on the reader...")
        if not self.wait_irq(IRQDATA_FINGER_ON, finger_timeout_s):
            raise TimeoutError("No finger detected")
        log("Finger detected, capturing")

        for attempt in range(5):
            self.write_reg(REG_MODE, MODE_CAPTURE)
            buf = bytearray(self.dev.read(EP_DATA, IMAGE_BUF_LEN, 5000))
            num_lines = struct.unpack_from("<H", buf, 4)[0]
            if num_lines >= IMAGE_HEIGHT or len(buf) < num_lines * IMAGE_WIDTH + HEADER_LEN:
                log(f"Bad frame ({num_lines} lines, {len(buf)} bytes), retrying")
                continue
            buf.extend(b"\0" * (IMAGE_BUF_LEN - len(buf)))
            return self.decode(buf, num_lines)
        raise RuntimeError("Could not get a valid frame from the reader")

    def fetch_key(self, key_number, seed):
        self.write_regs(REG_SCRAMBLE_DATA_INDEX, bytes([key_number & 0xFF]) + struct.pack("<I", seed))
        raw = struct.unpack("<I", self.read_regs(REG_SCRAMBLE_DATA_KEY, 4))[0]
        return raw ^ seed

    def decode(self, buf, num_lines):
        key_number = buf[6]
        blocks = [[buf[16 + 2 * i], buf[17 + 2 * i]] for i in range(15)]
        data = memoryview(buf)[HEADER_LEN:]

        if calc_dev2(data, blocks) < ENC_THRESHOLD:
            log("Image is not scrambled")
        else:
            log("Image is scrambled, fetching key from reader")
            seed = random.randrange(0, 2**31)
            key = self.fetch_key(key_number, seed)
            lines_done = 0
            block = 0
            while block < 15 and lines_done < num_lines:
                flags, n = blocks[block]
                if n == 0:
                    break
                if lines_done + n > IMAGE_HEIGHT:
                    raise RuntimeError("Bad frame: block overflows image")
                if flags & BLOCKF_CHANGE_KEY:
                    blocks[block][0] &= ~BLOCKF_CHANGE_KEY
                    key_number += 1
                    seed = random.randrange(0, 2**31)
                    key = self.fetch_key(key_number, seed)
                    continue
                start = lines_done * IMAGE_WIDTH
                nbytes = n * IMAGE_WIDTH
                mode = flags & (BLOCKF_NO_KEY_UPDATE | BLOCKF_ENCRYPTED)
                if mode == BLOCKF_ENCRYPTED:
                    key = do_decode(data, start, nbytes, key)
                elif mode == 0:
                    for _ in range(nbytes):
                        key = update_key(key)
                if not (flags & BLOCKF_NOT_PRESENT):
                    lines_done += n
                block += 1

        # Assemble the output image from the blocks.
        out = bytearray(IMAGE_WIDTH * IMAGE_HEIGHT)
        r = to = 0
        for flags, n in blocks:
            if r >= num_lines or n == 0:
                break
            if r + n > IMAGE_HEIGHT or to + n * IMAGE_WIDTH > len(out):
                raise RuntimeError("Bad frame: block overflows image")
            out[to:to + n * IMAGE_WIDTH] = data[r * IMAGE_WIDTH:(r + n) * IMAGE_WIDTH]
            if not (flags & BLOCKF_NOT_PRESENT):
                r += n
            to += n * IMAGE_WIDTH
        return bytes(out)

    def wait_finger_off(self, timeout_s=10):
        self.write_reg(REG_MODE, MODE_AWAIT_FINGER_OFF)
        log("Lift your finger")
        self.wait_irq(IRQDATA_FINGER_OFF, timeout_s)


# ---------------------------------------------------------------- unscrambling

def update_key(key):
    # linear feedback shift register, taps at 1 3 4 7 11 13 20 23 26 29 32
    bit = key & 0x9248144D
    bit ^= (bit << 16) & MASK32
    bit ^= (bit << 8) & MASK32
    bit ^= (bit << 4) & MASK32
    bit ^= (bit << 2) & MASK32
    bit ^= (bit << 1) & MASK32
    return (bit & 0x80000000) | (key >> 1)


def do_decode(data, start, num_bytes, key):
    for i in range(start, start + num_bytes - 1):
        xorbyte = (((key >> 4) & 1)
                   | ((key >> 8) & 1) << 1
                   | ((key >> 11) & 1) << 2
                   | ((key >> 14) & 1) << 3
                   | ((key >> 18) & 1) << 4
                   | ((key >> 21) & 1) << 5
                   | ((key >> 24) & 1) << 6
                   | ((key >> 29) & 1) << 7)
        key = update_key(key)
        data[i] = data[i + 1] ^ xorbyte
    data[start + num_bytes - 1] = 0
    return update_key(key)


def calc_dev2(data, blocks):
    """Noise measure of the first two lines; high means the image is scrambled."""
    rows = []
    r = 0
    for flags, n in blocks:
        if len(rows) >= 2:
            break
        if flags & BLOCKF_NOT_PRESENT:
            continue
        for _ in range(n):
            if len(rows) >= 2:
                break
            rows.append(data[r * IMAGE_WIDTH:(r + 1) * IMAGE_WIDTH])
            r += 1
    if len(rows) < 2:
        return 0
    sums = [a + b for a, b in zip(rows[0], rows[1])]
    mean = sum(sums) // IMAGE_WIDTH
    return sum((s - mean) ** 2 for s in sums) // IMAGE_WIDTH


# ---------------------------------------------------------------- helpers

def find_device():
    backend = None
    for path in ("/opt/homebrew/lib/libusb-1.0.dylib", "/usr/local/lib/libusb-1.0.dylib"):
        if os.path.exists(path):
            backend = usb.backend.libusb1.get_backend(find_library=lambda _: path)
            break
    dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID, backend=backend)
    if dev is None:
        raise RuntimeError("U.are.U 4500 reader not found. Is it plugged in?")
    return dev


def raw_image(pixels):
    return Image.frombytes("L", (IMAGE_WIDTH, IMAGE_HEIGHT), pixels)


def normalize(img):
    # The sensor reports ridges as light; invert so ridges are dark like ink,
    # rotate 180 degrees as libfprint does for this model, and stretch the
    # contrast since raw frames are quite faint.
    return ImageOps.autocontrast(ImageOps.invert(img).rotate(180), cutoff=1)


def save_png(pixels, path, raw_path=None):
    img = raw_image(pixels)
    if raw_path:
        img.save(raw_path)
    normalize(img).save(path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("output", nargs="?", default="fingerprint.png")
    ap.add_argument("--raw", action="store_true", help="also save the unprocessed sensor image")
    ap.add_argument("--timeout", type=float, default=30, help="seconds to wait for a finger")
    args = ap.parse_args()

    reader = Reader()
    reader.open()
    try:
        reader.power_up()
        pixels = reader.capture(args.timeout)
        raw_path = os.path.splitext(args.output)[0] + "_raw.png" if args.raw else None
        save_png(pixels, args.output, raw_path)
        log(f"Saved {args.output}")
        reader.wait_finger_off()
    finally:
        reader.close()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, TimeoutError, usb.core.USBError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
