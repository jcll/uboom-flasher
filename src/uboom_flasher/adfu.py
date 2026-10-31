"""USB update-mode protocol of the EarFun UBoom L (Actions ATS2833), as EarFun's Windows tool uses it.

Two stages, both speaking 31-byte "USBC" command blocks and 13-byte "USBS" status replies over bulk
endpoints 0x02/0x81:
  1. The boot ROM (10d6:10d6): 0xCC identify, 0xCD/0x13 write RAM, 0xCD/0x93 read RAM, 0xCD/0x20 run.
  2. The helper program mp_usb_andes.bin from the firmware package, loaded into RAM at 0x69000 and
     started by the ROM. It only accepts 0xCD (and 0xB0) command blocks. Subcode in CB[1], storage
     index in CB[2], length in CB[5..8], address in CB[9..12]; flash addresses and lengths are in
     512-byte sectors. See docs/PROTOCOL.md.
"""
import struct
import time
import zlib

import usb.backend.libusb1
import usb.core
import usb.util

from . import romstage as P

VID = PID = 0x10D6
HELPER_FILE = "mp_usb_andes.bin"
HELPER_ADDR = 0x69000
SECTOR = 512

# Helper subcodes (CB[1]); bit 7 set means data flows device -> host.
DISK_INIT = 0x16
STORAGE_INFO = 0x15
SET_STORAGE_INFO = 0x14
FLASH_READ = 0x90
FLASH_WRITE = 0x10
ERASE = 0x17
VERIFY = 0x30
RESET = 0x18
DISCARD_ADDR = 0xFF000000  # blocks written here are read and thrown away by the helper ("transfer over")

UPDATE_MODE_HELP = ("Put the speaker in update mode: switch it off, hold the Bluetooth button and plug the USB-C "
                    "cable straight into the computer (not through a hub or monitor).")


def package_files(path):
    """UBoom L .fw = 16-byte header (u32 FAT image length at 0x0C) + FAT32 image; files XOR 0x38."""
    with open(path, "rb") as f:
        d = f.read()
    if d[:12] != b"100TMFWFSTCA":
        raise ValueError(f"{path} is not an Actions .fw package")
    length = struct.unpack_from("<I", d, 0x0C)[0]
    files = P.read_fat32(d[0x10:0x10 + length])
    return {n: P.xor_decode(raw) for n, raw in files.items() if raw[:16] != P.ENCRYPTED_MAGIC}


def usb_backend():
    """The libusb bundled with the libusb-package wheel, else whatever libusb pyusb finds itself."""
    try:
        import libusb_package
        backend = libusb_package.get_libusb1_backend()
        if backend is not None:
            return backend
    except ImportError:
        pass
    return usb.backend.libusb1.get_backend()


class Link:
    """Bulk endpoints read as a byte stream. Reads always ask for whole 512-byte packets and keep the
    surplus: on macOS, asking libusb for fewer bytes than the device sends raises an Overflow error."""

    def __init__(self, dev, ep_out, ep_in, log):
        self.dev, self.ep_out, self.ep_in, self.log = dev, ep_out, ep_in, log
        self.pending = b""

    def write(self, data, quiet=True, timeout_ms=10000):
        if not quiet:
            self.log(f"   -> {len(data):6d} B  {bytes(data[:32]).hex(' ')}")
        for off in range(0, len(data), 0x10000):  # the device sees one continuous bulk stream either way
            self.dev.write(self.ep_out, data[off:off + 0x10000], timeout=timeout_ms)

    def read(self, n, timeout_ms=10000, quiet=True):
        while len(self.pending) < n:
            want = -(-(n - len(self.pending)) // 512) * 512
            chunk = bytes(self.dev.read(self.ep_in, want, timeout=timeout_ms))
            if not quiet:
                self.log(f"   <- {len(chunk):6d} B  {chunk[:32].hex(' ')}")
            self.pending += chunk
        out, self.pending = self.pending[:n], self.pending[n:]
        return out

    def drain(self):
        try:
            while True:
                stale = self.read(1, timeout_ms=200, quiet=True)
                self.log(f"drained stale byte {stale.hex()}")
        except usb.core.USBTimeoutError:
            self.pending = b""


def open_device(log):
    backend = usb_backend()
    if backend is None:
        raise SystemExit("No libusb found. Reinstall with the libusb-package dependency, or install libusb.")
    dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
    if dev is None:
        raise SystemExit("No speaker in update mode (USB 10d6:10d6). " + UPDATE_MODE_HELP)
    dev.set_configuration()
    intf = dev.get_active_configuration()[(0, 0)]

    def ep(direction):
        return usb.util.find_descriptor(
            intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == direction)

    ep_out, ep_in = ep(usb.util.ENDPOINT_OUT), ep(usb.util.ENDPOINT_IN)
    log(f"speaker found: endpoints OUT 0x{ep_out.bEndpointAddress:02x}, IN 0x{ep_in.bEndpointAddress:02x}")
    link = Link(dev, ep_out.bEndpointAddress, ep_in.bEndpointAddress, log)
    link.drain()
    return link


def start_helper(link, helper, log):
    """ROM stage: identify, load the helper into RAM, read it back, start it."""
    rom = P.RomSession(link)
    data, status = rom.inquiry()
    if status != 0 or b"ADFU" not in data:
        raise SystemExit(f"The boot ROM did not answer as expected ({data!r}). Unplug, re-enter update mode, retry.")
    rom.write_ram(HELPER_ADDR, helper, 4096)
    if rom.read_ram(HELPER_ADDR, len(helper)) != helper:
        raise SystemExit("Helper read-back from RAM did not match; not starting it.")
    rom.run(HELPER_ADDR)
    log(f"helper program ({len(helper)} bytes) loaded into RAM, verified and started")
    time.sleep(0.1)
    return Helper(link, log)


class Helper:
    """Talks to the running helper program (stage 2)."""

    def __init__(self, link, log):
        self.link, self.log, self.tag = link, log, 0x100

    def command(self, sub, storage=0, b3=0, length=0, address=0, data_in=0, data_out=b"", quiet=True,
                timeout_ms=10000):
        self.tag += 1
        cb = bytearray(16)
        cb[0], cb[1], cb[2], cb[3] = 0xCD, sub, storage, b3
        struct.pack_into("<II", cb, 5, length, address)
        n = data_in or len(data_out)
        self.link.write(P.cbw(self.tag, n, bool(data_in), cb), quiet=quiet)
        if data_out:
            self.link.write(data_out, quiet=quiet, timeout_ms=timeout_ms)
        reply = self.link.read(data_in, quiet=quiet) if data_in else b""
        status = P.parse_csw(self.link.read(13, quiet=quiet, timeout_ms=timeout_ms))
        if status != 0:
            raise IOError(f"helper command 0x{sub:02x} failed (status {status})")
        return reply

    def disk_init(self, storage=0):
        self.command(DISK_INIT, storage)

    def storage_info(self, storage=0):
        return self.command(STORAGE_INFO, storage, length=SECTOR, data_in=SECTOR)

    def read_sectors(self, lba, count, storage=0):
        return self.command(FLASH_READ, storage, length=count, address=lba, data_in=count * SECTOR)

    def set_storage_info(self, block, storage=0):
        """0x14 "DOWN HARDWARE INFO": EarFun's tool sends the 0x15 block straight back."""
        self.command(SET_STORAGE_INFO, storage, data_out=block)

    def erase(self, lba, count, storage=0):
        # A 64 KB block erase can take over a second on this kind of chip; allow 2 s per block.
        self.command(ERASE, storage, length=count, address=lba, timeout_ms=10000 + count // 128 * 2000)

    def write_sectors(self, lba, data, storage=0):
        """Programs whole sectors; the caller pads with 0xFF as EarFun's tool does."""
        assert len(data) % SECTOR == 0
        self.command(FLASH_WRITE, storage, length=len(data) // SECTOR, address=lba, data_out=data,
                     timeout_ms=10000 + len(data) // 1024 * 30)

    def verify(self, byte_addr, data, storage=0):
        """0x30: the helper CRC32s flash [byte_addr, +len) and fails the command on a mismatch."""
        payload = struct.pack("<III", byte_addr, len(data), zlib.crc32(data))
        self.command(VERIFY, storage, length=len(payload), data_out=payload,
                     timeout_ms=10000 + len(data) // 1024 * 10)

    def transfer_over(self):
        self.command(FLASH_WRITE, length=1, address=DISCARD_ADDR, data_out=bytes(SECTOR))

    def reset(self, delay_s=2):
        """Restart, as EarFun's tool ends an update: 0x18 only stores the mode (CB[2]: 0 restart) and the
        delay in seconds (CB[3]); the 0xB0 "detach" block then makes the helper act on it."""
        self.command(RESET, b3=delay_s)
        self.tag += 1
        cb = bytearray(16)
        cb[0] = 0xB0
        self.link.write(P.cbw(self.tag, 0, False, cb), quiet=True)
        status = P.parse_csw(self.link.read(13, quiet=True))
        if status != 0:
            raise IOError(f"helper detach failed (status {status})")
