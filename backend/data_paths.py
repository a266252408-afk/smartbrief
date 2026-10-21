"""运行数据路径契约；只读预置资源始终来自代码目录。"""
import os
from pathlib import Path, PureWindowsPath

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent
load_dotenv(BACKEND_DIR / '.env')


def data_root() -> Path:
    """默认兼容旧本地布局，显式DATA_DIR必须是绝对路径。"""
    value = os.getenv('DATA_DIR')
    if value is None:
        return BACKEND_DIR
    path = Path(value)
    if not value.strip() or not path.is_absolute():
        raise ValueError('DATA_DIR必须是非空绝对路径')
    return path.resolve()


def runtime_path(relative: str) -> Path:
    """返回运行根下的路径，并建立其父目录。"""
    component = Path(relative)
    if component.is_absolute() or PureWindowsPath(relative).drive or '..' in component.parts:
        raise ValueError('运行路径必须位于DATA_DIR内')
    root = data_root()
    path = root / component
    if not path.resolve().is_relative_to(root):
        raise ValueError('运行路径不能通过链接越界')
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
