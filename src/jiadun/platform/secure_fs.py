"""跨平台的目录句柄安全写入原语。

核心恢复逻辑只依赖 :class:`SecureDirectory` 的统一接口。POSIX 使用目录
fd、``O_NOFOLLOW`` 和 ``*at`` 系列操作；Windows 使用不共享删除的目录句柄、
``FILE_FLAG_OPEN_REPARSE_POINT`` 和句柄最终路径校验。这样调用方不会在完成
路径检查后再无保护地跟随一个可被替换的目录链。
"""
from __future__ import annotations

import ctypes
import os
import stat
from pathlib import Path
from typing import BinaryIO


class SecureDirectory:
    """持有一个不会被本次进程主动释放前删除替换的目录句柄。"""

    def __init__(self, path: Path, *, fd: int | None = None, handle: int | None = None):
        self.path = Path(path).absolute()
        self._fd = fd
        self._handle = handle

    @classmethod
    def open(cls, path: Path) -> SecureDirectory:
        path = Path(path).absolute()
        if os.name == "nt":
            return cls._open_windows(path)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise ValueError(f"无法以安全目录句柄打开：{path}") from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode):
                raise NotADirectoryError(str(path))
        except BaseException:
            os.close(fd)
            raise
        return cls(path, fd=fd)

    @classmethod
    def open_or_create(
        cls, path: Path
    ) -> tuple[SecureDirectory, list[Path], list[SecureDirectory]]:
        """打开目录链并逐级安全创建缺失项。

        返回最终目录、此次创建的 lexical 路径和仍需保持打开的全部句柄。
        句柄列表不能提前关闭：POSIX 上后续操作依赖目录 fd，Windows 上
        目录句柄不共享删除，以阻止父项在检查与使用之间被替换。
        """
        absolute = Path(path).absolute()
        parts = absolute.parts
        if not parts:
            raise ValueError(f"无法确定安全目录根：{path}")
        current = cls.open(Path(parts[0]))
        guards = [current]
        created_paths: list[Path] = []
        try:
            for name in parts[1:]:
                child, created = current.child(name, create=True)
                guards.append(child)
                current = child
                if created:
                    created_paths.append(child.path)
            return current, created_paths, guards
        except BaseException:
            for guard in reversed(guards):
                guard.close()
            raise

    def close(self) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            os.close(fd)
        if self._handle is not None:
            handle, self._handle = self._handle, None
            _close_windows_handle(handle)

    def __enter__(self) -> SecureDirectory:
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def child(self, name: str, *, create: bool = False) -> tuple[SecureDirectory, bool]:
        """打开一个直接子目录，返回 ``(句柄, 是否由本调用创建)``。"""
        _validate_component(name)
        if self._fd is not None:
            created = False
            if create:
                try:
                    os.mkdir(name, dir_fd=self._fd)
                    created = True
                except FileExistsError:
                    pass
            child_path = self.path / name
            return self._open_posix_child(child_path, name), created

        if self._handle is not None:
            child_path = self.path / name
            return _open_windows_relative_directory(
                self._handle, child_path, name, create=create
            )
        raise ValueError("安全目录句柄已经关闭")

    def create_file(self, name: str) -> BinaryIO:
        """在当前目录中独占创建普通文件，并返回仍绑定到句柄的二进制流。"""
        _validate_component(name)
        if self._fd is not None:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            try:
                fd = os.open(name, flags, 0o600, dir_fd=self._fd)
            except FileExistsError:
                raise
            except OSError as exc:
                raise ValueError(f"无法安全创建文件：{self.path / name}") from exc
            return os.fdopen(fd, "wb")

        if self._handle is not None:
            return _create_windows_file(self._handle, self.path / name, name)
        raise ValueError("安全目录句柄已经关闭")

    def open_file(self, name: str) -> BinaryIO:
        """打开当前目录中的既有普通文件，拒绝 symlink/reparse。"""
        _validate_component(name)
        if self._fd is not None:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_CLOEXEC", 0)
            try:
                fd = os.open(name, flags, dir_fd=self._fd)
            except OSError as exc:
                raise ValueError(f"无法安全打开文件：{self.path / name}") from exc
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):
                    raise ValueError(f"路径不是普通文件：{self.path / name}")
            except BaseException:
                os.close(fd)
                raise
            return os.fdopen(fd, "rb")

        if self._handle is not None:
            return _open_windows_file(self._handle, self.path / name, name)
        raise ValueError("安全目录句柄已经关闭")

    def _open_posix_child(self, path: Path, name: str) -> SecureDirectory:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(name, flags, dir_fd=self._fd)
        except OSError as exc:
            raise ValueError(f"无法安全打开子目录：{path}") from exc
        return SecureDirectory(path, fd=fd)

    @classmethod
    def _open_windows(cls, path: Path) -> SecureDirectory:
        handle = _open_windows_directory(path)
        return cls(path, handle=handle)


def _validate_component(name: str) -> None:
    if not isinstance(name, str) or not name or name in {".", ".."}:
        raise ValueError(f"非法安全路径段：{name!r}")
    if "/" in name or "\\" in name or "\x00" in name:
        raise ValueError(f"安全路径段不能包含分隔符或 NUL：{name!r}")


if os.name == "nt":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _FILE_READ_ATTRIBUTES = 0x80
    _FILE_LIST_DIRECTORY = 0x0001
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _FILE_SHARE_DELETE = 0x00000004
    _OPEN_EXISTING = 3
    _CREATE_NEW = 1
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _FILE_ATTRIBUTE_DIRECTORY = 0x10
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_CREATE = 2
    _FILE_OPEN = 1
    _FILE_OPEN_IF = 3
    _FILE_DIRECTORY_FILE = 0x00000001
    _FILE_NON_DIRECTORY_FILE = 0x00000040
    _FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
    _OBJ_CASE_INSENSITIVE = 0x00000040
    _SYNCHRONIZE = 0x00100000
    _STATUS_OBJECT_NAME_COLLISION = -1073741771  # 0xC0000035
    _FILE_CREATED = 2
    _FILE_OPENED = 1

    _kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    _kernel32.CreateFileW.restype = ctypes.c_void_p
    _kernel32.CreateDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
    _kernel32.CreateDirectoryW.restype = ctypes.c_int
    _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    _kernel32.CloseHandle.restype = ctypes.c_int
    _kernel32.GetFileInformationByHandle.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _kernel32.GetFileInformationByHandle.restype = ctypes.c_int
    _kernel32.GetFinalPathNameByHandleW.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32
    ]
    _kernel32.GetFinalPathNameByHandleW.restype = ctypes.c_uint32

    class _UnicodeString(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_uint16),
            ("maximum_length", ctypes.c_uint16),
            ("buffer", ctypes.c_wchar_p),
        ]

    class _ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_uint32),
            ("root_directory", ctypes.c_void_p),
            ("object_name", ctypes.POINTER(_UnicodeString)),
            ("attributes", ctypes.c_uint32),
            ("security_descriptor", ctypes.c_void_p),
            ("security_quality_of_service", ctypes.c_void_p),
        ]

    class _IoStatusBlock(ctypes.Structure):
        _fields_ = [("status", ctypes.c_void_p), ("information", ctypes.c_size_t)]

    _ntdll.NtCreateFile.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_uint32,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock),
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    _ntdll.NtCreateFile.restype = ctypes.c_long

    class _FileTime(ctypes.Structure):
        _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("attributes", ctypes.c_uint32),
            ("creation_time", _FileTime),
            ("last_access_time", _FileTime),
            ("last_write_time", _FileTime),
            ("volume_serial", ctypes.c_uint32),
            ("file_size_high", ctypes.c_uint32),
            ("file_size_low", ctypes.c_uint32),
            ("number_of_links", ctypes.c_uint32),
            ("file_index_high", ctypes.c_uint32),
            ("file_index_low", ctypes.c_uint32),
        ]


def _windows_error(path: Path) -> OSError:
    error = ctypes.get_last_error()
    return OSError(error, f"Windows 文件系统操作失败（错误 {error}）：{path}", str(path))


def _windows_path(path: Path) -> str:
    value = str(Path(path).absolute())
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def _normalise_windows_final_path(value: str) -> str:
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return os.path.normcase(os.path.normpath(value))


def _windows_final_path(handle: int, requested: Path) -> None:
    capacity = 512
    while capacity <= 32768:
        buffer = ctypes.create_unicode_buffer(capacity)
        length = _kernel32.GetFinalPathNameByHandleW(handle, buffer, capacity, 0)
        if length == 0:
            raise _windows_error(requested)
        if length < capacity - 1:
            actual = _normalise_windows_final_path(buffer.value)
            expected = _normalise_windows_final_path(str(requested.absolute()))
            if actual != expected:
                raise ValueError(f"句柄最终路径越界或发生 reparse：{requested}")
            return
        capacity *= 2
    raise ValueError(f"句柄最终路径过长，拒绝继续：{requested}")


def _windows_handle_info(handle: int, path: Path) -> _ByHandleFileInformation:
    info = _ByHandleFileInformation()
    if not _kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise _windows_error(path)
    return info


def _open_windows_directory(path: Path) -> int:
    handle = _kernel32.CreateFileW(
        _windows_path(path),
        _FILE_READ_ATTRIBUTES | _FILE_LIST_DIRECTORY,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle in {None, _INVALID_HANDLE_VALUE}:
        raise ValueError(f"无法以安全句柄打开目录：{path}") from _windows_error(path)
    try:
        info = _windows_handle_info(handle, path)
        if not info.attributes & _FILE_ATTRIBUTE_DIRECTORY:
            raise NotADirectoryError(str(path))
        if info.attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError(f"目录是 reparse point，拒绝继续：{path}")
        _windows_final_path(handle, path)
        return int(handle)
    except BaseException:
        _close_windows_handle(handle)
        raise


def _nt_error(path: Path, status: int) -> OSError:
    unsigned_status = ctypes.c_uint32(status).value
    return OSError(
        unsigned_status,
        f"Windows NT 文件系统操作失败（状态 0x{unsigned_status:08X}）：{path}",
        str(path),
    )


def _nt_create_relative(
    parent_handle: int,
    name: str,
    *,
    path: Path,
    directory: bool,
    create: bool,
) -> tuple[int, int]:
    """以目录句柄为 RootDirectory 创建/打开一个直接子项。

    这是 Windows 上关闭 reparse/path TOCTOU 的关键：对象名只有一个已校验
    的子项，NT 内核不会重新解析调用方提供的整条字符串路径。
    """
    name_buffer = ctypes.create_unicode_buffer(name)
    name_length = len(name.encode("utf-16-le"))
    object_name = _UnicodeString(
        name_length,
        name_length + ctypes.sizeof(ctypes.c_wchar),
        ctypes.cast(name_buffer, ctypes.c_wchar_p),
    )
    object_attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes),
        ctypes.c_void_p(parent_handle),
        ctypes.pointer(object_name),
        _OBJ_CASE_INSENSITIVE,
        None,
        None,
    )
    io_status = _IoStatusBlock()
    handle = ctypes.c_void_p()
    if directory:
        desired_access = _FILE_READ_ATTRIBUTES | _FILE_LIST_DIRECTORY | _SYNCHRONIZE
        create_options = (
            _FILE_DIRECTORY_FILE
            | _FILE_SYNCHRONOUS_IO_NONALERT
            | _FILE_FLAG_OPEN_REPARSE_POINT
        )
        file_attributes = _FILE_ATTRIBUTE_DIRECTORY
        disposition = _FILE_OPEN_IF if create else _FILE_OPEN
    else:
        desired_access = _GENERIC_READ | (_GENERIC_WRITE if create else 0) | _SYNCHRONIZE
        create_options = (
            _FILE_NON_DIRECTORY_FILE
            | _FILE_SYNCHRONOUS_IO_NONALERT
            | _FILE_FLAG_OPEN_REPARSE_POINT
        )
        file_attributes = _FILE_ATTRIBUTE_NORMAL
        disposition = _FILE_CREATE if create else _FILE_OPEN

    status = int(
        _ntdll.NtCreateFile(
            ctypes.byref(handle),
            desired_access,
            ctypes.byref(object_attributes),
            ctypes.byref(io_status),
            None,
            file_attributes,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE,
            disposition,
            create_options,
            None,
            0,
        )
    )
    if status < 0:
        if not directory and create and status == _STATUS_OBJECT_NAME_COLLISION:
            raise FileExistsError(str(path))
        raise ValueError(f"无法安全打开路径：{path}") from _nt_error(path, status)
    if not handle.value:
        raise ValueError(f"安全句柄为空，拒绝继续：{path}")
    return int(handle.value), int(io_status.information)


def _open_windows_relative_directory(
    parent_handle: int, path: Path, name: str, *, create: bool
) -> tuple[SecureDirectory, bool]:
    handle, information = _nt_create_relative(
        parent_handle, name, path=path, directory=True, create=create
    )
    try:
        info = _windows_handle_info(handle, path)
        if not info.attributes & _FILE_ATTRIBUTE_DIRECTORY:
            raise NotADirectoryError(str(path))
        if info.attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError(f"目录是 reparse point，拒绝继续：{path}")
        _windows_final_path(handle, path)
        return SecureDirectory(path, handle=handle), information == _FILE_CREATED
    except BaseException:
        _close_windows_handle(handle)
        raise


def _open_windows_relative_file_handle(
    parent_handle: int, path: Path, name: str, *, create: bool
) -> int:
    handle, _information = _nt_create_relative(
        parent_handle, name, path=path, directory=False, create=create
    )
    try:
        info = _windows_handle_info(handle, path)
        if info.attributes & (_FILE_ATTRIBUTE_DIRECTORY | _FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError(f"目标不是安全的普通文件：{path}")
        _windows_final_path(handle, path)
        return handle
    except BaseException:
        _close_windows_handle(handle)
        raise


def _handle_to_binary(handle: int, *, writable: bool) -> BinaryIO:
    import msvcrt

    flags = (os.O_RDWR if writable else os.O_RDONLY) | getattr(os, "O_BINARY", 0)
    fd: int | None = None
    try:
        fd = msvcrt.open_osfhandle(handle, flags)
        return os.fdopen(fd, "w+b" if writable else "rb")
    except BaseException:
        # open_osfhandle transfers ownership only after returning an fd. Avoid
        # double-closing a native handle that has already become fd-owned.
        if fd is None:
            _close_windows_handle(handle)
        else:
            os.close(fd)
        raise


def _create_windows_file(parent_handle: int, path: Path, name: str) -> BinaryIO:
    return _handle_to_binary(
        _open_windows_relative_file_handle(parent_handle, path, name, create=True),
        writable=True,
    )


def _open_windows_file(parent_handle: int, path: Path, name: str) -> BinaryIO:
    return _handle_to_binary(
        _open_windows_relative_file_handle(parent_handle, path, name, create=False),
        writable=False,
    )


def _close_windows_handle(handle: int) -> None:
    if os.name == "nt":
        _kernel32.CloseHandle(handle)
