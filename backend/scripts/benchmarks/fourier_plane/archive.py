"""Access the verified original Cambridge Apollo deposit without extracting unrelated captures."""

import hashlib
import io
import subprocess
import tempfile
import zipfile
from pathlib import Path

import numpy as np

CACHE = Path(tempfile.gettempdir()) / "beamweft-fourier-bench"
URL = "https://api.repository.cam.ac.uk/server/api/core/bitstreams/4cd2b7f9-d9fe-49cc-86a7-f0d1e6dc1f3d/content"
ARCHIVE_MD5 = "4f57f018d813f934f774411ab9b243b8"
ARCHIVE_SHA256 = "670394caaefc68e9a2f133a55f9788bc680e3e3310e4fd063a6c7346153eb7cd"
ARCHIVE_SIZE = 377218383
IMAGE = "data/image_correction/f6cm_exact/"
WAVE = "data/wavefront_measurement/f6cm_exact/"


def digest(path, algorithm="sha256"):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def verified_archive(cache, offline):
    path = cache / "database.zip"
    cache.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        if offline:
            raise FileNotFoundError(f"Offline archive is missing: {path}")
        with tempfile.TemporaryDirectory(dir=cache) as folder:
            tmp = Path(folder) / "download.zip"
            transfer = subprocess.run(["curl", "-fsSL", "--max-time", "180", URL, "-o", str(tmp)])
            if transfer.returncode or tmp.stat().st_size != ARCHIVE_SIZE:
                with tmp.open("wb") as destination:
                    for start in range(0, ARCHIVE_SIZE, 8 * 1024**2):
                        end = min(start + 8 * 1024**2, ARCHIVE_SIZE) - 1
                        data = subprocess.check_output(
                            [
                                "curl",
                                "-fsSL",
                                "--retry",
                                "2",
                                "--max-time",
                                "60",
                                "-r",
                                f"{start}-{end}",
                                f"{URL}?start={start}",
                            ]
                        )
                        if len(data) != end - start + 1:
                            raise ValueError("The repository did not return the requested byte range.")
                        destination.write(data)
            if digest(tmp) != ARCHIVE_SHA256:
                raise ValueError("Downloaded archive SHA-256 does not match the locked copy.")
            tmp.replace(path)
    if digest(path) != ARCHIVE_SHA256 or digest(path, "md5") != ARCHIVE_MD5:
        raise ValueError("Local archive differs from the verified Apollo deposit.")
    return zipfile.ZipFile(path)


def array(archive, member):
    return np.load(io.BytesIO(archive.read(member)), allow_pickle=False)
