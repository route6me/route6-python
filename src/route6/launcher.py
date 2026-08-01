"""Fetch, verify and cache the r6me binary.

This package implements no protocol. It is a launcher: it makes sure the
arch-matched r6me binary is present and authentic, then hands over to it.

The fetch is LAZY — on first use, not at install time. pip wheels have no
reliable post-install hook, and npm's equivalent is skipped whenever anyone
installs with --ignore-scripts (routine in CI). Doing it on first run is the one
approach that behaves identically everywhere.
"""

from __future__ import annotations

import hashlib
import os
import platform
import stat
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request

BASE_URL = os.environ.get("R6ME_BASE_URL", "https://dl.route6.me").rstrip("/")
MIRROR_URL = "https://github.com/route6me/r6me-releases/releases/download"
TIMEOUT = 60

# Kept in step with dist-installer/install.sh — same names, same mapping.
_ARCH = {
    "x86_64": "amd64", "amd64": "amd64",
    "aarch64": "arm64", "arm64": "arm64",
    "armv7l": "armv7", "armv6l": "armv6",
    "riscv64": "riscv64",
    "mips": "mips_softfloat", "mipsel": "mipsle_softfloat",
}


class LauncherError(RuntimeError):
    pass


def state_dir() -> str:
    d = os.environ.get("R6ME_STATE_DIR")
    if d:
        return d
    return os.path.join(os.path.expanduser("~"), ".r6me")


def bin_dir() -> str:
    return os.path.join(state_dir(), "bin")


def platform_tuple() -> tuple[str, str]:
    sysname = platform.system()
    if sysname == "Linux":
        os_name = "linux"
    elif sysname == "Darwin":
        os_name = "darwin"
    elif sysname == "Windows":
        os_name = "windows"
    else:
        raise LauncherError(f"unsupported operating system: {sysname}")

    machine = platform.machine().lower()
    arch = _ARCH.get(machine)
    if not arch:
        raise LauncherError(
            f"unsupported architecture: {machine}. "
            f"Published builds are listed at {BASE_URL}/"
        )
    if os_name in ("darwin", "windows") and arch not in ("amd64", "arm64"):
        raise LauncherError(f"unsupported architecture for {os_name}: {machine}")
    return os_name, arch


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "route6-launcher"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def resolve_version() -> str:
    pinned = os.environ.get("R6ME_VERSION", "").strip()
    if pinned:
        return pinned if pinned.startswith("v") else "v" + pinned
    try:
        v = _get(f"{BASE_URL}/stable").decode().strip()
    except Exception as e:  # noqa: BLE001 - any failure here is the same failure
        raise LauncherError(
            f"could not resolve the current version from {BASE_URL}/stable: {e}"
        ) from e
    if not v:
        raise LauncherError(f"{BASE_URL}/stable was empty")
    return v if v.startswith("v") else "v" + v


def cached_binary() -> str | None:
    """Newest already-downloaded binary, or None.

    Checked BEFORE any network call so ordinary invocations cost nothing and
    work offline. An upgrade is therefore explicit (`route6 upgrade`), which is
    the right default for something holding a network identity: it does not
    change under you between runs.
    """
    pinned = os.environ.get("R6ME_VERSION", "").strip()
    if pinned:
        p = os.path.join(bin_dir(), f"r6me-{pinned if pinned.startswith('v') else 'v' + pinned}")
        return p if os.path.exists(p) else None
    try:
        names = [n for n in os.listdir(bin_dir()) if n.startswith("r6me-")]
    except OSError:
        return None
    if not names:
        return None

    def key(n: str):
        try:
            return tuple(int(x) for x in n[len("r6me-v"):].split("."))
        except Exception:  # noqa: BLE001
            return (0,)

    return os.path.join(bin_dir(), sorted(names, key=key)[-1])


def download(version: str) -> str:
    os_name, arch = platform_tuple()
    asset = f"r6me_{version.lstrip('v')}_{os_name}_{arch}.tar.gz"
    dest = os.path.join(bin_dir(), f"r6me-{version}")

    sources = [(f"{BASE_URL}/{version}/{asset}", f"{BASE_URL}/{version}/checksums.txt")]
    sources.append((f"{MIRROR_URL}/{version}/{asset}", f"{MIRROR_URL}/{version}/checksums.txt"))

    last: Exception | None = None
    for asset_url, sums_url in sources:
        try:
            blob = _get(asset_url)
            sums = _get(sums_url).decode()
        except Exception as e:  # noqa: BLE001
            last = e
            continue

        # Fail closed on a missing manifest line exactly as on a mismatch: both
        # mean we cannot say what we just downloaded.
        want = None
        for line in sums.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == asset:
                want = parts[0]
                break
        if want is None:
            raise LauncherError(
                f"checksum for {asset} is not listed in checksums.txt — refusing to install"
            )
        got = hashlib.sha256(blob).hexdigest()
        if got != want:
            raise LauncherError(
                "checksum mismatch for "
                f"{asset}\n  expected: {want}\n  actual:   {got}\n"
                "refusing to install — the download does not match the published checksum"
            )

        os.makedirs(bin_dir(), exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            tar_path = os.path.join(tmp, asset)
            with open(tar_path, "wb") as fh:
                fh.write(blob)
            with tarfile.open(tar_path) as tf:
                member = next((m for m in tf.getmembers() if os.path.basename(m.name) == "r6me"), None)
                if member is None:
                    raise LauncherError("archive did not contain an r6me binary")
                extracted = tf.extractfile(member)
                if extracted is None:
                    raise LauncherError("could not read r6me from the archive")
                staged = dest + ".new"
                with open(staged, "wb") as out:
                    out.write(extracted.read())
        os.chmod(staged, os.stat(staged).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        # Move into place so an upgrade never truncates a running binary.
        os.replace(staged, dest)
        return dest

    raise LauncherError(f"could not download r6me {version}: {last}")


def ensure_binary(force_refresh: bool = False) -> str:
    if not force_refresh:
        cached = cached_binary()
        if cached:
            return cached
    version = resolve_version()
    existing = os.path.join(bin_dir(), f"r6me-{version}")
    if os.path.exists(existing) and not force_refresh:
        return existing
    sys.stderr.write(f"route6: fetching r6me {version}...\n")
    path = download(version)
    sys.stderr.write(f"route6: verified and installed {path}\n")
    return path
