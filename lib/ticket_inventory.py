"""Read-only, source-bounded Ticketinventur capture. No completion decisions."""
from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EVIDENCE_DIRS = {"SOLVED", "DONE", "_archive"}
CACHE_DIRS = {".git", ".pytest_cache", "__pycache__"}
OPEN_DIRS = {"INBOX", "ACTIONABLE", "QUEUED", "BLOCKED", "WAITING", "USER", "PARKED", "PENDING", ".USER"}
ID_RE = re.compile(r"(?m)^ID:\s*(T-\d{8}-\d+)\b")
NAME_RE = re.compile(r"^(T-\d{8}-\d+)(?:[._]|$)")


class _TraversalProblem(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _NotDirectory(Exception):
    pass


class _PosixHandles:
    """Descriptor-relative traversal: no path lookup below a held directory."""

    def __init__(self) -> None:
        required = (
            hasattr(os, "O_NOFOLLOW")
            and hasattr(os, "O_DIRECTORY")
            and os.open in getattr(os, "supports_dir_fd", set())
            and os.listdir in getattr(os, "supports_fd", set())
        )
        if not required:
            raise _TraversalProblem("safe-handle-traversal-unavailable")
        self.dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        self.file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
        self.root_device: int | None = None

    def close(self, handle: int) -> None:
        os.close(handle)

    def open_queue(self, path: Path) -> int:
        handle = os.open(os.sep, self.dir_flags)
        try:
            for name in path.parts[1:]:
                if name in ("", ".", os.sep):
                    continue
                if name == ".." or os.sep in name:
                    raise _TraversalProblem("queue-path-invalid")
                try:
                    child = os.open(name, self.dir_flags, dir_fd=handle)
                except OSError as exc:
                    if exc.errno == errno.ELOOP:
                        raise _TraversalProblem("queue-or-ancestor-link-reparse") from None
                    raise _TraversalProblem("queue-or-ancestor-unreadable") from None
                os.close(handle)
                handle = child
            info = os.fstat(handle)
            if not stat.S_ISDIR(info.st_mode):
                raise _TraversalProblem("queue-or-ancestor-unreadable")
            self.root_device = info.st_dev
            return handle
        except BaseException:
            os.close(handle)
            raise

    def list_entries(self, directory: int) -> list[tuple[str, None]]:
        scan = os.open(".", self.dir_flags, dir_fd=directory)
        try:
            return [(name, None) for name in os.listdir(scan)]
        finally:
            os.close(scan)

    def entry_is_reparse(self, attributes: None) -> bool:
        # POSIX does not expose attributes from listdir(fd). Classification is
        # deferred to the no-follow, descriptor-relative open operation.
        return False

    def entry_is_directory(self, attributes: None) -> None:
        # Probe with openat(O_DIRECTORY | O_NOFOLLOW); do not stat by path.
        return None

    def open_directory(self, parent: int, name: str) -> int:
        try:
            child = os.open(name, self.dir_flags, dir_fd=parent)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise _TraversalProblem("link-or-reparse-not-followed") from None
            if exc.errno == errno.ENOTDIR:
                raise _NotDirectory from None
            raise _TraversalProblem("directory-unreadable") from None
        info = os.fstat(child)
        if not stat.S_ISDIR(info.st_mode):
            os.close(child)
            raise _TraversalProblem("entry-changed-before-open")
        if self.root_device is not None and info.st_dev != self.root_device:
            os.close(child)
            raise _TraversalProblem("cross-device-entry-not-followed")
        return child

    def open_file(self, parent: int, name: str) -> int:
        try:
            handle = os.open(name, self.file_flags, dir_fd=parent)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise _TraversalProblem("link-or-reparse-not-followed") from None
            raise _TraversalProblem("file-unreadable") from None
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode):
            os.close(handle)
            raise _TraversalProblem("not-a-regular-file")
        if info.st_nlink != 1:
            os.close(handle)
            raise _TraversalProblem("multiple-hardlinks-not-followed")
        if self.root_device is not None and info.st_dev != self.root_device:
            os.close(handle)
            raise _TraversalProblem("cross-device-entry-not-followed")
        return handle

    def identity(self, handle: int) -> tuple[int, int, int, int, int, int]:
        info = os.fstat(handle)
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)

    def read_file(self, handle: int) -> bytes:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(handle, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)


class _WindowsHandles:
    """Handle-relative NT traversal with reparse traversal explicitly disabled."""

    FILE_LIST_DIRECTORY = 0x0001
    FILE_TRAVERSE = 0x0020
    FILE_READ_ATTRIBUTES = 0x0080
    SYNCHRONIZE = 0x00100000
    GENERIC_READ = 0x80000000
    FILE_SHARE_ALL = 0x00000007
    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    FILE_OPEN_REPARSE_POINT = 0x00200000
    FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    OBJ_CASE_INSENSITIVE = 0x00000040
    OBJ_DONT_REPARSE = 0x00001000
    FILE_OPEN = 1
    FILE_DIRECTORY_FILE = 0x00000001
    FILE_NON_DIRECTORY_FILE = 0x00000040
    FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
    ERROR_NO_MORE_FILES = 18
    STATUS_REPARSE_POINT_ENCOUNTERED = 0xC000050B
    STATUS_STOPPED_ON_SYMLINK = 0x8000002D

    class _UnicodeString:
        pass

    class _ObjectAttributes:
        pass

    class _IoStatusBlock:
        pass

    class _FileAttributeTagInfo:
        pass

    class _FileBasicInfo:
        pass

    class _FileStandardInfo:
        pass

    class _FileIdInfo:
        pass

    class _FileIdBothDirectoryInfo:
        pass

    def __init__(self) -> None:
        if os.name != "nt":
            raise _TraversalProblem("safe-handle-traversal-unavailable")
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes

        class UnicodeString(ctypes.Structure):
            _fields_ = [
                ("Length", wintypes.USHORT),
                ("MaximumLength", wintypes.USHORT),
                ("Buffer", wintypes.LPWSTR),
            ]

        class ObjectAttributes(ctypes.Structure):
            _fields_ = [
                ("Length", wintypes.ULONG),
                ("RootDirectory", wintypes.HANDLE),
                ("ObjectName", ctypes.POINTER(UnicodeString)),
                ("Attributes", wintypes.ULONG),
                ("SecurityDescriptor", wintypes.LPVOID),
                ("SecurityQualityOfService", wintypes.LPVOID),
            ]

        class IoStatusBlock(ctypes.Structure):
            _fields_ = [("Status", ctypes.c_long), ("Information", ctypes.c_size_t)]

        class FileAttributeTagInfo(ctypes.Structure):
            _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]

        class FileBasicInfo(ctypes.Structure):
            _fields_ = [
                ("CreationTime", ctypes.c_longlong),
                ("LastAccessTime", ctypes.c_longlong),
                ("LastWriteTime", ctypes.c_longlong),
                ("ChangeTime", ctypes.c_longlong),
                ("FileAttributes", wintypes.DWORD),
            ]

        class FileStandardInfo(ctypes.Structure):
            _fields_ = [
                ("AllocationSize", ctypes.c_longlong),
                ("EndOfFile", ctypes.c_longlong),
                ("NumberOfLinks", wintypes.DWORD),
                ("DeletePending", wintypes.BOOLEAN),
                ("Directory", wintypes.BOOLEAN),
            ]

        class FileIdInfo(ctypes.Structure):
            _fields_ = [
                ("VolumeSerialNumber", ctypes.c_ulonglong),
                ("FileId", ctypes.c_ubyte * 16),
            ]

        class FileIdBothDirectoryInfo(ctypes.Structure):
            _fields_ = [
                ("NextEntryOffset", wintypes.DWORD),
                ("FileIndex", wintypes.DWORD),
                ("CreationTime", ctypes.c_longlong),
                ("LastAccessTime", ctypes.c_longlong),
                ("LastWriteTime", ctypes.c_longlong),
                ("ChangeTime", ctypes.c_longlong),
                ("EndOfFile", ctypes.c_longlong),
                ("AllocationSize", ctypes.c_longlong),
                ("FileAttributes", wintypes.DWORD),
                ("FileNameLength", wintypes.DWORD),
                ("EaSize", wintypes.DWORD),
                ("ShortNameLength", wintypes.BYTE),
                ("ShortName", wintypes.WCHAR * 12),
                ("FileId", ctypes.c_longlong),
                ("FileName", wintypes.WCHAR * 1),
            ]

        self.UnicodeString = UnicodeString
        self.ObjectAttributes = ObjectAttributes
        self.IoStatusBlock = IoStatusBlock
        self.FileAttributeTagInfo = FileAttributeTagInfo
        self.FileBasicInfo = FileBasicInfo
        self.FileStandardInfo = FileStandardInfo
        self.FileIdInfo = FileIdInfo
        self.FileIdBothDirectoryInfo = FileIdBothDirectoryInfo

        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.ntdll = ctypes.WinDLL("ntdll")
        self.kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        self.kernel32.CreateFileW.restype = wintypes.HANDLE
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self.kernel32.GetFileInformationByHandleEx.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        ]
        self.kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
        self.kernel32.ReadFile.argtypes = [
            wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
        ]
        self.kernel32.ReadFile.restype = wintypes.BOOL
        self.ntdll.NtCreateFile.argtypes = [
            ctypes.POINTER(wintypes.HANDLE), wintypes.ULONG,
            ctypes.POINTER(ObjectAttributes), ctypes.POINTER(IoStatusBlock),
            ctypes.c_void_p, wintypes.ULONG, wintypes.ULONG, wintypes.ULONG,
            wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG,
        ]
        self.ntdll.NtCreateFile.restype = ctypes.c_long
        self.root_volume: int | None = None

    def _close(self, handle: Any) -> None:
        if handle and handle != self.wintypes.HANDLE(-1).value:
            self.kernel32.CloseHandle(handle)

    def close(self, handle: Any) -> None:
        self._close(handle)

    def _information(self, handle: Any, info_class: int, info: Any) -> None:
        ok = self.kernel32.GetFileInformationByHandleEx(
            handle, info_class, self.ctypes.byref(info), self.ctypes.sizeof(info)
        )
        if not ok:
            raise OSError(self.ctypes.get_last_error(), "GetFileInformationByHandleEx failed")

    def _attributes(self, handle: Any) -> int:
        info = self.FileAttributeTagInfo()
        self._information(handle, 9, info)
        return int(info.FileAttributes)

    def _file_state(self, handle: Any) -> tuple[Any, ...]:
        standard = self.FileStandardInfo()
        basic = self.FileBasicInfo()
        identity = self.FileIdInfo()
        self._information(handle, 1, standard)
        self._information(handle, 0, basic)
        self._information(handle, 18, identity)
        attributes = int(basic.FileAttributes)
        if attributes & self.FILE_ATTRIBUTE_REPARSE_POINT:
            raise _TraversalProblem("link-or-reparse-not-followed")
        return (
            int(identity.VolumeSerialNumber),
            bytes(identity.FileId),
            int(standard.EndOfFile),
            int(basic.LastWriteTime),
            int(basic.ChangeTime),
            int(standard.NumberOfLinks),
            bool(standard.Directory),
        )

    def _open_relative(self, parent: Any, name: str, *, directory: bool) -> Any:
        if not name or name in (".", "..") or "\\" in name or "/" in name or ":" in name:
            raise _TraversalProblem("entry-name-invalid")
        ctypes = self.ctypes
        wintypes = self.wintypes
        name_buffer = ctypes.create_unicode_buffer(name)
        unicode_name = self.UnicodeString()
        unicode_name.Length = len(name.encode("utf-16-le"))
        unicode_name.MaximumLength = unicode_name.Length + 2
        unicode_name.Buffer = ctypes.cast(name_buffer, wintypes.LPWSTR)
        attributes = self.ObjectAttributes()
        attributes.Length = ctypes.sizeof(self.ObjectAttributes)
        attributes.RootDirectory = parent
        attributes.ObjectName = ctypes.pointer(unicode_name)
        attributes.Attributes = self.OBJ_CASE_INSENSITIVE | self.OBJ_DONT_REPARSE
        iosb = self.IoStatusBlock()
        output = wintypes.HANDLE()
        access = self.FILE_LIST_DIRECTORY | self.FILE_TRAVERSE | self.FILE_READ_ATTRIBUTES | self.SYNCHRONIZE
        options = self.FILE_OPEN_REPARSE_POINT | self.FILE_SYNCHRONOUS_IO_NONALERT
        if directory:
            options |= self.FILE_DIRECTORY_FILE
        else:
            access = self.GENERIC_READ | self.FILE_READ_ATTRIBUTES | self.SYNCHRONIZE
            options |= self.FILE_NON_DIRECTORY_FILE
        status = self.ntdll.NtCreateFile(
            ctypes.byref(output), access, ctypes.byref(attributes), ctypes.byref(iosb),
            None, 0, self.FILE_SHARE_ALL, self.FILE_OPEN, options, None, 0,
        )
        unsigned_status = ctypes.c_uint32(status).value
        if status < 0:
            if unsigned_status in (self.STATUS_REPARSE_POINT_ENCOUNTERED, self.STATUS_STOPPED_ON_SYMLINK):
                raise _TraversalProblem("link-or-reparse-not-followed")
            raise _TraversalProblem("directory-unreadable" if directory else "file-unreadable")
        handle = output
        try:
            state = self._file_state(handle)
            if state[0] != self.root_volume:
                raise _TraversalProblem("cross-volume-entry-not-followed")
            if directory and not state[6]:
                raise _TraversalProblem("entry-changed-before-open")
            if not directory and state[6]:
                raise _TraversalProblem("entry-changed-before-open")
            if not directory and state[5] != 1:
                raise _TraversalProblem("multiple-hardlinks-not-followed")
            return handle
        except BaseException:
            self._close(handle)
            raise

    def _root_parts(self, path: Path) -> tuple[str, list[str]]:
        import ntpath

        raw = ntpath.normpath(str(path)).replace("/", "\\")
        drive, tail = ntpath.splitdrive(raw)
        if not drive or not ntpath.isabs(raw) or drive.startswith("\\\\.\\"):
            raise _TraversalProblem("queue-path-invalid")
        lower_drive = drive.lower()
        if lower_drive.startswith("\\\\?\\globalroot"):
            raise _TraversalProblem("queue-path-invalid")
        if lower_drive.startswith("\\\\?\\unc\\"):
            root = drive.rstrip("\\") + "\\"
        elif lower_drive.startswith("\\\\?\\"):
            root = drive.rstrip("\\") + "\\"
        elif drive.startswith("\\\\"):
            root = "\\\\?\\UNC\\" + drive[2:].rstrip("\\") + "\\"
        elif len(drive) == 2 and drive[1] == ":":
            root = "\\\\?\\" + drive + "\\"
        else:
            raise _TraversalProblem("queue-path-invalid")
        components = [part for part in tail.split("\\") if part not in ("", "\\")]
        if any(part in (".", "..") for part in components):
            raise _TraversalProblem("queue-path-invalid")
        return root, components

    def open_queue(self, path: Path) -> Any:
        root_path, components = self._root_parts(path)
        access = self.FILE_LIST_DIRECTORY | self.FILE_TRAVERSE | self.FILE_READ_ATTRIBUTES | self.SYNCHRONIZE
        handle = self.kernel32.CreateFileW(
            root_path, access, self.FILE_SHARE_ALL, None, self.OPEN_EXISTING,
            self.FILE_FLAG_BACKUP_SEMANTICS | self.FILE_FLAG_OPEN_REPARSE_POINT, None,
        )
        if handle == self.wintypes.HANDLE(-1).value:
            raise _TraversalProblem("queue-or-ancestor-unreadable")
        try:
            state = self._file_state(handle)
            if not state[6]:
                raise _TraversalProblem("queue-or-ancestor-unreadable")
            self.root_volume = state[0]
            for name in components:
                child = self._open_relative(handle, name, directory=True)
                self._close(handle)
                handle = child
            return handle
        except BaseException:
            self._close(handle)
            raise

    def list_entries(self, directory: Any) -> list[tuple[str, int]]:
        ctypes = self.ctypes
        entries: list[tuple[str, int]] = []
        first = True
        while True:
            buffer = ctypes.create_string_buffer(64 * 1024)
            info_class = 11 if first else 10
            ok = self.kernel32.GetFileInformationByHandleEx(
                directory, info_class, buffer, len(buffer)
            )
            if not ok:
                error = ctypes.get_last_error()
                if error == self.ERROR_NO_MORE_FILES:
                    break
                raise _TraversalProblem("directory-unreadable")
            offset = 0
            parsed = 0
            while True:
                record = ctypes.cast(
                    ctypes.byref(buffer, offset),
                    ctypes.POINTER(self.FileIdBothDirectoryInfo),
                ).contents
                name_length = int(record.FileNameLength)
                name_offset = self.FileIdBothDirectoryInfo.FileName.offset
                if name_length % 2 or name_offset + name_length > len(buffer) - offset:
                    raise _TraversalProblem("directory-entry-invalid")
                name = ctypes.wstring_at(
                    ctypes.addressof(buffer) + offset + name_offset, name_length // 2
                )
                entries.append((name, int(record.FileAttributes)))
                parsed += 1
                next_offset = int(record.NextEntryOffset)
                if next_offset == 0:
                    break
                if next_offset < name_offset + name_length or offset + next_offset > len(buffer):
                    raise _TraversalProblem("directory-entry-invalid")
                offset += next_offset
            first = False
            if parsed == 0:
                break
        return entries

    def entry_is_reparse(self, attributes: int | None) -> bool:
        return attributes is not None and bool(attributes & self.FILE_ATTRIBUTE_REPARSE_POINT)

    def entry_is_directory(self, attributes: int | None) -> bool | None:
        if attributes is None:
            return None
        return bool(attributes & self.FILE_ATTRIBUTE_DIRECTORY)

    def open_directory(self, parent: Any, name: str) -> Any:
        return self._open_relative(parent, name, directory=True)

    def open_file(self, parent: Any, name: str) -> Any:
        return self._open_relative(parent, name, directory=False)

    def identity(self, handle: Any) -> tuple[Any, ...]:
        return self._file_state(handle)

    def read_file(self, handle: Any) -> bytes:
        ctypes = self.ctypes
        chunks: list[bytes] = []
        while True:
            buffer = ctypes.create_string_buffer(1024 * 1024)
            received = self.wintypes.DWORD()
            ok = self.kernel32.ReadFile(
                handle, buffer, len(buffer), ctypes.byref(received), None
            )
            if not ok:
                raise _TraversalProblem("file-unreadable")
            if not received.value:
                break
            chunks.append(buffer.raw[: received.value])
        return b"".join(chunks)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sections(raw: bytes, include_content: bool) -> list[dict[str, Any]]:
    """Contiguous byte ranges cover every byte, including separators and BOM."""
    ends = [match.end() for match in re.finditer(rb"(?:\r?\n){2,}", raw)]
    if not ends or ends[-1] != len(raw):
        ends.append(len(raw))
    rows = []
    start = 0
    for end in ends:
        chunk = raw[start:end]
        row: dict[str, Any] = {
            "section": len(rows) + 1,
            "byte_start": start,
            "byte_end": end,
            "sha256": _sha(chunk),
            "disposition": "pending-semantic-review",
        }
        if include_content:
            row["text"] = chunk.decode("utf-8-sig" if start == 0 else "utf-8")
        rows.append(row)
        start = end
    return rows


def _add_source(
    result: dict[str, Any],
    *,
    relative: str,
    name: str,
    parent_parts: tuple[str, ...],
    raw: bytes,
    include_content: bool,
) -> None:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError:
        text = None
    match = ID_RE.search(text or "") or NAME_RE.match(name)
    lock_file = name.startswith("LOCK.")
    ticket_file = bool(match) and name.endswith(".txt") and (
        not parent_parts or (len(parent_parts) == 1 and parent_parts[0] in OPEN_DIRS)
    )
    row: dict[str, Any] = {
        "path": relative,
        "id": match.group(1) if match and not lock_file else None,
        "bytes": len(raw),
        "sha256": _sha(raw),
        "kind": "lock" if lock_file else ("ticket" if ticket_file else "ancillary"),
        "source_class": "historical-log-evidence" if "_logs" in parent_parts else "queue-source",
        "location": relative.split("/")[0] if "/" in relative else "ROOT",
        "encoding": "utf-8" if text is not None else "opaque-needs-reader",
        "disposition": "KEEP_ACTIVE_PENDING_REVIEW",
        "semantic_review": "pending",
        "current_directive": None,
        "rest": None,
        "successor": None,
        "acceptance": None,
        "sections": _sections(raw, include_content) if text is not None else [],
    }
    result["sources"].append(row)


def capture_inventory(queue: Path | str, *, include_content: bool = False) -> dict[str, Any]:
    """Capture queue files only through stable directory and file handles.

    Each child is opened relative to a held queue-directory handle without
    following links/reparse points. The capture is sequential, not an atomic
    cloud snapshot; unsupported primitives fail closed before enumeration.
    """
    base = Path(os.path.abspath(Path(queue).expanduser()))
    result: dict[str, Any] = {
        "schema": "ticket-inventory.v1",
        "queue": str(base),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "state": "SEMANTIC_REVIEW_REQUIRED",
        "archive_go": False,
        "observation": "sequential-handle-relative-not-atomic",
        "sources": [],
        "excluded": [],
        "errors": [],
    }

    def error(path: str, code: str) -> None:
        result["errors"].append({"path": path, "code": code})

    if os.name == "nt":
        try:
            backend: Any = _WindowsHandles()
        except _TraversalProblem as exc:
            backend = None
            error(str(base), exc.code)
    elif os.name == "posix":
        try:
            backend = _PosixHandles()
        except _TraversalProblem as exc:
            backend = None
            error(str(base), exc.code)
    else:
        backend = None
        error(str(base), "safe-handle-traversal-unavailable")

    if backend is not None:
        try:
            root = backend.open_queue(base)
        except _TraversalProblem as exc:
            error(str(base), exc.code)
            root = None
        except OSError:
            error(str(base), "queue-or-ancestor-unreadable")
            root = None

        def visit(directory: Any, parent_parts: tuple[str, ...]) -> None:
            directory_label = "/".join(parent_parts) or str(base)
            try:
                before = backend.list_entries(directory)
            except (_TraversalProblem, OSError) as exc:
                error(directory_label, getattr(exc, "code", "directory-unreadable"))
                return
            names_before = sorted(name for name, _attrs in before)
            for name, attributes in before:
                if name in ("", ".", ".."):
                    continue
                relative = "/".join((*parent_parts, name))
                if backend.entry_is_reparse(attributes):
                    error(relative, "link-or-reparse-not-followed")
                    continue

                entry_is_directory = backend.entry_is_directory(attributes)
                if entry_is_directory is True:
                    if name in CACHE_DIRS or (not parent_parts and name in EVIDENCE_DIRS):
                        result["excluded"].append({
                            "path": relative,
                            "reason": "operational-cache" if name in CACHE_DIRS else "targeted-evidence-only",
                        })
                        continue
                    try:
                        child = backend.open_directory(directory, name)
                    except (_TraversalProblem, OSError) as exc:
                        error(relative, getattr(exc, "code", "directory-unreadable"))
                        continue
                    try:
                        visit(child, (*parent_parts, name))
                    finally:
                        backend.close(child)
                    continue

                if entry_is_directory is None:
                    try:
                        child = backend.open_directory(directory, name)
                    except _NotDirectory:
                        child = None
                    except (_TraversalProblem, OSError) as exc:
                        error(relative, getattr(exc, "code", "directory-unreadable"))
                        continue
                    if child is not None:
                        if name in CACHE_DIRS or (not parent_parts and name in EVIDENCE_DIRS):
                            result["excluded"].append({
                                "path": relative,
                                "reason": "operational-cache" if name in CACHE_DIRS else "targeted-evidence-only",
                            })
                        else:
                            try:
                                visit(child, (*parent_parts, name))
                            finally:
                                backend.close(child)
                        continue

                try:
                    handle = backend.open_file(directory, name)
                except (_TraversalProblem, OSError) as exc:
                    error(relative, getattr(exc, "code", "file-unreadable"))
                    continue
                try:
                    before_identity = backend.identity(handle)
                    raw = backend.read_file(handle)
                    after_identity = backend.identity(handle)
                    if before_identity != after_identity:
                        error(relative, "file-changed-during-read")
                        continue
                    _add_source(
                        result,
                        relative=relative,
                        name=name,
                        parent_parts=parent_parts,
                        raw=raw,
                        include_content=include_content,
                    )
                except (_TraversalProblem, OSError) as exc:
                    error(relative, getattr(exc, "code", "file-unreadable"))
                finally:
                    backend.close(handle)

            try:
                after = backend.list_entries(directory)
                if names_before != sorted(name for name, _attrs in after):
                    error(directory_label, "directory-changed-during-read")
            except (_TraversalProblem, OSError) as exc:
                error(directory_label, getattr(exc, "code", "directory-unreadable-after-read"))

        if root is not None:
            try:
                visit(root, ())
            finally:
                backend.close(root)

    result["sources"].sort(key=lambda item: item["path"])
    result["counts"] = {
        "sources": len(result["sources"]),
        "tickets": sum(item["kind"] == "ticket" for item in result["sources"]),
        "errors": len(result["errors"]),
    }
    if result["errors"]:
        result["state"] = "INCOMPLETE_CAPTURE_HOLD"
    return result
