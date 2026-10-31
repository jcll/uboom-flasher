"""A simulated UBoom L in update mode: boot ROM and helper command sets, NOR flash semantics (erase sets
0xFF, programming can only clear bits), CRC32 verify on the device, the discard address, and the
restart (0x18 then 0xB0). Synthetic data only."""
import os
import random
import struct
import zlib

from uboom_flasher import adfu

FLASH = 0x200000


def nvram_record(name, value):
    name = name.encode() + b"\0"
    return b"\x49\xff\x00\x00\x00" + bytes([len(name)]) + struct.pack("<H", len(value)) + name + value


def synthetic_package(seed=1):
    rng = random.Random(seed)
    data = lambda n: bytes(rng.getrandbits(8) for _ in range(n))  # noqa: E731
    return {
        adfu.HELPER_FILE: data(9536),
        "mbrec.bin": data(4096),
        "zephyr.bin": data(981_104),  # the sizes of the 1.2.7 images, so erase ranges match EarFun's tool
        "sdfs.bin": data(755_752),
        "nvram.bin": b"NVSR" + data(1532),
    }


def synthetic_flash(package, mac=b"0123456789AB"):
    img = bytearray(b"\xff" * FLASH)
    for name, off in (("mbrec.bin", 0), ("zephyr.bin", 0x11000), ("sdfs.bin", 0x132000), ("nvram.bin", 0x1F0000)):
        img[off:off + len(package[name])] = package[name]
    rw = b"NVSR\xff\x0a\x01\x00\x01\x10\x00\x10\x00\x00\x00\x00" + nvram_record("BT_MAC", mac)
    img[0x1F2000:0x1F2000 + len(rw)] = rw
    user = b"NVSR" + bytes(12) + nvram_record("VM_BT_PLIST0", bytes(range(48))) + nvram_record("EQ", b"105")
    img[0x1F8000:0x1F8000 + len(user)] = user
    return img


class Speaker:
    def __init__(self, flash, corrupt_sector=None, pull_at_sector=None, stuck_after_erase=None):
        """corrupt_sector: flip a bit in that sector while programming it (its verify then fails);
        pull_at_sector: the cable is pulled when a write reaches that sector (earlier sectors are written);
        stuck_after_erase: {address: value} left behind by every erase that covers the address."""
        self.flash = bytearray(flash)
        self.pull_at_sector = pull_at_sector
        self.stuck_after_erase = stuck_after_erase or {}
        self.ram = bytearray(0x80000)
        self.stage = "rom"
        self.pending = None
        self.reset_stored = self.restarted = False
        self.corrupt_sector = corrupt_sector
        self.erases = []

    @staticmethod
    def csw(status=0):
        return b"USBS" + bytes(8) + bytes([status])

    def handle(self, blk):
        if self.pending is not None:
            kind, addr, n, buf = self.pending
            buf += blk
            if len(buf) < n:
                self.pending = (kind, addr, n, buf)
                return b""
            self.pending = None
            return self.finish_out(kind, addr, bytes(buf[:n]))
        assert blk[:4] == b"USBC" and len(blk) == 31
        dlen, flags, cb = struct.unpack_from("<I", blk, 8)[0], blk[12], bytes(blk[15:31])
        sub, length, addr = cb[1], struct.unpack_from("<I", cb, 5)[0], struct.unpack_from("<I", cb, 9)[0]
        if self.stage == "rom":
            if cb[0] == 0xCC:
                return b"\x00CADFUD\x16QA\x00" + self.csw()
            if sub == 0x13:
                self.pending = ("ram", addr, length, bytearray())
                return b""
            if sub == 0x93:
                return bytes(self.ram[addr:addr + length]) + self.csw()
            if sub == 0x20:
                self.stage = "helper"
                return self.csw()
            raise AssertionError(f"unexpected ROM command {cb.hex()}")
        if cb[0] == 0xB0:
            assert self.reset_stored
            self.restarted = True
            return self.csw()
        assert cb[0] == 0xCD and cb[2] == 0, cb.hex()
        if sub == 0x16:
            return self.csw()
        if sub == 0x15:
            assert dlen == 512 and flags == 0x80
            return bytes(4) + bytes.fromhex("c8401500") + bytes(504) + self.csw()
        if sub == 0x14:
            assert length == 0 and dlen == 512
            self.pending = ("info", 0, 512, bytearray())
            return b""
        if sub == 0x90:
            assert dlen == length * 512 and flags == 0x80
            return bytes(self.flash[addr * 512:(addr + length) * 512]) + self.csw()
        if sub == 0x17:
            a, n = addr * 512, length * 512
            assert a % 0x10000 == 0 and n % 0x10000 == 0, "erase not 64 KB aligned"
            self.flash[a:a + n] = b"\xff" * n
            for where, value in self.stuck_after_erase.items():
                if a <= where < a + n:
                    self.flash[where] = value
            self.erases.append((a, a + n))
            return self.csw()
        if sub == 0x10:
            assert dlen == length * 512
            self.pending = ("discard" if addr >> 24 == 0xFF else "flash", addr, dlen, bytearray())
            return b""
        if sub == 0x30:
            assert length == 12 and dlen == 12
            self.pending = ("verify", 0, 12, bytearray())
            return b""
        if sub == 0x18:
            assert cb[3] == 2
            self.reset_stored = True
            return self.csw()
        raise AssertionError(f"unexpected helper command {cb.hex()}")

    def finish_out(self, kind, addr, data):
        if kind == "ram":
            self.ram[addr:addr + len(data)] = data
        elif kind == "flash":
            off = addr * 512
            if self.pull_at_sector is not None and addr <= self.pull_at_sector < addr + len(data) // 512:
                done = (self.pull_at_sector - addr) * 512
                for i, b in enumerate(data[:done]):
                    self.flash[off + i] &= b
                self.pull_at_sector = None
                raise adfu.usb.core.USBError("simulated: cable pulled")
            if self.corrupt_sector is not None and addr <= self.corrupt_sector < addr + len(data) // 512:
                data = bytearray(data)
                data[(self.corrupt_sector - addr) * 512] ^= 0x01
            for i, b in enumerate(data):
                self.flash[off + i] &= b
        elif kind == "verify":
            start, n, crc = struct.unpack("<III", data)
            return self.csw(0 if zlib.crc32(bytes(self.flash[start:start + n])) == crc else 2)
        return self.csw()


class FakeLink(adfu.Link):
    def __init__(self, speaker, log):
        self.dev, self.log, self.pending, self.queue = speaker, log, b"", b""

    def write(self, data, quiet=False, timeout_ms=0):
        self.queue += self.dev.handle(bytes(data))

    def read(self, n, timeout_ms=0, quiet=False):
        assert len(self.queue) >= n, f"device has {len(self.queue)} bytes, host wants {n}"
        out, self.queue = self.queue[:n], self.queue[n:]
        return out

    def drain(self):
        pass


def install(monkeypatch, speaker, package, tmp_path):
    """Points the tool at the simulated speaker and package; returns a dummy .fw path."""
    from uboom_flasher import operations
    monkeypatch.setattr(adfu, "open_device", lambda log: FakeLink(speaker, log))
    monkeypatch.setattr(adfu, "package_files", lambda path: package)
    monkeypatch.setattr(operations, "KNOWN_HELPER", operations.sha256(package[adfu.HELPER_FILE]))
    monkeypatch.setattr("time.sleep", lambda s: None)
    fw = tmp_path / "test.fw"
    fw.write_bytes(os.urandom(64))
    return str(fw)
