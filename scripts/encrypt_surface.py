#!/usr/bin/env python3
"""Encrypt / decrypt surface_attacks files with Fernet symmetric encryption.

Usage:
    # Generate a key (store this — you need it at runtime)
    python scripts/encrypt_surface.py --gen-key

    # Encrypt all .txt files (writes .enc, leaves .txt intact)
    python scripts/encrypt_surface.py --dir prompts/surface_attacks

    # Encrypt and delete the plaintext originals
    python scripts/encrypt_surface.py --dir prompts/surface_attacks --delete

    # Decrypt all .enc files back to plaintext (for editing)
    python scripts/encrypt_surface.py --dir prompts/surface_attacks --decrypt

Set the key at runtime via SURFACE_ATTACKS_KEY env var.
"""

import argparse
import os
import sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(
        description="Encrypt / decrypt surface_attacks files with Fernet."
    )
    ap.add_argument(
        "--dir",
        help="Directory containing files (default: prompts/surface_attacks)",
    )
    ap.add_argument(
        "--gen-key",
        action="store_true",
        help="Print a new Fernet key and exit (store it in SURFACE_ATTACKS_KEY)",
    )
    ap.add_argument(
        "--encrypt",
        action="store_true",
        help="Encrypt .txt -> .enc (default action)",
    )
    ap.add_argument(
        "--decrypt",
        action="store_true",
        help="Decrypt .enc -> .txt",
    )
    ap.add_argument(
        "--delete",
        action="store_true",
        help="Delete source files after encrypt/decrypt (keeps only the output)",
    )
    ap.add_argument(
        "--key", default=None, help="Fernet key (or set SURFACE_ATTACKS_KEY env var)"
    )
    args = ap.parse_args()

    if args.gen_key:
        from cryptography.fernet import Fernet

        print(Fernet.generate_key().decode())
        return

    key = args.key or os.environ.get("SURFACE_ATTACKS_KEY", "")
    if not key:
        print("ERROR: No key. Pass --key or set SURFACE_ATTACKS_KEY.", file=sys.stderr)
        sys.exit(1)

    from cryptography.fernet import Fernet

    try:
        fernet = Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as e:
        print(f"ERROR: Invalid Fernet key: {e}", file=sys.stderr)
        sys.exit(1)

    d = Path(args.dir or "prompts/surface_attacks")
    if not d.is_dir():
        print(f"ERROR: {d} is not a directory", file=sys.stderr)
        sys.exit(1)

    # Default to encrypt when neither flag is set
    if not args.encrypt and not args.decrypt:
        args.encrypt = True

    if args.decrypt:
        enc_files = sorted(d.glob("*.enc"))
        if not enc_files:
            print(f"No .enc files found in {d}")
            return
        for enc in enc_files:
            plaintext = fernet.decrypt(enc.read_bytes())
            out = enc.with_suffix("")  # strip .enc
            out.write_bytes(plaintext)
            print(f"  {enc.name} -> {out.name}  ({len(plaintext)}B)")
            if args.delete:
                enc.unlink()
                print(f"  deleted {enc.name}")
        print(f"\nDecrypted {len(enc_files)} file(s).")
    else:
        txt_files = sorted(d.glob("*.txt"))
        if not txt_files:
            print(f"No .txt files found in {d}")
            return
        for txt in txt_files:
            plaintext = txt.read_bytes()
            encrypted = fernet.encrypt(plaintext)
            enc_path = txt.with_suffix(".txt.enc")
            enc_path.write_bytes(encrypted)
            print(f"  {txt.name} -> {enc_path.name}  ({len(plaintext)}B -> {len(encrypted)}B)")
            if args.delete:
                txt.unlink()
                print(f"  deleted {txt.name}")
        print(f"\nEncrypted {len(txt_files)} file(s). Set SURFACE_ATTACKS_KEY at runtime.")


if __name__ == "__main__":
    main()
