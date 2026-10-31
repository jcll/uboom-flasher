"""Boot-ROM stage of the Actions ADFU protocol and the .fw (FAT32) package reader.

Copied verbatim (apart from this docstring and dropping the unused Link base class) from actions-adfu,
https://github.com/sonigeez/actions-adfu at commit 8ba5f6e5ee4c8643d2254ccc87e0abbc7f0f86b1
(src/actions_adfu/protocol.py and firmware.py), under its MIT License:

  MIT License

  Copyright (c) 2026 bharat

  Permission is hereby granted, free of charge, to any person obtaining a copy
  of this software and associated documentation files (the "Software"), to deal
  in the Software without restriction, including without limitation the rights
  to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
  copies of the Software, and to permit persons to whom the Software is
  furnished to do so, subject to the following conditions:

  The above copyright notice and this permission notice shall be included in all
  copies or substantial portions of the Software.

  THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
  IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
  FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
  AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
  LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
  OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
  SOFTWARE.
"""
import re
import struct


XOR_KEY = 0x38
ENCRYPTED_MAGIC = bytes.fromhex("112233445566778899aabbccddeeff75")


def xor_decode(data: bytes) -> bytes:
    return bytes(b ^ XOR_KEY for b in data)


def read_fat32(img: bytes):
    """Minimal FAT32 reader with long-name support. Returns {path: bytes}."""
    bps, spc, rsv, nfat = struct.unpack_from("<HBHB", img, 11)
    spf = struct.unpack_from("<I", img, 36)[0]
    root = struct.unpack_from("<I", img, 44)[0]
    fat = rsv * bps
    data = (rsv + nfat * spf) * bps
    cs = bps * spc

    def chain(c):
        while 2 <= c < 0x0FFFFFF8:
            yield c
            c = struct.unpack_from("<I", img, fat + 4 * c)[0] & 0x0FFFFFFF

    def read(c, size=None):
        b = b"".join(img[data + (x - 2) * cs:data + (x - 1) * cs] for x in chain(c))
        return b if size is None else b[:size]

    out = {}

    def walk(c, prefix):
        raw, lfn = read(c), []
        for i in range(0, len(raw), 32):
            e = raw[i:i + 32]
            if e[0] == 0:
                break
            if e[0] == 0xE5:
                lfn = []
                continue
            if e[11] == 0x0F:
                lfn.insert(0, e[1:11] + e[14:26] + e[28:32])
                continue
            if lfn:
                name = b"".join(lfn).decode("utf-16le", "ignore")
                name = name.split("\0")[0].rstrip("￿")
                # The Actions packer appends a "~<n>" version suffix to long names.
                name = re.sub(r"~\d+$", "", name)
            else:
                base, ext = e[:8].decode("latin1").strip(), e[8:11].decode("latin1").strip()
                name = base + ("." + ext if ext else "")
            lfn = []
            if name in (".", "..") or e[11] & 0x08:
                continue
            cl = (struct.unpack_from("<H", e, 20)[0] << 16) | struct.unpack_from("<H", e, 26)[0]
            size = struct.unpack_from("<I", e, 28)[0]
            path = f"{prefix}{name}"
            if e[11] & 0x10:
                walk(cl, path + "/")
            else:
                out[path] = read(cl, size)

    walk(root, "")
    return out


def cbw(tag, data_len, direction_in, cb):
    cb = bytes(cb).ljust(16, b"\0")
    return (b"USBC" + struct.pack("<IIBBB", tag, data_len, 0x80 if direction_in else 0, 0, 16) + cb)


def parse_csw(raw):
    if len(raw) != 13 or raw[:4] != b"USBS":
        raise IOError(f"bad CSW: {raw.hex()}")
    return raw[12]


def rom_write_ram_cbw(tag, address, data_len):
    cb = bytearray(16)
    cb[0], cb[1] = 0xCD, 0x13
    struct.pack_into("<II", cb, 5, data_len, address)   # CBW bytes 20..27
    return cbw(tag, data_len, False, cb)


def rom_run_cbw(tag, entry):
    cb = bytearray(16)
    cb[0], cb[1] = 0xCD, 0x20
    struct.pack_into("<I", cb, 9, entry)                # CBW bytes 24..27 (ROM skips 17..23)
    return cbw(tag, 0, False, cb)


def rom_inquiry_cbw(tag, length=11):
    # The boot ROM (sub_2B8) skips CB[1:7] and reads the reply length as LE u16 at CB[7:9],
    # not dCBWDataTransferLength. The app firmware ignores those bytes.
    cb = bytearray(16)
    cb[0] = 0xCC
    struct.pack_into("<H", cb, 7, length)
    return cbw(tag, length, True, cb)


class RomSession:
    def __init__(self, link):
        self.link, self.tag = link, 1

    def _next(self):
        self.tag += 1
        return self.tag

    def inquiry(self):
        self.link.write(rom_inquiry_cbw(self._next()))
        data = self.link.read(11)
        status = parse_csw(self.link.read(13))
        return data, status

    def write_ram(self, address, blob, chunk=4096):
        for off in range(0, len(blob), chunk):
            part = blob[off:off + chunk]
            self.link.write(rom_write_ram_cbw(self._next(), address + off, len(part)))
            self.link.write(part)
            st = parse_csw(self.link.read(13))
            if st != 0:
                raise IOError(f"ROM write to 0x{address + off:08x} failed, CSW status {st}")

    def read_ram(self, address, length, chunk=4096):
        out = b""
        for off in range(0, length, chunk):
            n = min(chunk, length - off)
            cb = bytearray(16)
            cb[0], cb[1] = 0xCD, 0x93
            struct.pack_into("<II", cb, 5, n, address + off)
            self.link.write(cbw(self._next(), n, True, cb))
            out += self.link.read(n)
            st = parse_csw(self.link.read(13))
            if st != 0:
                raise IOError(f"ROM read from 0x{address + off:08x} failed, CSW status {st}")
        return out

    def run(self, entry):
        # The vendor tool sends the Thumb entry (0x2FF70001); the ROM ORs in bit 0 anyway.
        self.link.write(rom_run_cbw(self._next(), entry))
        st = parse_csw(self.link.read(13))
        if st != 0:
            raise IOError(f"ROM run failed, CSW status {st}")
