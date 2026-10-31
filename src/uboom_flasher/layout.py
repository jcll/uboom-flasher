"""Flash layout of the EarFun UBoom L (2 MB GigaDevice SPI NOR) and the firmware packages this tool knows.

Partitions, from the partition table in the speaker's boot record:
  boot 0x000000 (4 KB) · system 0x011000 · sdfs 0x132000 · nvram factory 0x1F0000 (8 KB) ·
  nvram factory-rw 0x1F2000 (8 KB, holds the Bluetooth address) · nvram user 0x1F4000 (48 KB, pairings)
"""

FLASH_SIZE = 0x200000
BLOCK = 0x10000  # erase granularity EarFun's tool uses

# file in the .fw package, flash offset, first byte past what its partition may use
IMAGES = [("zephyr.bin", 0x11000, 0x110000), ("sdfs.bin", 0x132000, 0x1F0000), ("nvram.bin", 0x1F0000, 0x1F2000)]
BOOT = ("mbrec.bin", 0x0, 0x1000)

ADDRESS_AREA = (0x1F2000, 0x1F4000)  # NVRAM factory-rw: the BT_MAC record
NVRAM_KEEP = {
    "keep": (0x1F2000, 0x200000),     # Bluetooth address + paired devices and settings
    "address": (0x1F2000, 0x1F4000),  # Bluetooth address only
    "reset": None,                    # wipe both, as EarFun's tool does
}

# sha256 of firmware packages checked against this tool; others need --allow-unknown-package
KNOWN_PACKAGES = {
    "a3b0d6c662116bfd35f7d580de189e8675a4fcf11d516fc7856447dfd4498b40": "0.4.3",
    "d1bb6e678d40860dfc0ce4fd949a333a25c091c1c65a8127d1c22e4025c277a6": "1.2.7",
}
KNOWN_HELPER = "7bedab97f1f5fee283fadf8896ed5a5c834118da5428f2d945988c436334df1f"  # mp_usb_andes.bin
