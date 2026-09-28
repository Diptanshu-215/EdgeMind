"""Find or download a Qdrant Server binary, so the demo runs a REAL server with no Docker."""
import platform
import shutil
import stat
import tarfile
import zipfile

import httpx

from . import config

BIN_DIR = config.DATA_DIR / "bin"


def _asset():
    sysname, arch = platform.system(), platform.machine().lower()
    if sysname == "Windows":
        return "qdrant-x86_64-pc-windows-msvc.zip", "qdrant.exe"
    if sysname == "Darwin":
        return ("qdrant-aarch64-apple-darwin.tar.gz" if arch in ("arm64", "aarch64")
                else "qdrant-x86_64-apple-darwin.tar.gz"), "qdrant"
    if sysname == "Linux":
        return ("qdrant-aarch64-unknown-linux-musl.tar.gz" if arch in ("arm64", "aarch64")
                else "qdrant-x86_64-unknown-linux-gnu.tar.gz"), "qdrant"
    return None, None


def ensure_binary(log=print):
    asset, exe = _asset()
    if not asset:
        return None
    target = BIN_DIR / exe
    if target.exists():
        return target
    on_path = shutil.which("qdrant")
    if on_path:
        return on_path
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/qdrant/qdrant/releases/download/{config.QDRANT_VERSION}/{asset}"
    archive = BIN_DIR / asset
    log(f"Downloading Qdrant Server {config.QDRANT_VERSION} (~30 MB, one time)...")
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=120) as r:
            r.raise_for_status()
            with open(archive, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
        if asset.endswith(".zip"):
            with zipfile.ZipFile(archive) as z:
                z.extractall(BIN_DIR)
        else:
            with tarfile.open(archive) as t:
                t.extractall(BIN_DIR)
        archive.unlink(missing_ok=True)
        found = next(BIN_DIR.rglob(exe), None)
        if found is None:
            raise FileNotFoundError(exe)
        if found != target:
            shutil.move(str(found), target)
        target.chmod(target.stat().st_mode | stat.S_IEXEC)
        log("Qdrant Server ready.")
        return target
    except Exception as exc:
        log(f"Could not download Qdrant Server ({exc}). Falling back to embedded mode (point sync).")
        archive.unlink(missing_ok=True)
        return None
