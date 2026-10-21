"""业务库实例文件锁；保证同一数据目录只有一个Uvicorn worker。"""
import os
from pathlib import Path


class InstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self.file = None

    def acquire(self):
        handle = self.path.open("a+b")
        try:
            if handle.seek(0, 2) == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RuntimeError("同一业务库仅允许单worker，请使用--workers 1") from None
        self.file = handle

    def release(self):
        if self.file is not None:
            # 关闭句柄释放锁；不能删除锁文件，避免另一个进程绕过旧inode。
            self.file.close()
            self.file = None
