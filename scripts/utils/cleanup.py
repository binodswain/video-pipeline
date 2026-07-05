"""
Resource guards and wind-down protocol for the video pipeline.

Provides:
- ResourceGuard: context manager that enforces CPU/RAM limits
- TempDirManager: unique temp directory with guaranteed cleanup
- LockFile: single-job-at-a-time enforcement
- wind_down(): full cleanup + idle-state verification
"""

import atexit
import fcntl
import gc
import os
import shutil
import signal
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Optional


class LockFile:
    """Ensure only one video build at a time via advisory file lock."""

    def __init__(self, lock_path: str = "/tmp/video_pipeline.lock"):
        self.lock_path = Path(lock_path)
        self._fd = None

    def acquire(self) -> bool:
        """Try to acquire the lock. Returns True if successful."""
        self._fd = open(self.lock_path, "w")
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fd.write(f"{os.getpid()}\n")
            self._fd.flush()
            return True
        except (IOError, OSError):
            self._fd.close()
            self._fd = None
            return False

    def release(self):
        """Release the lock and clean up."""
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                self._fd.close()
            except Exception:
                pass
            self._fd = None
        try:
            self.lock_path.unlink(missing_ok=True)
        except Exception:
            pass

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError(
                f"Another video build is already running (lock: {self.lock_path}). "
                "Only one job at a time is allowed."
            )
        return self

    def __exit__(self, *args):
        self.release()


class TempDirManager:
    """Create a unique temporary directory that is guaranteed to be deleted on exit."""

    def __init__(self, prefix: str = "video_pipeline_", base_dir: Optional[str] = None):
        self.prefix = prefix
        self.base_dir = base_dir or tempfile.gettempdir()
        self._path: Optional[Path] = None
        self._deleted = False

    @property
    def path(self) -> Path:
        if self._path is None:
            raise RuntimeError("TempDirManager not yet entered (use 'with' statement)")
        return self._path

    def __enter__(self) -> "TempDirManager":
        self._path = Path(tempfile.mkdtemp(prefix=self.prefix, dir=self.base_dir))
        atexit.register(self._cleanup)
        return self

    def __exit__(self, *args):
        self._cleanup()

    def _cleanup(self):
        if self._path and not self._deleted:
            try:
                shutil.rmtree(self._path, ignore_errors=True)
                self._deleted = True
            except Exception:
                pass


@contextmanager
def resource_guard(max_cores: int = 2, nice_level: int = 19):
    """
    Context manager that applies CPU resource limits for the current process.

    - Sets process niceness to lowest priority (prevents starving other processes)
    - CPU affinity constrained to specified cores
    - On exit, does NOT restore (process is exiting anyway)
    """
    pid = os.getpid()
    try:
        os.nice(nice_level)
    except PermissionError:
        pass  # non-root may not be able to set niceness; best-effort

    if max_cores > 0:
        try:
            os.sched_setaffinity(pid, range(max_cores))
        except (OSError, AttributeError, PermissionError):
            pass  # best-effort; not all systems support affinity

    yield


def wind_down(temp_dirs=None, lock=None):
    """
    Full wind-down protocol after a video build.

    Args:
        temp_dirs: list of Path objects to temporary directories to delete
        lock: LockFile instance to release
    """
    errors = []

    # 1. Delete temp directories
    if temp_dirs:
        for d in temp_dirs:
            try:
                if isinstance(d, Path) and d.exists():
                    shutil.rmtree(d, ignore_errors=True)
                elif isinstance(d, str):
                    p = Path(d)
                    if p.exists():
                        shutil.rmtree(p, ignore_errors=True)
            except Exception as e:
                errors.append(f"Failed to delete {d}: {e}")

    # 2. Release lock
    if lock:
        try:
            lock.release()
        except Exception as e:
            errors.append(f"Failed to release lock: {e}")

    # 3. Force garbage collection to release memory
    gc.collect()

    # 4. Verify idle state
    idle_ok, idle_msg = verify_idle()
    if not idle_ok:
        errors.append(f"Idle check failed: {idle_msg}")

    if errors:
        print(f"Wind-down completed with {len(errors)} warning(s):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
    else:
        print("Wind-down complete: VM returned to idle state.", file=sys.stderr)


def verify_idle():
    """
    Verify the VM is in idle state:
    - Load average < 0.2
    - Free RAM > 15 GB
    Returns: (is_idle: bool, message: str)
    """
    try:
        load = os.getloadavg()[0]
    except (OSError, AttributeError):
        load = 0.0

    free_ram_gb = 0.0
    try:
        with open("/proc/meminfo") as f:
            meminfo = f.read()
        for line in meminfo.splitlines():
            if line.startswith("MemAvailable:"):
                free_ram_gb = int(line.split()[1]) / (1024 * 1024)  # kB to GB
                break
    except Exception:
        free_ram_gb = 0.0

    issues = []
    if load >= 0.2:
        issues.append(f"Load average {load:.2f} >= 0.2")
    if free_ram_gb <= 15.0:
        issues.append(f"Free RAM {free_ram_gb:.1f} GB <= 15 GB")

    if issues:
        return False, "; ".join(issues)
    return True, f"Load={load:.2f}, Free RAM={free_ram_gb:.1f} GB"


# Register signal handlers so cleanup happens even on SIGTERM/SIGINT
_cleanup_registry = []


def register_cleanup(callback, *args, **kwargs):
    """Register a cleanup function to be called on exit or signal."""
    _cleanup_registry.append((callback, args, kwargs))


def _on_exit():
    for cb, args, kwargs in _cleanup_registry:
        try:
            cb(*args, **kwargs)
        except Exception:
            pass


def _signal_handler(signum, frame):
    _on_exit()
    sys.exit(128 + signum)


signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)
atexit.register(_on_exit)
