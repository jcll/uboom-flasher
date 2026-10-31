"""uboom-flasher: back up, update and restore an EarFun UBoom L over USB (macOS; Linux untested).

  uboom-flasher backup  <firmware.fw>
  uboom-flasher write   <firmware.fw> --backup <backup.bin> [--nvram keep|address|reset] [--dry-run]
  uboom-flasher restore <backup.bin> --fw <firmware.fw> [--dry-run]

The speaker must be in update mode: switch it off, hold the Bluetooth button and plug the USB-C cable
straight into the computer. The helper program the speaker needs is taken from the .fw file you give,
which must be an EarFun UBoom L firmware package. Backups and logs go to ~/uboom-flasher.
"""
import argparse
import os

from . import __version__, operations
from .layout import NVRAM_KEEP

BACKUP_ADDRESS_HELP = ("take the Bluetooth address and paired devices from the backup even though the speaker's "
                       "settings area is erased or different: to finish an update or restore that stopped partway, or "
                       "to restore after --nvram reset. Only with a backup of this same speaker.")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="uboom-flasher", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--data-dir", default=os.path.expanduser("~/uboom-flasher"),
                    help="where backups and logs are kept (default: ~/uboom-flasher)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backup", help="read-only: save a verified copy of the speaker's whole flash")
    b.add_argument("firmware", help="a UBoom L .fw package (only its USB helper program is used)")

    w = sub.add_parser("write", help="install a .fw firmware package")
    w.add_argument("firmware")
    w.add_argument("--backup", required=True, help="a backup of this speaker made with 'uboom-flasher backup'")
    w.add_argument("--nvram", choices=NVRAM_KEEP, default="keep",
                   help="keep (default): Bluetooth address and paired devices; address: only the Bluetooth address "
                        "(pair again); reset: wipe both, as EarFun's tool does (new address; older backups then need "
                        "--use-backup-address to restore)")
    w.add_argument("--allow-unknown-device", action="store_true", help=argparse.SUPPRESS)

    r = sub.add_parser("restore", help="put a backup back onto the speaker")
    r.add_argument("backup")
    r.add_argument("--fw", required=True, help="a UBoom L .fw package (only its USB helper program is used)")

    for p in (w, r):
        p.add_argument("--use-backup-address", action="store_true", help=BACKUP_ADDRESS_HELP)
        p.add_argument("--dry-run", action="store_true", help="connect, check and show the plan; change nothing")
        p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
        p.add_argument("--allow-unknown-package", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.cmd == "backup":
        operations.backup(args.firmware, args.data_dir)
    elif args.cmd == "write":
        operations.write(args.firmware, args.backup, args.data_dir, args.nvram, args.dry_run, args.yes,
                         args.allow_unknown_package, args.use_backup_address, args.allow_unknown_device)
    else:
        operations.restore(args.backup, args.fw, args.data_dir, args.dry_run, args.yes, args.allow_unknown_package,
                           args.use_backup_address)
    return 0
