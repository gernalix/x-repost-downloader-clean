#!/usr/bin/python3 -I
"""Start the installed application without importing code from the user's home."""
import site
import sys

sys.dont_write_bytecode = True
# Fedora's -I also hides /usr/local. Add only administrator-owned global
# package directories, without processing user paths or .pth files.
sys.path.extend(site.getsitepackages(["/usr/local"]))
sys.path.insert(0, "/usr/local/libexec/x-repost-downloader")

from x_repost_downloader.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
