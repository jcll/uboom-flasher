# uboom-flasher

Back up, update and restore the firmware of an EarFun UBoom L Bluetooth speaker over USB from a Mac,
without Windows or a Windows virtual machine.

The tool sends the speaker the same commands as EarFun's Windows tool (Actions "Media Product Tool"), in
the same order; the sequence was recorded from that tool. It also adds safety checks the official tool
lacks.

> Not affiliated with EarFun or Actions Semiconductor. Tested on one speaker from macOS on Apple silicon.
> Linux should work but has not been tried. Please read [Safety](#safety) first. If an update fails
> partway, the boot record has not been written yet, so you can put the speaker back in update mode and
> run the update again.

## What it does

| Command | What happens |
|---|---|
| `uboom-flasher backup` | Read-only. Saves the speaker's whole 2 MB flash; it is read twice and saved only if both reads agree. |
| `uboom-flasher write` | Installs a firmware package (`.fw`). |
| `uboom-flasher restore` | Puts a backup back onto the speaker. |

## Safety

`write` will not start without a backup of the same speaker. Before erasing anything it compares the
backup's Bluetooth address area with the speaker's, checks that the speaker's boot record looks like a
UBoom L one, and saves the current flash once more (the `-pre-update.bin` file).

EarFun's tool erases the speaker's settings area, so afterwards the speaker makes up a new Bluetooth
address and every phone has to pair again. This tool writes that area back after the update
(`--nvram keep`, the default). `--nvram address` keeps only the address, and `--nvram reset` does what
EarFun's tool does.

The tool accepts only the firmware packages listed below, which it identifies by SHA-256.

The tool erases the boot record first and writes it last. It checks each image with the speaker's own
CRC32 check, and before writing the boot record it reads the whole flash back and compares it. If a step
fails before that, the speaker has no boot record, so it cannot start the half-written firmware and you
can run the same command again or use `restore`.

Nothing in this repository comes from EarFun. You supply the firmware package, and the small USB helper
program the speaker needs during an update comes from that package.

Keep backups private: they contain your speaker's Bluetooth address and the pairing keys of your devices.
Keep the `.sha256` file saved next to each backup, because `restore` checks it.

## Install

Python 3.9 or newer, then:

```sh
pipx install git+https://github.com/jcll/uboom-flasher@v0.1.0
# or: uv tool install git+https://github.com/jcll/uboom-flasher@v0.1.0
```

libusb comes with it (the `libusb-package` wheel), so you don't need Homebrew.

On Linux, a normal user can only open the speaker with a udev rule:

```sh
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="10d6", ATTR{idProduct}=="10d6", MODE="0660", TAG+="uaccess"' \
  | sudo tee /etc/udev/rules.d/70-uboom-flasher.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

## Firmware packages

This tool does not include firmware. It recognizes these packages (check yours with `shasum -a 256`):

| Version | File name | SHA-256 |
|---|---|---|
| 1.2.7 (July 2024, EarFun Audio app support) | `CGBT2205_ats2833_evb_SP300_240729_V1.2.7.fw` | `d1bb6e678d40860dfc0ce4fd949a333a25c091c1c65a8127d1c22e4025c277a6` |
| 0.4.3 (earlier build with Oluv's sound tuning) | `v0.4.3.fw` | `a3b0d6c662116bfd35f7d580de189e8675a4fcf11d516fc7856447dfd4498b40` |

## Update mode

1. Switch the speaker off.
2. Hold the Bluetooth button and plug a USB-C data cable straight into the computer. Through a hub, dock
   or monitor port the speaker may not show up at all.
3. The speaker appears as USB device `10d6:10d6`. Run one of the commands below.

After each command the speaker restarts on its own, so enter update mode again before the next one.

## Usage

```sh
# 1. back up (read-only)
uboom-flasher backup CGBT2205_ats2833_evb_SP300_240729_V1.2.7.fw
#    -> ~/uboom-flasher/backups/uboom-l-<chip>-<date>.bin

# 2. see the plan without changing anything
uboom-flasher write CGBT2205_ats2833_evb_SP300_240729_V1.2.7.fw --backup ~/uboom-flasher/backups/uboom-l-<...>.bin --dry-run

# 3. install
uboom-flasher write CGBT2205_ats2833_evb_SP300_240729_V1.2.7.fw --backup ~/uboom-flasher/backups/uboom-l-<...>.bin

# put a backup back (any UBoom L package works for --fw; only its helper program is used)
uboom-flasher restore ~/uboom-flasher/backups/uboom-l-<...>.bin --fw CGBT2205_ats2833_evb_SP300_240729_V1.2.7.fw
```

Every run writes a log to `~/uboom-flasher/logs/`. If you report a problem, please attach it.

## If a run stops partway

When a command fails or the cable comes out, the tool says how far it got. Unplug the speaker, put it
back in update mode and run the same command again.

If the run stopped while the tool was rewriting the settings area, the next run says the settings area
is erased or may be incomplete. Run the same command again with `--use-backup-address`, and the Bluetooth
address and paired devices then come from the backup you give. Use the newest backup of this speaker.
After a stopped run that is usually the `-pre-update.bin` file it saved in `~/uboom-flasher/backups/`
when it started:

```sh
uboom-flasher write CGBT2205_ats2833_evb_SP300_240729_V1.2.7.fw --backup ~/uboom-flasher/backups/uboom-l-<...>-pre-update.bin --use-backup-address
```

The same flag lets you restore an older backup after `--nvram reset`, since the speaker has a new
Bluetooth address by then. Only use it with a backup of this same speaker.

A backup saved while an update was unfinished has no boot record. The tool will not restore it or take
settings from it.

## How it works

The speaker's boot ROM accepts a small helper program over USB, and EarFun's firmware package contains
that program. The tool loads it into the speaker's RAM, checks it and starts it, then erases, writes and
verifies the flash in the same order as EarFun's tool. The one change to that order is that the boot
record is written only after the whole flash has been read back and compared. The firmware images are
written exactly as they come in the package. [docs/PROTOCOL.md](docs/PROTOCOL.md) describes the protocol
and the flash layout.

To run the tests, which drive every command against a simulated speaker: `pip install -e '.[test]' && pytest`.

## Credits

- [actions-adfu](https://github.com/sonigeez/actions-adfu) by sonigeez (MIT): the boot-ROM stage and the
  `.fw` reader in `src/uboom_flasher/romstage.py` are copied from it.
- Oluv, for the UBoom L tuning and for keeping its firmware versions available to the community.

## License

MIT (see [LICENSE](LICENSE)); `romstage.py` keeps actions-adfu's MIT notice.
