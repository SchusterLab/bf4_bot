"""
Convert the Task Scheduler XML files from UTF-8 to UTF-16 LE with BOM.

Windows Task Scheduler refuses to import UTF-8 XMLs -- it errors out with:
    ERROR: The task XML is malformed.
    (1,40)::ERROR: unable to switch the encoding

The encoding it wants is UTF-16 LE with a leading byte-order mark (BOM).
Python's "utf-16" codec writes UTF-16 LE + BOM automatically (in contrast
to "utf-16-le" which is LE without a BOM).

This script is idempotent -- safe to run multiple times.  It reads each
file, detects whether it is already UTF-16, and only re-encodes if needed.

Run:
    python scheduler/convert_xml_utf16.py
"""

import os
import sys

# Paths are resolved relative to the script's own location so it can be
# run from anywhere, not just the project root.
HERE = os.path.dirname(os.path.abspath(__file__))
FILES = [
    os.path.join(HERE, "BF4Bot-Main.xml"),
    os.path.join(HERE, "BF4Bot-Watchdog.xml"),
]

# UTF-16 LE BOM is the two bytes 0xFF 0xFE at the start of the file.
# If we see those bytes, the file is already UTF-16 LE and we can skip it.
UTF16_LE_BOM = b"\xff\xfe"


def convert_one(path):
    """Return a short status string after processing one file."""
    with open(path, "rb") as f:
        raw = f.read(2)
    if raw == UTF16_LE_BOM:
        return f"  [skip]  {path}  (already UTF-16 LE)"

    # Read as UTF-8 text.  If this fails, the file is something unexpected --
    # better to raise than to silently mis-convert.
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    # Update the XML prologue to advertise the new encoding.  Task Scheduler
    # reads the prologue first to decide how to decode the rest of the file,
    # so the declaration MUST match the actual byte encoding.
    text = text.replace('encoding="UTF-8"', 'encoding="UTF-16"', 1)

    # Write as UTF-16 LE + BOM.  Python's "utf-16" codec does both for us.
    with open(path, "w", encoding="utf-16") as f:
        f.write(text)

    return f"  [OK]    {path}  (converted to UTF-16 LE + BOM)"


def main():
    print("Converting Task Scheduler XMLs:")
    for p in FILES:
        if not os.path.exists(p):
            print(f"  [MISS]  {p}  (file not found)")
            continue
        print(convert_one(p))
    print("Done.")


if __name__ == "__main__":
    main()
