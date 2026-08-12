#!/usr/bin/env python3
"""
ns_sandbox_v4.py — Phase 1, Milestone 3 (step 2): seccomp filtering.

Same as ns_sandbox_v3.py (namespaces + capability drop), plus a syscall
allowlist loaded via pyseccomp as the very last step before exec. Once
loaded, any syscall not on this list returns EPERM instead of running.

The allowlist below was built by tracing a real bash session (strace -c)
doing basic file/process work, then adding a small set of standard
syscalls for clean exit, signal handling, and common file operations —
verified against a live test, not guessed. Running other workloads later
(npm, pip, gcc, etc.) will likely need syscalls added here; when that
happens, trace the real command the same way rather than guessing.

Usage:
    python3 ns_sandbox_v4.py -- /bin/bash
"""

import ctypes
import ctypes.util
import os
import sys

import prctl
import pyseccomp as seccomp

libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

CLONE_NEWNS   = 0x00020000
CLONE_NEWUTS  = 0x04000000
CLONE_NEWIPC  = 0x08000000
CLONE_NEWPID  = 0x20000000
CLONE_NEWUSER = 0x10000000

MS_PRIVATE = 1 << 18
MS_REC     = 16384

ALLOWED_SYSCALLS = [
    # traced from a real bash session (strace -c)
    "wait4", "execve", "mmap", "read", "openat", "mprotect", "getdents64",
    "close", "fstat", "munmap", "statfs", "access", "brk", "ioctl", "write",
    "pread64", "getrandom", "statx", "arch_prctl", "set_tid_address",
    "set_robust_list", "prlimit64", "rseq", "lseek", "rt_sigaction",
    "rt_sigprocmask", "rt_sigreturn", "dup2", "getpid", "socket", "connect",
    "getpeername", "clone", "uname", "fcntl", "getuid", "getgid", "geteuid",
    "getegid", "getppid", "getpgrp", "newfstatat", "getgroups",
    # exit / signals
    "exit", "exit_group", "kill", "tgkill",
    # pipes / dup / polling — job control, redirection, command substitution
    "pipe", "pipe2", "dup", "dup3", "select", "pselect6", "poll", "ppoll",
    # timing
    "nanosleep", "clock_gettime", "clock_nanosleep",
    # filesystem ops common to shell/coreutils
    "getcwd", "chdir", "fchdir", "mkdir", "mkdirat", "rmdir", "rename",
    "renameat", "renameat2", "unlink", "unlinkat", "symlink", "symlinkat",
    "readlink", "readlinkat", "chmod", "fchmod", "fchmodat", "chown",
    "fchown", "fchownat", "lchown", "umask", "truncate", "ftruncate",
    "flock", "fsync", "fdatasync",
    # process/resource introspection
    "times", "getrlimit", "setrlimit", "sysinfo", "getrusage", "sigaltstack",
    "setpgid", "sched_yield", "sched_getaffinity", "futex", "capget",
    "prctl", "madvise", "mremap", "msync", "mincore",
]


def unshare(flags: int) -> None:
    if libc.unshare(flags) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))


def write_file(path: str, data: str) -> None:
    with open(path, "w") as f:
        f.write(data)


def setup_userns(uid: int, gid: int) -> None:
    write_file("/proc/self/uid_map", f"0 {uid} 1\n")
    try:
        write_file("/proc/self/setgroups", "deny")
    except OSError:
        print("[*] /proc/self/setgroups not present — continuing anyway",
              file=sys.stderr)
    write_file("/proc/self/gid_map", f"0 {gid} 1\n")


def sethostname(name: str) -> None:
    if libc.sethostname(name.encode(), len(name)) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))


def mount(source, target, fstype, flags, data=None) -> None:
    src = source.encode() if source else None
    tgt = target.encode()
    fst = fstype.encode() if fstype else None
    d = data.encode() if data else None
    if libc.mount(src, tgt, fst, ctypes.c_ulong(flags), d) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))


def drop_all_capabilities() -> None:
    prctl.capbset.limit()         # needs CAP_SETPCAP effective — do this first
    prctl.cap_effective.limit()   # effective must be ⊆ permitted, so shrink
    prctl.cap_permitted.limit()   # this before permitted, not after
    prctl.cap_inheritable.limit()


def apply_seccomp_filter() -> None:
    """Load the syscall allowlist. Must be the last thing before exec —
    once loaded, it applies to every syscall this process (and whatever
    it exec's) makes from this point on, including our own setup calls
    if any were still pending."""
    f = seccomp.SyscallFilter(defaction=seccomp.ERRNO(1))  # EPERM default
    for name in ALLOWED_SYSCALLS:
        f.add_rule(seccomp.ALLOW, name)
    f.load()


def run_sandboxed(command: list) -> None:
    uid, gid = os.getuid(), os.getgid()

    unshare(CLONE_NEWUSER)
    setup_userns(uid, gid)
    unshare(CLONE_NEWNS | CLONE_NEWUTS | CLONE_NEWIPC | CLONE_NEWPID)

    pid = os.fork()
    if pid == 0:
        sethostname("sandboxed")
        mount(None, "/", None, MS_PRIVATE | MS_REC)
        mount("proc", "/proc", "proc", 0)

        drop_all_capabilities()
        apply_seccomp_filter()   # last step — nothing outside the list runs after this

        os.execvp(command[0], command)
        os._exit(1)
    else:
        _, status = os.waitpid(pid, 0)
        sys.exit(os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1)


if __name__ == "__main__":
    if "--" not in sys.argv:
        print("Usage: python3 ns_sandbox_v4.py -- <command> [args...]")
        sys.exit(1)
    idx = sys.argv.index("--")
    run_sandboxed(sys.argv[idx + 1:])
