import errno
import logging
import os
import re
import secrets
import shutil
from pathlib import Path

BLOB_ID = re.compile(r"[0-9a-f]{32}")
BLOB_ID_BYTES = 16
FOLDER_MODE = 0o700

log = logging.getLogger("drive.blobs")


class BlobStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=FOLDER_MODE)
        self._links_work: bool | None = None

    @staticmethod
    def new_id() -> str:
        return secrets.token_hex(BLOB_ID_BYTES)

    def path(self, blob_id: str) -> Path:
        if not isinstance(blob_id, str) or not BLOB_ID.fullmatch(blob_id):
            raise ValueError(f"not a blob id: {blob_id!r}")
        return self.root / blob_id[:2] / blob_id

    def adopt(self, source: Path) -> str:
        blob_id = self.new_id()
        target = self.path(blob_id)
        target.parent.mkdir(exist_ok=True, mode=FOLDER_MODE)
        os.replace(source, target)
        return blob_id

    def duplicate(self, blob_id: str) -> str:
        copy_id = self.new_id()
        target = self.path(copy_id)
        target.parent.mkdir(exist_ok=True, mode=FOLDER_MODE)
        try:
            os.link(self.path(blob_id), target)
        except OSError as error:
            if error.errno not in (errno.EPERM, errno.EMLINK, errno.EXDEV, errno.ENOTSUP):
                raise
            shutil.copyfile(self.path(blob_id), target)
        return copy_id

    def copies_take_space(self) -> bool:
        if self._links_work is None:
            self._links_work = self._probe_links()
        return not self._links_work

    def _probe_links(self) -> bool:
        probe = self.root / f".link-probe-{secrets.token_hex(4)}"
        twin = probe.with_name(probe.name + "-twin")
        try:
            probe.touch()
            os.link(probe, twin)
            return True
        except OSError:
            return False
        finally:
            probe.unlink(missing_ok=True)
            twin.unlink(missing_ok=True)

    def remove(self, blob_id: str) -> None:
        try:
            self.path(blob_id).unlink()
        except FileNotFoundError:
            log.warning("blob %s was already gone when it was deleted", blob_id)
