"""Bounded, descriptor-relative filesystem access for the icon worker (Linux)."""
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import secrets
import stat
import time


class UnsafePath(ValueError):
    pass


class LimitExceeded(ValueError):
    pass


def identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def same_directory(first, second):
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


class Budget:
    def __init__(self):
        self.entries = 8192
        self.bytes = 128 * 1024 * 1024
        self.decodes = 64
        self.deadline = time.monotonic() + 60

    def take(self, kind, amount=1):
        if time.monotonic() >= self.deadline:
            raise LimitExceeded("scan exceeded 60 seconds")
        remaining = getattr(self, kind) - amount
        if remaining < 0:
            raise LimitExceeded(f"scan {kind} limit exceeded")
        setattr(self, kind, remaining)


class Directory:
    """Keep every ancestor open and reject redirected or writable path components."""
    def __init__(self, path, *, create=False, owned=True):
        self.path = Path(path)
        self.owned = owned
        self.chain = []
        if (not self.path.is_absolute() or ".." in self.path.parts or len(self.path.parts) > 32
                or "\n" in str(path) or "\r" in str(path)):
            raise UnsafePath(f"expected a bounded absolute path: {path}")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            self.chain.append((None, os.open("/", flags)))
            for part in self.path.parts[1:]:
                parent = self.chain[-1][1]
                self._check_directory(os.fstat(parent), ancestor=True)
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=parent)
                    except FileExistsError:
                        pass
                self.chain.append((part, os.open(part, flags, dir_fd=parent)))
            info = os.fstat(self.fd)
            self._check_directory(info)
            if owned and info.st_uid != os.getuid():
                raise UnsafePath(f"directory is not owned by current user: {path}")
            self.verify()
        except OSError as error:
            self.close()
            if error.errno in (errno.ELOOP, errno.ENOTDIR):
                raise UnsafePath(f"symlink or non-directory in path: {path}") from error
            raise
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _check_directory(info, ancestor=False):
        if info.st_uid not in (0, os.getuid()):
            raise UnsafePath("directory has an untrusted owner")
        # Root-owned sticky ancestors (e.g. /tmp) cannot replace our owned child.
        sticky = ancestor and info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        if info.st_mode & 0o022 and not sticky:
            raise UnsafePath("directory is writable by another user")

    @property
    def fd(self):
        return self.chain[-1][1]

    def verify(self):
        for index, (name, fd) in enumerate(self.chain):
            info = os.fstat(fd)
            self._check_directory(info, ancestor=index < len(self.chain) - 1)
            if index == len(self.chain) - 1 and self.owned and info.st_uid != os.getuid():
                raise UnsafePath(f"directory ownership changed: {self.path}")
            if index:
                named = os.stat(name, dir_fd=self.chain[index - 1][1], follow_symlinks=False)
                if not same_directory(info, named) or not stat.S_ISDIR(named.st_mode):
                    raise UnsafePath(f"directory changed during scan: {self.path}")

    def close(self):
        for _, fd in reversed(self.chain):
            os.close(fd)
        self.chain.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @staticmethod
    def _name(name):
        if not name or name in (".", "..") or "/" in name:
            raise UnsafePath("expected a single filename")

    def snapshot(self, name, *, owned=True):
        self._name(name)
        self.verify()
        try:
            info = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        owners = (os.getuid(),) if owned else (0, os.getuid())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in owners
                or info.st_mode & 0o022 or info.st_nlink != 1):
            raise UnsafePath(f"not a safe regular file: {self.path / name}")
        return identity(info)

    def unchanged(self, name, expected, *, owned=True):
        if self.snapshot(name, owned=owned) != expected:
            raise UnsafePath(f"file changed during scan: {self.path / name}")

    def read(self, name, limit, budget, *, owned=True, expected=None):
        before = self.snapshot(name, owned=owned)
        if before is None:
            raise FileNotFoundError(self.path / name)
        if expected is not None and before != expected:
            raise UnsafePath(f"file changed after discovery: {self.path / name}")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=self.fd)
        with os.fdopen(fd, "rb") as stream:
            if identity(os.fstat(stream.fileno())) != before:
                raise UnsafePath("file changed while opening")
            if before[6] > limit:
                raise LimitExceeded(f"file exceeds {limit} bytes: {self.path / name}")
            budget.take("bytes", before[6])
            data = stream.read(limit + 1)
            if len(data) > limit:
                raise LimitExceeded(f"file grew beyond {limit} bytes")
            if identity(os.fstat(stream.fileno())) != before:
                raise UnsafePath("file changed while reading")
        self.unchanged(name, before, owned=owned)
        return data, before

    def write(self, name, data, expected, *, before_replace=None):
        self._name(name)
        self.unchanged(name, expected)
        temporary = ".steam-icons-" + secrets.token_hex(16)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                os.fchmod(stream.fileno(), stat.S_IMODE(expected[2]) & 0o777 if expected else 0o600)
                stream.flush()
                os.fsync(stream.fileno())
                temporary_identity = identity(os.fstat(stream.fileno()))
            if before_replace:
                before_replace()
            self.unchanged(temporary, temporary_identity)
            # Revalidate both the retained ancestor chain and target immediately
            # before the descriptor-relative rename. Never follow the target.
            self.unchanged(name, expected)
            os.replace(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    @contextmanager
    def lock(self):
        import fcntl
        self.verify()
        self._name("lock")
        fd = os.open("lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     0o600, dir_fd=self.fd)
        try:
            expected = self.snapshot("lock")
            if identity(os.fstat(fd)) != expected:
                raise UnsafePath("lock changed while opening")
            # Event bursts must not accumulate workers waiting on a lock.
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.unchanged("lock", expected)
            yield lambda: self.unchanged("lock", expected)
        finally:
            os.close(fd)


def walk_files(path, budget, *, recursive=False):
    """No symlink traversal; one shared entry budget covers all discovery."""
    pending = [(Path(path), 0)]
    while pending:
        folder, depth = pending.pop()
        try:
            directory = Directory(folder, owned=False)
        except FileNotFoundError:
            continue
        with directory:
            with os.scandir(directory.fd) as entries:
                for item in entries:
                    budget.take("entries")
                    if item.is_dir(follow_symlinks=False) and recursive:
                        if depth >= 8:
                            raise LimitExceeded("icon directory depth exceeds 8")
                        pending.append((folder / item.name, depth + 1))
                    elif item.is_file(follow_symlinks=False):
                        yield folder / item.name
            directory.verify()
