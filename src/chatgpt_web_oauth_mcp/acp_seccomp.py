"""Defense-in-depth restrictions on native helper process introspection.

This is not a network filter: the helper has its own network namespace.
The fixed WebSocket/inference channels live in acp_network, without allowing
helper-supplied destinations or exposing host network interfaces.
"""
import ctypes
import ctypes.util
import errno
import os

from .acp_session_store import AcpStateError


def helper_filter_fd(*, block_unix: bool = False) -> int:
    library = ctypes.util.find_library('seccomp')
    if not library:
        raise AcpStateError('acp_seccomp_unavailable')
    lib = ctypes.CDLL(library, use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    class ArgCompare(ctypes.Structure):
        _fields_ = [('arg', ctypes.c_uint), ('op', ctypes.c_uint),
                    ('datum_a', ctypes.c_uint64), ('datum_b', ctypes.c_uint64)]
    lib.seccomp_rule_add_array.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint,
        ctypes.POINTER(ArgCompare)]
    lib.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
    ctx = lib.seccomp_init(0x7fff0000)  # SCMP_ACT_ALLOW
    if not ctx:
        raise AcpStateError('acp_seccomp_init_failed')
    fd = -1
    try:
        for name in ('ptrace','process_vm_readv',
                     'process_vm_writev','keyctl','bpf','mount','setns','unshare',
                     'io_uring_setup','pidfd_getfd'):
            number = lib.seccomp_syscall_resolve_name(name.encode())
            if number < 0 or lib.seccomp_rule_add(ctx, 0x50000 | errno.EPERM, number, 0):
                raise AcpStateError('acp_seccomp_rule_failed')
        if block_unix:
            # AF_UNIX pathname sockets remain usable through read-only mounts.
            # Native tools therefore cannot create either kind of Unix socket;
            # only the trusted bootstrap retains the fixed broker channels.
            comparison = ArgCompare(0, 4, 1, 0)  # SCMP_CMP_EQ, AF_UNIX
            for name in ('socket', 'socketpair'):
                number = lib.seccomp_syscall_resolve_name(name.encode())
                if number < 0 or lib.seccomp_rule_add_array(
                        ctx, 0x50000 | errno.EPERM, number, 1, ctypes.byref(comparison)):
                    raise AcpStateError('acp_seccomp_rule_failed')
        fd = os.memfd_create('opss-acp-helper-policy', os.MFD_CLOEXEC)
        if lib.seccomp_export_bpf(ctx, fd):
            raise AcpStateError('acp_seccomp_export_failed')
        os.lseek(fd, 0, os.SEEK_SET)
        return fd
    except BaseException:
        if fd >= 0:
            os.close(fd)
        raise
    finally:
        lib.seccomp_release(ctx)
