"""Filesystem access diagnostics for the corpus roots.

Documents that arrive from outside the app — dropped onto a mounted volume by a
host shell, an rsync feed, another container — routinely carry ownership and
modes this container cannot read. The app runs as an unprivileged user with
every capability dropped, so a directory a feed created as ``root:root 0700`` is
not merely awkward to read: it is invisible. The ingest walk used to skip such
directories in silence, which is the worst available failure mode — a data room
of 4,000 documents ingesting as 0 files under a clean, green log.

This module answers the three questions an operator actually has:

  * what, specifically, can this process not read?
  * how much of that can the process repair by itself?
  * what is the exact command to run on the host for the rest?

The repair is narrow by construction, and deliberately so. ``chown`` needs
CAP_CHOWN and the container drops it; ``chmod`` on a path this process does not
own needs CAP_FOWNER, likewise dropped; and ``/corpus`` is mounted read-only,
where every write fails with EROFS. So :func:`repair` fixes exactly one class of
problem — a path this process owns, on a writable mount, whose own mode locks it
out — and reports everything else as a host-side command rather than pretending
to have handled it.
"""

from __future__ import annotations

import grp
import os
import pwd
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .config import SUPPORTED_EXTS, settings

# A check must stay bounded: a mis-pointed root can be the whole filesystem.
# The walk stops here and says so rather than hanging the admin panel.
MAX_ENTRIES = 200_000
# Issues are capped in the payload; the *counts* remain exact.
MAX_ISSUES = 60
# Repair re-audits after each pass, because opening a directory can reveal more
# locked paths inside it. Bounded so a pathological tree cannot spin forever.
MAX_REPAIR_PASSES = 12


def _owner(st: os.stat_result) -> str:
    try:
        user = pwd.getpwuid(st.st_uid).pw_name
    except (KeyError, OSError):
        user = str(st.st_uid)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except (KeyError, OSError):
        group = str(st.st_gid)
    return f"{user}:{group}"


def identity() -> dict:
    """Who this process is, in the terms a host-side chown/chmod needs."""
    uid, gid = os.geteuid(), os.getegid()
    try:
        user = pwd.getpwuid(uid).pw_name
    except (KeyError, OSError):
        user = str(uid)
    try:
        group = grp.getgrgid(gid).gr_name
    except (KeyError, OSError):
        group = str(gid)
    try:
        groups = sorted(os.getgroups())
    except OSError:
        groups = []
    current = os.umask(0o022)
    os.umask(current)
    return {
        "uid": uid,
        "gid": gid,
        "user": user,
        "group": group,
        "groups": groups,
        "umask": f"{current:04o}",
    }


def _mounts() -> list[tuple[str, dict]]:
    """(mount_point, info) from /proc/self/mountinfo, longest path first.

    Field 4 is the mount's ``root``: the path of the mounted directory *within
    its source filesystem*. For a Docker bind mount or named volume that is very
    close to the host path an operator needs — but it is not the same thing, and
    treating it as one is wrong whenever the source sits on a filesystem that is
    not mounted at ``/`` on the host. A bind of ``/srv/room`` where ``/srv`` is
    its own filesystem reports ``/room``.

    So it is carried as ``source_root``, paired with the device it belongs to,
    and presented as a hint to verify rather than an answer to paste blindly.
    There is no way to resolve the true host path from inside the container.
    """
    out: list[tuple[str, dict]] = []
    try:
        raw = Path("/proc/self/mountinfo").read_text()
    except OSError:
        return out
    for line in raw.splitlines():
        try:
            pre, post = line.split(" - ", 1)
            fields = pre.split()
            point = fields[4].replace("\\040", " ")
            info = {
                "mount_point": point,
                "options": fields[5],
                "read_only": "ro" in fields[5].split(","),
                "fstype": post.split()[0],
                "source": post.split()[1],
                "source_root": fields[3].replace("\\040", " "),
            }
        except (IndexError, ValueError):
            continue
        out.append((point, info))
    out.sort(key=lambda item: len(item[0]), reverse=True)
    return out


def mount_for(path: Path) -> dict:
    """The mount that actually backs `path`, with its read-only flag."""
    resolved = str(path)
    for point, info in _mounts():
        if resolved == point or resolved.startswith(point.rstrip("/") + "/"):
            return info
    return {"mount_point": "", "options": "", "read_only": False,
            "fstype": "", "source": "", "source_root": ""}


def source_path_for(path: Path) -> str:
    """Where `path` sits inside its source filesystem.

    A *candidate* host path, not a certainty — see :func:`_mounts`. Callers must
    label it as needing verification rather than presenting it as fact.
    """
    info = mount_for(path)
    point, src_root = info.get("mount_point", ""), info.get("source_root", "")
    if not point or not src_root:
        return ""
    rest = str(path)[len(point):].lstrip("/")
    return str(Path(src_root) / rest) if rest else src_root


@dataclass
class Issue:
    path: str
    kind: str      # "dir" | "file"
    problem: str
    owner: str = "?"
    mode: str = "?"
    fixable: bool = False
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "path": self.path, "kind": self.kind, "problem": self.problem,
            "owner": self.owner, "mode": self.mode, "fixable": self.fixable,
            "detail": self.detail,
        }


@dataclass
class RootReport:
    root: str
    exists: bool = False
    is_dir: bool = False
    in_browse_roots: bool = False
    read_only_mount: bool = False
    mount: dict = field(default_factory=dict)
    source_path: str = ""
    supported_files: int = 0
    other_files: int = 0
    denied_dirs: int = 0
    denied_files: int = 0
    truncated: bool = False
    issues: list[Issue] = field(default_factory=list)

    @property
    def fixable(self) -> int:
        return sum(1 for i in self.issues if i.fixable)

    @property
    def blocked(self) -> int:
        return self.denied_dirs + self.denied_files

    def as_dict(self) -> dict:
        return {
            "root": self.root,
            "exists": self.exists,
            "is_dir": self.is_dir,
            "in_browse_roots": self.in_browse_roots,
            "read_only_mount": self.read_only_mount,
            "mount": self.mount,
            "source_path": self.source_path,
            "supported_files": self.supported_files,
            "other_files": self.other_files,
            "denied_dirs": self.denied_dirs,
            "denied_files": self.denied_files,
            "blocked": self.blocked,
            "fixable": self.fixable,
            "truncated": self.truncated,
            "issues": [i.as_dict() for i in self.issues],
        }


def _can_fix(path: Path, st: os.stat_result, read_only: bool) -> tuple[bool, str]:
    """Whether *this* process could chmod `path` into readability itself."""
    if read_only:
        return False, "mount is read-only — chmod would fail with EROFS"
    if st.st_uid != os.geteuid():
        return False, f"owned by {_owner(st)}, not by this process (chown needs CAP_CHOWN)"
    return True, ""


def _issue_for(path: Path, kind: str, read_only: bool) -> Issue:
    try:
        st = path.stat()
    except OSError as exc:
        return Issue(str(path), kind, "cannot stat", detail=f"{type(exc).__name__}: {exc}")
    fixable, why = _can_fix(path, st, read_only)
    problem = "not searchable (missing +x)" if kind == "dir" else "not readable"
    return Issue(
        path=str(path),
        kind=kind,
        problem=problem,
        owner=_owner(st),
        mode=f"{stat.S_IMODE(st.st_mode):04o}",
        fixable=fixable,
        detail=why,
    )


def audit_root(root: Path, skip_dirs: set[str] | None = None) -> RootReport:
    """Walk `root` and report every path this process cannot read.

    Uses ``os.walk`` with an ``onerror`` hook rather than ``rglob``: pathlib
    swallows PermissionError while walking, which is precisely the silence this
    module exists to end.
    """
    from .ingest import SKIP_DIRS  # local import: ingest imports config, not us

    skip = SKIP_DIRS if skip_dirs is None else skip_dirs
    report = RootReport(root=str(root))
    roots = settings.browse_roots
    report.in_browse_roots = any(
        root == r or str(root).startswith(str(r).rstrip("/") + "/") for r in roots
    )
    report.exists = root.exists()
    report.is_dir = root.is_dir()
    report.mount = mount_for(root)
    report.read_only_mount = bool(report.mount.get("read_only"))
    report.source_path = source_path_for(root)
    if not report.exists:
        report.issues.append(Issue(str(root), "dir", "does not exist"))
        return report
    if not report.is_dir:
        report.issues.append(Issue(str(root), "dir", "not a directory"))
        return report
    if not os.access(root, os.R_OK | os.X_OK, effective_ids=True):
        report.denied_dirs += 1
        report.issues.append(_issue_for(root, "dir", report.read_only_mount))
        return report

    seen = 0

    def on_error(exc: OSError) -> None:
        target = Path(getattr(exc, "filename", None) or root)
        report.denied_dirs += 1
        if len(report.issues) < MAX_ISSUES:
            report.issues.append(_issue_for(target, "dir", report.read_only_mount))

    for dirpath, dirnames, filenames in os.walk(root, onerror=on_error):
        here = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if d not in skip and not d.startswith("~$"))
        for name in sorted(filenames):
            seen += 1
            if seen > MAX_ENTRIES:
                report.truncated = True
                return report
            child = here / name
            supported = child.suffix.lower() in SUPPORTED_EXTS
            if supported:
                report.supported_files += 1
            else:
                report.other_files += 1
                continue
            if os.access(child, os.R_OK, effective_ids=True):
                continue
            if not child.exists():
                # A symlink pointing nowhere reads as unreadable but is not a
                # permission the operator can grant. Naming it as one would send
                # them off to chmod a path that is not the problem.
                if len(report.issues) < MAX_ISSUES:
                    report.issues.append(Issue(str(child), "file", "broken symlink"))
                continue
            report.denied_files += 1
            if len(report.issues) < MAX_ISSUES:
                report.issues.append(_issue_for(child, "file", report.read_only_mount))
    return report


def _targets(paths: list[str] | None) -> list[Path]:
    """Which roots to inspect: an explicit path, else everything configured."""
    if paths:
        return [Path(p).expanduser().resolve() for p in paths if p.strip()]
    roots: list[Path] = []
    if settings.corpus_root:
        roots.append(settings.corpus_root)
    for r in settings.browse_roots:
        if r not in roots:
            roots.append(r)
    return roots


def host_commands(reports: list[RootReport], me: dict) -> list[str]:
    """Copy-pasteable host-side repairs for what the container cannot fix.

    ``a+rX`` rather than ``chown``: the capital X adds the execute bit to
    directories only, so it opens the tree for traversal without marking every
    PDF executable, and it needs no assumption about which uid the operator
    wants the files to end up owned by. For a volume the app must also write to,
    chown to the runtime uid is the better answer, so both are offered.
    """
    cmds: list[str] = []
    for rep in reports:
        if not rep.blocked:
            continue
        target = rep.source_path or rep.root
        cmds.append(f"# {rep.root} — {rep.blocked} unreadable path(s)")
        if rep.source_path and rep.source_path != rep.root:
            src = rep.mount.get("source") or "its source filesystem"
            fstype = rep.mount.get("fstype") or "?"
            cmds.append(
                f"#   mounted from {src} ({fstype}), at {rep.source_path} within it."
            )
            cmds.append(
                "#   That is the host path when that filesystem is mounted at / on the "
                "host;"
            )
            cmds.append(
                "#   otherwise prefix it with its host mount point. Check before running."
            )
        cmds.append(f'sudo chmod -R a+rX "{target}"')
        if not rep.read_only_mount:
            cmds.append(
                f'sudo chown -R {me["uid"]}:{me["gid"]} "{target}"'
                "   # only if the app must also write here"
            )
    return cmds


def check(paths: list[str] | None = None) -> dict:
    """Full access report for the corpus roots (or an explicit path)."""
    me = identity()
    reports = [audit_root(p) for p in _targets(paths)]
    blocked = sum(r.blocked for r in reports)
    return {
        "identity": me,
        "browse_roots": [str(r) for r in settings.browse_roots],
        "corpus_root": str(settings.corpus_root) if settings.corpus_root else None,
        "roots": [r.as_dict() for r in reports],
        "blocked": blocked,
        "fixable": sum(r.fixable for r in reports),
        "supported_files": sum(r.supported_files for r in reports),
        "host_commands": host_commands(reports, me),
        "ok": blocked == 0 and all(r.exists and r.is_dir for r in reports),
    }


def repair(paths: list[str] | None = None) -> dict:
    """Chmod what this process owns into readability; report the rest.

    Adds only the owner bits needed to read (and, for directories, traverse).
    It never widens access to group or other: the container is the only reader
    that matters, and quietly making a data room world-readable on a host that
    other services share is not a repair anyone asked for.
    """
    targets = _targets(paths)
    me = identity()
    fixed: list[str] = []
    failed: list[dict] = []
    attempted: set[str] = set()
    incomplete = False

    # Repeat until nothing new is repairable. One pass is not enough, for two
    # reasons that compound: opening a directory reveals its contents, which may
    # themselves be locked (a feed that ran with umask 077 produces exactly this
    # nesting), and a single audit caps its issue list at MAX_ISSUES while
    # counting every problem. A single-pass repair on a tree of app-owned 0000
    # directories fixes the top one, reports the newly exposed child as still
    # broken, and prints host commands for something it could have fixed itself.
    for _ in range(MAX_REPAIR_PASSES):
        progressed = False
        for rep in (audit_root(p) for p in targets):
            for issue in rep.issues:
                if not issue.fixable or issue.path in attempted:
                    continue
                attempted.add(issue.path)
                progressed = True
                target = Path(issue.path)
                try:
                    mode = stat.S_IMODE(target.stat().st_mode)
                    want = mode | stat.S_IRUSR | (stat.S_IXUSR if issue.kind == "dir" else 0)
                    if want != mode:
                        os.chmod(target, want)
                    fixed.append(issue.path)
                except OSError as exc:
                    failed.append({"path": issue.path, "error": f"{type(exc).__name__}: {exc}"})
        if not progressed:
            break
    else:
        # Ran out of passes with work still being found. Say so rather than
        # letting the final count imply the tree is as repaired as it can be.
        incomplete = any(r.fixable for r in (audit_root(p) for p in targets))

    after = check(paths)
    after["repaired"] = fixed
    after["repair_failed"] = failed
    after["repair_incomplete"] = incomplete
    after["unfixable"] = max(0, after["blocked"])
    return after
