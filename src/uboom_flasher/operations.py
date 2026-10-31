"""Backup, firmware update and restore, in the order EarFun's Windows tool uses (see docs/PROTOCOL.md).

Update: clear the boot partition first, then for zephyr.bin, sdfs.bin and nvram.bin erase the file's extent
rounded out to 64 KB blocks, write it (0xFF-padded to whole sectors) and CRC-verify it on the speaker.
Before the boot record is written, the whole flash is read back and compared; only then is the boot record
written, checked and read back, and the speaker restarted. Until that last step the speaker has no boot
record, so after any failure it stays in update mode and can be flashed again.
Unlike EarFun's tool, the NVRAM after the factory template is put back by default, so the speaker keeps
its Bluetooth address and paired devices.
"""
import hashlib
import os
import struct
import time

from . import adfu
from .layout import ADDRESS_AREA, BLOCK, BOOT, FLASH_SIZE, IMAGES, KNOWN_HELPER, KNOWN_PACKAGES, NVRAM_KEEP

SIMILAR_BOOT = 0.9  # share of identical bytes expected between the speaker's boot record and a UBoom L one


def sha256(b):
    return hashlib.sha256(b).hexdigest()


def pad(data):
    return data + b"\xff" * (-len(data) % adfu.SECTOR)


def extent(offset, size):
    return offset // BLOCK * BLOCK, -(-(offset + size) // BLOCK) * BLOCK


def blank(b):
    return b.count(0xFF) == len(b)


def similarity(a, b):
    return sum(x == y for x, y in zip(a, b)) / max(len(a), 1)


def free_path(path):
    """path, or path with -2, -3, ... added before the extension if that file already exists."""
    base, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(path):
        n += 1
        path = f"{base}-{n}{ext}"
    return path


def save(path, data):
    """Writes data and a .sha256 file next to it, never over an existing file; returns the path used."""
    path = free_path(path)
    with open(path, "wb") as f:
        f.write(data)
    with open(os.path.splitext(path)[0] + ".sha256", "w") as f:
        f.write(f"{sha256(data)}  {os.path.basename(path)}\n")
    return path


def bt_address(image):
    """The BT_MAC record of the NVRAM factory-rw area as AA:BB:CC:DD:EE:FF, or None."""
    area = image[ADDRESS_AREA[0]:ADDRESS_AREA[1]]
    i = area.find(b"BT_MAC\x00")
    if i < 8:
        return None
    length = int.from_bytes(area[i - 2:i], "little")
    text = area[i + 7:i + 7 + length].split(b"\0")[0].decode("ascii", "replace")
    return ":".join(text[k:k + 2] for k in range(0, len(text), 2)) if len(text) == 12 else text


class Refusal(SystemExit):
    """A SystemExit whose message is already in the log."""


class Session:
    def __init__(self, data_dir, tag, allow_unknown=False):
        self.stamp = time.strftime("%Y%m%d-%H%M%S")
        self.backups = os.path.join(data_dir, "backups")
        logs = os.path.join(data_dir, "logs")
        os.makedirs(self.backups, exist_ok=True)
        os.makedirs(logs, exist_ok=True)
        self.log_path = free_path(os.path.join(logs, f"{tag}-{self.stamp}.log"))
        self.log_file = open(self.log_path, "w")
        self.allow_unknown = allow_unknown
        self.allow_unknown_device = False
        self.erased = self.boot_written = False

    def log(self, msg):
        print(msg, flush=True)
        self.log_file.write(msg + "\n")
        self.log_file.flush()

    def fail(self, msg):
        self.log_file.write(msg + "\n")
        self.log_file.flush()
        raise Refusal(msg)

    def load_package(self, path):
        try:
            files = adfu.package_files(path)
        except (OSError, ValueError, struct.error, IndexError) as e:
            self.fail(f"Cannot read the firmware package {path}: {e}")
        if adfu.HELPER_FILE not in files:
            self.fail(f"{path} is not a UBoom L firmware package ({adfu.HELPER_FILE} is missing).")
        return files

    def load_backup(self, path, require_checksum, need_boot_record):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            self.fail(f"Cannot read the backup {path}: {e}")
        if len(data) != FLASH_SIZE:
            self.fail(f"{path} is {len(data)} bytes, expected a full {FLASH_SIZE >> 20} MB backup.")
        sums = os.path.splitext(path)[0] + ".sha256"
        if os.path.exists(sums):
            with open(sums) as f:
                if f.read().split()[:1] != [sha256(data)]:
                    self.fail(f"{path} does not match its checksum file {os.path.basename(sums)}; it may be damaged.")
            self.log(f"backup {os.path.basename(path)}: checksum ok")
        elif require_checksum:
            self.fail(f"{path} has no checksum file ({os.path.basename(sums)}) next to it, so it cannot be checked. "
                      "Keep the .sha256 file uboom-flasher saved with the backup in the same folder.")
        else:
            self.log(f"backup {os.path.basename(path)}: no checksum file next to it, not checked")
        if need_boot_record and blank(data[:BOOT[2]]):
            self.fail(f"{path} was saved while an update was unfinished (it has no boot record), so it cannot be used "
                      "for this. Use an earlier backup of this speaker.")
        return data

    def guarded(self, step):
        """Runs the device part of a command; a USB failure or Ctrl-C is explained by how far it got."""
        try:
            return step()
        except Refusal:
            raise
        except SystemExit as e:  # the protocol layer's own refusals
            self.log_file.write(f"{e.code}\n")
            self.log_file.flush()
            raise
        except (IOError, adfu.usb.core.USBError, KeyboardInterrupt) as e:
            reason = "interrupted" if isinstance(e, KeyboardInterrupt) else str(e)
            if getattr(e, "errno", None) == 13:
                reason += " (on Linux, allow USB access with the udev rule in the README)"
            if not self.erased:
                self.fail(f"FAILED: {reason}\nNothing was erased. Unplug the speaker, put it back in update mode and "
                          "try again.")
            if not self.boot_written:
                self.fail(f"FAILED: {reason}\nThe boot record is erased and not rewritten yet, so the speaker stays in "
                          "update mode. Unplug it, enter update mode again and run the same command again. If the tool "
                          "then says the settings area is erased, add --use-backup-address as it explains.")
            self.fail(f"FAILED: {reason}\nEverything was written and verified, including the boot record; only the last "
                      "step failed. Unplug the speaker and switch it on. If it does not start normally, enter update mode "
                      "and run the same command again.")

    def connect(self, helper_bin, require_2mb=True):
        if sha256(helper_bin) != KNOWN_HELPER and not self.allow_unknown:
            self.fail("The helper program in this .fw is not the known UBoom L one; refusing.")
        self.link = adfu.open_device(self.log)
        self.helper = adfu.start_helper(self.link, helper_bin, self.log)
        self.helper.disk_init()
        self.info = self.helper.storage_info()
        chip = self.info[4:8]
        self.log(f"flash chip ID {chip.hex(' ')}")
        if require_2mb and chip[2] != 0x15:  # JEDEC capacity byte: 2**0x15 = 2 MB
            self.fail(f"Flash chip {chip.hex(' ')} is not a 2 MB part; this is not the expected layout.")
        self.helper.set_storage_info(self.info)  # as EarFun's tool does before erasing

    def read_flash(self, size=FLASH_SIZE):
        out = bytearray()
        for lba in range(0, size // adfu.SECTOR, 128):
            out += self.helper.read_sectors(lba, 128)
        return bytes(out)

    def read_twice(self, size=FLASH_SIZE):
        first = self.read_flash(size)
        if self.read_flash(size) != first:
            self.fail("Two reads of the flash disagree; nothing was saved or changed.")
        return first

    def snapshot(self, saved, use_backup_address=False):
        """Reads the whole flash, checks it belongs to the backup's speaker, and saves it."""
        now = self.read_twice()
        self.interrupted = blank(now[:BOOT[2]])  # an earlier run erased the boot record and did not finish
        a, b = ADDRESS_AREA
        if now[a:b] != saved[a:b]:
            backup_addr = bt_address(saved) or "unknown"
            if not use_backup_address:
                if blank(now[a:b]):
                    self.fail("The speaker's settings area (Bluetooth address) is erased. That happens when an earlier "
                              "update or restore stopped partway, or after --nvram reset. To finish, run the same command "
                              f"again with --use-backup-address: the Bluetooth address ({backup_addr}) and paired devices "
                              "then come from this backup. Use the newest backup of this same speaker; after a stopped "
                              "run that is usually the -pre-update.bin it saved when it started.")
                self.fail("The Bluetooth address area differs from the backup's: the backup is from another speaker, or "
                          "the address changed since (for example after --nvram reset). Make a new backup, or, only if "
                          "this backup is from this same speaker, add --use-backup-address.")
            self.log(f"--use-backup-address: the speaker gets the backup's Bluetooth address ({backup_addr}) and "
                     "paired devices")
        path = save(os.path.join(self.backups, f"uboom-l-{self.info[4:8].hex()}-{self.stamp}-pre-update.bin"), now)
        self.log(f"current flash saved to {path}")
        return now

    def check_product(self, now, reference_boot):
        """Refuses a device whose boot record does not look like a UBoom L's (unless a run was interrupted)."""
        if self.interrupted:
            return
        share = similarity(now[:BOOT[2]], pad(reference_boot)[:BOOT[2]])
        if share < SIMILAR_BOOT and not self.allow_unknown_device:
            self.fail(f"The speaker's boot record does not look like an EarFun UBoom L's ({share:.0%} identical to "
                      "this package's). This tool only knows the UBoom L flash layout; refusing.")
        self.log(f"boot record {share:.0%} identical to this package's")

    def write_region(self, offset, data, label):
        data = pad(data)
        self.log(f"  write {label}: 0x{offset:06x}-0x{offset + len(data):06x}")
        self.helper.write_sectors(offset // adfu.SECTOR, data)
        self.helper.verify(offset, data)
        self.log(f"  verified {label}")

    def erase_range(self, start, end):
        self.log(f"  erase 0x{start:06x}-0x{end:06x}")
        self.erased = True
        self.helper.erase(start // adfu.SECTOR, (end - start) // adfu.SECTOR)

    def check_before_boot(self, expected):
        """Reads the whole flash back while the boot record is still erased (expected: boot block blank)."""
        self.log("reading the whole flash back to compare, before writing the boot record")
        want = b"\xff" * BLOCK + expected[BLOCK:]
        final = self.read_flash()
        if final != want:
            bad = [o for o in range(0, FLASH_SIZE, 0x1000) if final[o:o + 0x1000] != want[o:o + 0x1000]]
            self.fail(f"READ-BACK MISMATCH in {len(bad)} 4 KB page(s): {', '.join(hex(o) for o in bad[:20])}\n"
                      "The boot record is not written, so the speaker stays in update mode: unplug it, enter update "
                      "mode again and run the same command again.")
        self.log("read-back matches")

    def finish(self, boot_data, label, expected):
        """Boot record last: write, CRC-verify, read back, then transfer-over and restart."""
        self.write_region(0, boot_data, label)
        self.boot_written = True
        if self.read_flash(BLOCK) != expected[:BLOCK]:
            self.fail("The boot block reads back differently from what was written. Unplug the speaker, enter update "
                      "mode again and run the same command again.")
        self.helper.transfer_over()
        self.log(f"flash matches the expected image (sha256 {sha256(expected)})")
        self.restart()

    def restart(self):
        try:
            self.helper.reset()
            self.log("restart sent; the speaker comes back on its own (or unplug it and switch it on)")
        except (IOError, adfu.usb.core.USBError) as e:
            self.log(f"restart not acknowledged ({e}); unplug the speaker and switch it on")


def confirm(dry_run, assume_yes, log, what):
    if dry_run:
        log("dry run: stopping before the first erase")
        return False
    if not assume_yes:
        try:
            answer = input(f"{what} Type 'flash' to continue: ")
        except EOFError:
            answer = ""
        if answer.strip() != "flash":
            log("cancelled")
            return False
    return True


def backup(firmware, data_dir):
    """Read-only: reads the whole flash twice and saves it only if both reads agree."""
    s = Session(data_dir, "backup")
    helper = s.load_package(firmware)[adfu.HELPER_FILE]

    def step():
        s.connect(helper, require_2mb=False)
        cap = s.info[6]
        size = 1 << cap if 0x13 <= cap <= 0x19 else FLASH_SIZE
        s.log(f"reading {size >> 10} KB, twice")
        image = s.read_twice(size)
        path = save(os.path.join(s.backups, f"uboom-l-{s.info[4:8].hex()}-{s.stamp}.bin"), image)
        with open(os.path.splitext(path)[0] + ".storage-info.bin", "wb") as f:
            f.write(s.info)
        s.log(f"backup saved: {path}")
        if blank(image[:BOOT[2]]):
            s.log("note: this speaker has no boot record (an update was interrupted); this backup cannot be restored")
        s.log("nothing was erased or written")
        s.restart()
        return path
    return s.guarded(step)


def write(firmware, backup_path, data_dir, nvram="keep", dry_run=False, assume_yes=False, allow_unknown=False,
          use_backup_address=False, allow_unknown_device=False):
    s = Session(data_dir, "update", allow_unknown)
    s.allow_unknown_device = allow_unknown_device
    try:
        with open(firmware, "rb") as f:
            version = KNOWN_PACKAGES.get(sha256(f.read()))
    except OSError as e:
        s.fail(f"Cannot read the firmware package {firmware}: {e}")
    if version is None and not allow_unknown:
        s.fail("Unknown .fw package (not a version this tool was checked with; see the README). "
               "Use --allow-unknown-package only if you know it is a UBoom L package.")
    files = s.load_package(firmware)
    for name, offset, limit in IMAGES + [BOOT]:
        if name not in files or offset + len(files[name]) > limit:
            s.fail(f"{name} is missing or too large for its partition; refusing.")
    s.log(f"package {os.path.basename(firmware)}: version {version or 'unknown'}")
    saved = s.load_backup(backup_path, require_checksum=use_backup_address, need_boot_record=use_backup_address)

    def step():
        s.connect(files[adfu.HELPER_FILE])
        current = s.snapshot(saved, use_backup_address)
        s.check_product(current, files[BOOT[0]])
        keep = NVRAM_KEEP[nvram]
        source = saved if use_backup_address else current
        if keep and s.interrupted and not use_backup_address and current[keep[0]:keep[1]] != saved[keep[0]:keep[1]]:
            s.fail("An earlier update stopped partway (the boot record is erased) and the speaker's settings differ "
                   "from the backup's, so they may be incomplete. Run the same command again with "
                   "--use-backup-address to take them from the backup (the -pre-update.bin saved by the stopped run "
                   "is the newest copy).")

        expected = bytearray(current)
        erases = [(0, BLOCK)] + [extent(off, len(files[n])) for n, off, _ in IMAGES]
        for a, b in erases:
            expected[a:b] = b"\xff" * (b - a)
        for name, off, _ in IMAGES + [BOOT]:
            expected[off:off + len(pad(files[name]))] = pad(files[name])
        if keep:
            a, b = keep
            expected[a:b] = source[a:b]

        s.log("plan: " + ", ".join(f"erase 0x{a:06x}-0x{b:06x}" for a, b in erases))
        s.log(f"plan: NVRAM after the factory template: {nvram} " + (
            f"(0x{keep[0]:06x}-0x{keep[1]:06x} put back from {'the backup' if use_backup_address else 'the current flash'})"
            if keep else "(wiped, as EarFun's tool does: new Bluetooth address, pair again; older backups then need "
                         "--use-backup-address to restore)"))
        if not confirm(dry_run, assume_yes, s.log, f"About to install {version or 'an unknown version'}."):
            s.restart()
            return

        s.erase_range(0, BLOCK)  # boot record gone first: until it is written last, the speaker stays in update mode
        for (name, off, _), (a, b) in zip(IMAGES, erases[1:]):
            s.erase_range(a, b)
            s.write_region(off, files[name], name)
            if name == "nvram.bin" and keep:
                ka, kb = keep
                s.write_region(ka, source[ka:kb], f"kept NVRAM 0x{ka:06x}-0x{kb:06x}")
        s.check_before_boot(bytes(expected))
        s.finish(files[BOOT[0]], BOOT[0], bytes(expected))
    s.guarded(step)


def restore(backup_path, firmware, data_dir, dry_run=False, assume_yes=False, allow_unknown=False,
            use_backup_address=False):
    s = Session(data_dir, "restore", allow_unknown)
    saved = s.load_backup(backup_path, require_checksum=True, need_boot_record=True)
    helper = s.load_package(firmware)[adfu.HELPER_FILE]

    def step():
        s.connect(helper)
        current = s.snapshot(saved, use_backup_address)
        changed = [o for o in range(BLOCK, FLASH_SIZE, BLOCK) if current[o:o + BLOCK] != saved[o:o + BLOCK]]
        s.log(f"plan: rewrite the boot block and {len(changed)} other 64 KB block(s) that differ from the backup")
        if not confirm(dry_run, assume_yes, s.log, "About to restore the backup."):
            s.restart()
            return
        s.erase_range(0, BLOCK)
        for o in changed:
            s.erase_range(o, o + BLOCK)
            if not blank(saved[o:o + BLOCK]):
                s.write_region(o, saved[o:o + BLOCK], f"block 0x{o:06x}")
        s.check_before_boot(saved)
        s.finish(saved[:BLOCK], "boot block", saved)
    s.guarded(step)
