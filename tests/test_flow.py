import os

import pytest

from uboom_flasher import operations
from uboom_flasher.layout import FLASH_SIZE

from sim import Speaker, install, nvram_record, synthetic_flash, synthetic_package

VENDOR_ERASES = [(0x0, 0x10000), (0x10000, 0x110000), (0x130000, 0x1F0000), (0x1F0000, 0x200000)]
NVRAM_SECTOR = 0x1F0000 // 512        # nvram.bin
USER_NVRAM_SECTOR = 0x1F8000 // 512   # the paired devices in the synthetic flash


@pytest.fixture
def setup(monkeypatch, tmp_path):
    def make(flash=None, package_seed=2, **faults):
        old = synthetic_package(1)
        new = synthetic_package(package_seed)
        new["mp_usb_andes.bin"] = old["mp_usb_andes.bin"]
        new["mbrec.bin"] = old["mbrec.bin"][:4000] + new["mbrec.bin"][4000:]  # versions share most of the boot record
        speaker = Speaker(flash if flash is not None else synthetic_flash(old), **faults)
        fw = install(monkeypatch, speaker, new, tmp_path)
        return speaker, new, fw, str(tmp_path / "data")
    return make


def replug(speaker):
    """The user unplugs the speaker and puts it back in update mode (simulated faults stay)."""
    speaker.stage, speaker.pending, speaker.reset_stored, speaker.restarted = "rom", None, False, False


def take_backup(speaker, fw, data):
    path = operations.backup(fw, data)
    replug(speaker)
    return path


def images_in_place(flash, package):
    return all(flash[off:off + len(package[n])] == package[n]
               for n, off in (("mbrec.bin", 0), ("zephyr.bin", 0x11000), ("sdfs.bin", 0x132000), ("nvram.bin", 0x1F0000)))


def update(fw, backup, data, **kw):
    operations.write(fw, backup, data, assume_yes=True, allow_unknown=True, **kw)


def test_backup_is_exact_and_read_only(setup):
    speaker, _, fw, data = setup()
    before = bytes(speaker.flash)
    path = operations.backup(fw, data)
    assert open(path, "rb").read() == before == bytes(speaker.flash)
    assert speaker.erases == [] and speaker.restarted


def test_update_keeps_bluetooth_address_and_pairings(setup):
    speaker, package, fw, data = setup()
    before = bytes(speaker.flash)
    backup = take_backup(speaker, fw, data)
    update(fw, backup, data, nvram="keep")
    assert speaker.erases == VENDOR_ERASES
    assert images_in_place(speaker.flash, package)
    assert speaker.flash[0x1F2000:] == before[0x1F2000:]
    assert speaker.flash[0x110000:0x130000] == before[0x110000:0x130000]
    assert speaker.restarted


def test_update_with_reset_wipes_nvram_like_the_vendor_tool(setup):
    speaker, package, fw, data = setup()
    backup = take_backup(speaker, fw, data)
    update(fw, backup, data, nvram="reset")
    assert images_in_place(speaker.flash, package)
    assert speaker.flash[0x1F2000:] == b"\xff" * (FLASH_SIZE - 0x1F2000)


def test_dry_run_changes_nothing(setup):
    speaker, _, fw, data = setup()
    before = bytes(speaker.flash)
    backup = take_backup(speaker, fw, data)
    operations.write(fw, backup, data, dry_run=True, allow_unknown=True)
    assert bytes(speaker.flash) == before and speaker.erases == [] and speaker.restarted


def test_failed_verify_stops_with_blank_boot_record(setup):
    speaker, _, fw, data = setup(corrupt_sector=0x88 + 5)
    backup = take_backup(speaker, fw, data)
    with pytest.raises(SystemExit, match="boot record is erased"):
        update(fw, backup, data)
    assert speaker.flash[:0x1000] == b"\xff" * 0x1000  # no boot record: the speaker stays in update mode
    assert not speaker.restarted


def test_backup_from_another_speaker_is_refused(setup):
    speaker, _, fw, data = setup()
    backup = take_backup(speaker, fw, data)
    other = bytearray(open(backup, "rb").read())
    other[0x1F2020] ^= 0x01
    other_path = backup.replace(".bin", "-other.bin")
    open(other_path, "wb").write(other)
    with pytest.raises(SystemExit, match="Bluetooth address"):
        update(fw, other_path, data)
    assert speaker.erases == []


def test_restore_puts_the_backup_back(setup):
    speaker, _, fw, data = setup()
    original = bytes(speaker.flash)
    backup = take_backup(speaker, fw, data)
    update(fw, backup, data)
    replug(speaker)
    operations.restore(backup, fw, data, assume_yes=True, allow_unknown=True)
    assert bytes(speaker.flash) == original


def test_unknown_package_is_refused_without_override(setup):
    speaker, _, fw, data = setup()
    backup = take_backup(speaker, fw, data)
    with pytest.raises(SystemExit, match="Unknown .fw package"):
        operations.write(fw, backup, data, assume_yes=True)
    assert speaker.erases == []


# --- recovery after an interrupted update (review findings 1, 3, 11, 12) ---

def test_update_stopped_in_the_nvram_step_can_be_finished(setup):
    speaker, package, fw, data = setup(corrupt_sector=NVRAM_SECTOR)
    before = bytes(speaker.flash)
    backup = take_backup(speaker, fw, data)
    with pytest.raises(SystemExit):
        update(fw, backup, data)
    assert speaker.flash[0x1F2000:0x1F4000] == b"\xff" * 0x2000  # address area erased by the stopped run
    replug(speaker)
    erases = len(speaker.erases)
    with pytest.raises(SystemExit, match="--use-backup-address"):
        update(fw, backup, data)
    assert len(speaker.erases) == erases
    replug(speaker)
    speaker.corrupt_sector = None
    update(fw, backup, data, use_backup_address=True)
    assert images_in_place(speaker.flash, package)
    assert speaker.flash[0x1F2000:] == before[0x1F2000:] and speaker.restarted


def test_pre_update_copy_from_the_stopped_run_finishes_it(setup):
    speaker, package, fw, data = setup(corrupt_sector=NVRAM_SECTOR)
    before = bytes(speaker.flash)
    backup = take_backup(speaker, fw, data)
    with pytest.raises(SystemExit):
        update(fw, backup, data)
    pre = [f for f in os.listdir(os.path.join(data, "backups")) if f.endswith("-pre-update.bin")]
    assert len(pre) == 1
    replug(speaker)
    speaker.corrupt_sector = None
    update(fw, os.path.join(data, "backups", pre[0]), data, use_backup_address=True)
    assert images_in_place(speaker.flash, package) and speaker.flash[0x1F2000:] == before[0x1F2000:]


def test_backup_from_an_unfinished_run_is_not_used_for_settings(setup):
    speaker, _, fw, data = setup(corrupt_sector=NVRAM_SECTOR)
    backup = take_backup(speaker, fw, data)
    with pytest.raises(SystemExit):
        update(fw, backup, data)
    replug(speaker)
    unfinished = take_backup(speaker, fw, data)  # boot record and settings area both blank
    erases = len(speaker.erases)
    with pytest.raises(SystemExit, match="saved while an update was unfinished"):
        update(fw, unfinished, data, use_backup_address=True)
    with pytest.raises(SystemExit, match="saved while an update was unfinished"):
        operations.restore(unfinished, fw, data, assume_yes=True, allow_unknown=True)
    assert len(speaker.erases) == erases


def test_restore_after_a_stopped_update(setup):
    speaker, _, fw, data = setup(corrupt_sector=NVRAM_SECTOR)
    original = bytes(speaker.flash)
    backup = take_backup(speaker, fw, data)
    with pytest.raises(SystemExit):
        update(fw, backup, data)
    replug(speaker)
    with pytest.raises(SystemExit, match="--use-backup-address"):
        operations.restore(backup, fw, data, assume_yes=True, allow_unknown=True)
    replug(speaker)
    speaker.corrupt_sector = None
    operations.restore(backup, fw, data, assume_yes=True, allow_unknown=True, use_backup_address=True)
    assert bytes(speaker.flash) == original


def test_partial_settings_write_is_not_copied_forward(setup):
    speaker, _, fw, data = setup(pull_at_sector=USER_NVRAM_SECTOR)
    before = bytes(speaker.flash)
    backup = take_backup(speaker, fw, data)
    with pytest.raises(SystemExit, match="boot record is erased"):
        update(fw, backup, data)
    assert speaker.flash[0x1F8000:0x1F8010] == b"\xff" * 16  # pairings not written yet
    replug(speaker)
    with pytest.raises(SystemExit, match="may be incomplete"):
        update(fw, backup, data)
    replug(speaker)
    update(fw, backup, data, use_backup_address=True)
    assert speaker.flash[0x1F2000:] == before[0x1F2000:]


def test_backup_can_be_restored_after_nvram_reset_with_the_flag(setup):
    speaker, _, fw, data = setup()
    original = bytes(speaker.flash)
    backup = take_backup(speaker, fw, data)
    update(fw, backup, data, nvram="reset")
    # on its first start the speaker makes up a new (random) Bluetooth address
    new = b"NVSR\xff\x0a\x01\x00\x01\x10\x00\x10\x00\x00\x00\x00" + nvram_record("BT_MAC", b"C2AABBCCDDEE")
    speaker.flash[0x1F2000:0x1F2000 + len(new)] = new
    replug(speaker)
    with pytest.raises(SystemExit, match="after --nvram reset"):
        operations.restore(backup, fw, data, assume_yes=True, allow_unknown=True)
    replug(speaker)
    operations.restore(backup, fw, data, assume_yes=True, allow_unknown=True, use_backup_address=True)
    assert bytes(speaker.flash) == original


# --- other review findings ---

def test_read_back_mismatch_happens_before_the_boot_record_is_written(setup):
    speaker, _, fw, data = setup(stuck_after_erase={0x10F000: 0x00})  # erase leaves a byte in a never-written tail
    backup = take_backup(speaker, fw, data)
    with pytest.raises(SystemExit, match="READ-BACK MISMATCH"):
        update(fw, backup, data)
    assert speaker.flash[:0x1000] == b"\xff" * 0x1000 and not speaker.restarted


def test_device_that_does_not_look_like_a_uboom_is_refused(setup):
    package = synthetic_package(1)
    flash = synthetic_flash(package)
    flash[:0x1000] = synthetic_package(7)["mbrec.bin"]  # a different product's boot record
    speaker, _, fw, data = setup(flash=flash)
    backup = take_backup(speaker, fw, data)
    with pytest.raises(SystemExit, match="does not look like an EarFun UBoom L"):
        update(fw, backup, data)
    assert speaker.erases == []
    replug(speaker)
    update(fw, backup, data, allow_unknown_device=True)
    assert speaker.restarted


def test_restore_needs_the_checksum_file(setup):
    speaker, _, fw, data = setup()
    backup = take_backup(speaker, fw, data)
    os.remove(backup[:-4] + ".sha256")
    with pytest.raises(SystemExit, match="no checksum file"):
        operations.restore(backup, fw, data, assume_yes=True, allow_unknown=True)
    assert speaker.erases == []


def test_missing_package_gives_a_plain_message(setup):
    speaker, _, fw, data = setup()
    backup = take_backup(speaker, fw, data)
    with pytest.raises(SystemExit) as e:
        update(fw + ".missing", backup, data)
    assert "Cannot read the firmware package" in str(e.value) and "boot record" not in str(e.value)


def test_refusals_are_written_to_the_log(setup):
    speaker, _, fw, data = setup()
    backup = take_backup(speaker, fw, data)
    os.remove(backup[:-4] + ".sha256")
    with pytest.raises(SystemExit):
        operations.restore(backup, fw, data, assume_yes=True, allow_unknown=True)
    logs = sorted(os.listdir(os.path.join(data, "logs")))
    assert "no checksum file" in open(os.path.join(data, "logs", logs[-1])).read()


def test_usb_permission_error_points_to_the_udev_rule(setup, monkeypatch):
    speaker, _, fw, data = setup()
    backup = take_backup(speaker, fw, data)

    def denied(log):
        raise operations.adfu.usb.core.USBError("Access denied (insufficient permissions)", errno=13)
    monkeypatch.setattr(operations.adfu, "open_device", denied)
    with pytest.raises(SystemExit, match="udev rule") as e:
        update(fw, backup, data)
    assert "Nothing was erased" in str(e.value)


def test_no_speaker_is_logged(setup, monkeypatch):
    speaker, _, fw, data = setup()
    backup = take_backup(speaker, fw, data)

    def absent(log):
        raise SystemExit("No speaker in update mode (USB 10d6:10d6).")
    monkeypatch.setattr(operations.adfu, "open_device", absent)
    with pytest.raises(SystemExit):
        update(fw, backup, data)
    logs = sorted(os.listdir(os.path.join(data, "logs")))
    assert "No speaker in update mode" in open(os.path.join(data, "logs", logs[-1])).read()


def test_backups_are_never_overwritten(setup):
    speaker, _, fw, data = setup()
    first = take_backup(speaker, fw, data)
    second = take_backup(speaker, fw, data)  # same second, same name
    assert first != second and os.path.exists(first) and os.path.exists(second[:-4] + ".sha256")
