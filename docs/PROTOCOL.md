# EarFun UBoom L USB update protocol

This is the update sequence of EarFun's Windows tool (Actions "Media Product Tool" 2.10.07). It was
recorded by running the tool against a stand-in USB layer, then confirmed on a real speaker. The UBoom L
uses an Actions ATS2833 chip with a 2 MB GigaDevice GD25Q16 SPI NOR flash.

## USB

- Update mode: speaker off, hold the Bluetooth button, connect USB-C. VID:PID `10d6:10d6`, USB 2.0 high
  speed, one vendor interface (class 0xFF), bulk OUT `0x02`, bulk IN `0x81`, 512-byte packets.
- Framing in both stages: 31-byte command block wrapper (`USBC`, tag, data length, flags 0x80 for
  device-to-host, LUN 0, CB length 16, 16-byte CB), optional data phase, 13-byte status (`USBS`, status
  byte 0 = OK, 2 = error).
- On macOS, reads must request whole 512-byte packets (libusb reports an overflow otherwise).

## Stage 1: boot ROM

| CB | Meaning |
|---|---|
| `CC` …, reply length at CB[7..8] | identify; replies `00 "CADFUD" 16 "QA" 00` |
| `CD 13`, length CB[5..8], address CB[9..12] | write RAM (data from host to device) |
| `CD 93`, same fields | read RAM |
| `CD 20`, address CB[9..12] | run code at address |

The firmware package's `mp_usb_andes.bin` (9536 bytes) is written to RAM at `0x69000`, read back, and
started there.

## Stage 2: the helper program

The helper accepts CB[0] = `CD` (commands below) and `B0` (detach), and ignores `CC`. CB[1] = subcode, CB[2] =
storage index (0), CB[5..8] = length, CB[9..12] = address. Flash lengths and addresses are in 512-byte
sectors. A subcode with bit 7 set moves data from the device to the host.

| Subcode | Data | Meaning |
|---|---|---|
| `16` | none | initialize the flash |
| `15` | 512 bytes in | storage info; flash JEDEC ID at bytes 4..7 |
| `14` | 512 bytes out | storage info back to the helper (EarFun's tool sends the `15` block unchanged) |
| `17` | none | erase `length` sectors at `address` |
| `10` | `length`×512 out | write sectors; address `0xFF000000` and up: data is read and discarded |
| `90` | `length`×512 in | read sectors |
| `30` | 12 bytes out | verify: byte address, byte length, CRC32 (zlib); status 2 on mismatch |
| `13` / `93` | as in the ROM | write / read RAM |
| `18` | none | store restart mode (CB[2]: 0 = restart) and delay in seconds (CB[3]) |
| `B0` (CB[0]) | none | detach: acknowledge, then act on the stored restart mode |

## Flash layout

From the partition table in the boot record (magic `ACPT`, 24-byte entries):

| Partition | Offset | Size | Contents |
|---|---|---|---|
| boot | `0x000000` | 4 KB | `mbrec.bin` |
| system | `0x011000` | 1.125 MB | `zephyr.bin` |
| sdfs | `0x132000` | 760 KB | `sdfs.bin` (sounds, prompts, audio effects) |
| nvram factory | `0x1F0000` | 8 KB | `nvram.bin` (defaults) |
| nvram factory-rw | `0x1F2000` | 8 KB | written by the speaker: `BT_MAC` record |
| nvram user | `0x1F4000` | 48 KB | written by the speaker: paired devices, volume, EQ |

NVRAM regions start with `NVSR`; records are `49 ff <crc16> 00 <name length> <data length, u16> <name\0>
<data>`.

## The update sequence (EarFun's tool)

1. Stage 1: load and start the helper.
2. `16`, `15`, then `14` with the same block.
3. Erase `0x000000-0x010000`, so the boot record is gone first.
4. For `zephyr.bin`, `sdfs.bin`, `nvram.bin`: erase the file's extent rounded out to 64 KB blocks
   (`0x010000-0x110000`, `0x130000-0x1F0000`, `0x1F0000-0x200000` for 1.2.7), write it padded with `0xFF` to
   whole sectors, verify with `30`.
5. Write `mbrec.bin` at `0x000000`, verify.
6. Write one sector to `0xFF000000` ("transfer over"), then `18` (mode 0, 2 s) and `B0`.

Step 4 erases the whole NVRAM block, including the Bluetooth address record and the paired devices.

## What uboom-flasher does differently

- It reads the whole flash before erasing and saves it (`-pre-update.bin`).
- After `nvram.bin` it writes back what the speaker had after the factory template: `0x1F2000-0x200000`
  with `--nvram keep` (the default), only `0x1F2000-0x1F4000` (the Bluetooth address) with
  `--nvram address`, and nothing with `--nvram reset`, which matches EarFun's tool.
- Step 5 moves after a full read-back. While the boot record is still erased, the tool reads the whole
  flash and compares it with the expected image; only then does it write `mbrec.bin`, verify it with
  `30` and read the boot block back. If anything fails before that, the speaker has no boot record and
  the update can be run again.
- It writes the images exactly as they come in the package.
