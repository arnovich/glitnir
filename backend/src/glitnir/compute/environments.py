"""Operator-registered, pinned builds with an exclusive bounded project cache."""

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
import tomllib
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from glitnir.compute.protocol import CONFIG_FILES, checker_digest, source_path
from glitnir.lean.project import read_regular
from glitnir.lean.project_inspection import TOOLCHAIN, _toolchain, lake_matches

Runner = Callable[[list[str], Path], str]
Monitor = Callable[[], None]
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_REV = re.compile(r"[0-9a-f]{40}\Z")


def _repository(value: str) -> bool:
    url = urlsplit(value)
    return (Path(value).is_absolute() and Path(value).is_dir()) or (
        url.scheme == "https"
        and bool(url.hostname)
        and not url.username
        and not url.password
        and not url.query
        and not url.fragment
    )


@dataclass(frozen=True)
class Recipe:
    """An operator-selected build; job inputs cannot change any of these fields."""

    project: str
    environment: str
    repository: str
    commit: str
    toolchain: str
    targets: tuple[str, ...]
    max_bytes: int
    mathlib_cache: bool = False

    def __post_init__(self) -> None:
        """Reject mutable revisions, other toolchains and command-line options."""
        if type(self.mathlib_cache) is not bool:
            raise ValueError("mathlib_cache must be a boolean")
        if self.toolchain != TOOLCHAIN:
            raise ValueError("Recipe toolchain must equal the supported toolchain")
        if not all(
            isinstance(v, str) and _NAME.fullmatch(v)
            for v in (self.project, self.environment)
        ):
            raise ValueError("Invalid recipe project or environment")
        if not isinstance(self.commit, str) or not _REV.fullmatch(self.commit):
            raise ValueError("Recipe needs a full Git commit")
        if not isinstance(self.repository, str) or not _repository(self.repository):
            raise ValueError(
                "Recipe needs an HTTPS repository or absolute local mirror"
            )
        if (
            not self.targets
            or len(self.targets) > 64
            or not all(
                isinstance(t, str)
                and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.:/+-]{0,255}", t)
                for t in self.targets
            )
        ):
            raise ValueError("Recipe needs explicit Lake build targets")
        if type(self.max_bytes) is not int or not 1 <= self.max_bytes <= 1024**4:
            raise ValueError("Invalid recipe disk budget")

    def key(self, checker: str) -> str:
        """Bind caches to the complete recipe and installed checker."""
        return hashlib.sha256(
            json.dumps(
                {"recipe": asdict(self), "checker": checker}, sort_keys=True
            ).encode()
        ).hexdigest()


def load_registry(path: Path) -> list[Recipe]:
    """Read bounded recipes; reject duplicate project/environment pairs."""
    if path.stat().st_size > 1000000:
        raise ValueError("Registry exceeds size limit")
    try:
        value = tomllib.loads(path.read_text())
        if (
            set(value) != {"environments"}
            or not isinstance(value["environments"], list)
            or not 1 <= len(value["environments"]) <= 64
        ):
            raise ValueError("Registry needs 1–64 environments")
        if any(
            not isinstance(entry, dict) or not isinstance(entry.get("targets"), list)
            for entry in value["environments"]
        ):
            raise ValueError("Recipe targets must be an array")
        recipes = [
            Recipe(**{**entry, "targets": tuple(entry["targets"])})
            for entry in value["environments"]
        ]
    except (KeyError, TypeError) as error:
        raise ValueError("Invalid environment recipe") from error
    if len({(r.project, r.environment) for r in recipes}) != len(recipes):
        raise ValueError("Duplicate registered environment")
    return recipes


def _files(root: Path) -> Iterator[Path]:
    count = 0
    for parent, directories, files in os.walk(root, followlinks=False):
        for name in sorted([*directories, *files]):
            count += 1
            if count > 200000:
                raise ValueError("Environment exceeds traversal budget")
            path = Path(parent) / name
            mode = path.lstat().st_mode
            if stat.S_ISREG(mode) or stat.S_ISLNK(mode):
                yield path
            elif not stat.S_ISDIR(mode):
                raise ValueError("Environment contains a non-regular file")


def _materialize_links(root: Path, monitor: Monitor = lambda: None) -> None:
    """Copy contained artifact links; source, configuration and directory links fail."""
    for path in _files(root):
        monitor()
        if not path.is_symlink():
            continue
        try:
            target = path.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ValueError("Invalid environment link") from error
        if (
            not target.is_relative_to(root.resolve())
            or not target.is_file()
            or any(
                p.suffix == ".lean" or p.name in CONFIG_FILES for p in (path, target)
            )
        ):
            raise ValueError(
                "Environment link crosses a source or containment boundary"
            )
        # Copy before replacing, without ever writing through a link.
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            with target.open("rb") as source:
                while data := source.read(1024 * 1024):
                    stream.write(data)
                    stream.flush()
                    monitor()
        shutil.copymode(target, temporary)
        os.replace(temporary, path)


def _inventory(root: Path, monitor: Monitor) -> dict[str, str]:
    found = {}
    for path in _files(root):
        monitor()
        if path.is_symlink():
            raise ValueError("Cached environment integrity: unexpected link")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while data := stream.read(1024 * 1024):
                digest.update(data)
                monitor()
        found[path.relative_to(root).as_posix()] = digest.hexdigest()
    return found


def _locked_dependencies(root: Path) -> list[dict[str, str]]:
    manifest = root / "lake-manifest.json"
    if not manifest.is_file() or manifest.is_symlink():
        raise ValueError("Recipe needs a committed Lake dependency manifest")
    value = json.loads(manifest.read_text())
    packages = value.get("packages")
    if (
        not isinstance(packages, list)
        or len(packages) > 256
        or value.get("packagesDir", ".lake/packages") != ".lake/packages"
    ):
        raise ValueError("Invalid locked dependency manifest")
    names = set()
    for entry in packages:
        name = entry.get("name", "")
        if (
            not isinstance(name, str)
            or not _NAME.fullmatch(name)
            or name in names
            or entry.get("type") != "git"
            or not _REV.fullmatch(entry.get("rev", ""))
            or not _repository(entry.get("url", ""))
            or entry.get("dir")
            or entry.get("subDir")
            or entry.get("configFile", "lakefile.lean")
            not in {"lakefile.lean", "lakefile.toml"}
            or entry.get("manifestFile", "lake-manifest.json") != "lake-manifest.json"
        ):
            raise ValueError(
                "Dependencies must be locked Git repositories without path overrides"
            )
        names.add(name)
    return packages


def _verify_git(root: Path, revision: str, run: Runner) -> None:
    if (
        run(["git", "rev-parse", "HEAD"], root).strip() != revision
        or run(["git", "status", "--porcelain", "--untracked-files=no"], root).strip()
    ):
        raise ValueError("Prepared Git revision or tracked files changed")


def _recover_stage(entry: Path) -> None:
    """Discard a copy or candidate journal interrupted before its ready boundary."""
    stage = entry / "stage"
    if not stage.exists():
        return
    try:
        if not (stage / "ready").is_file():
            raise ValueError("Interrupted candidate copy")
        marker = stage / "candidate.json"
        if marker.exists():
            source_path(json.loads(read_regular(marker, 1024))["path"])
    except (OSError, ValueError, KeyError, TypeError):
        shutil.rmtree(stage)


class EnvironmentCache:
    """Lock through preparation and checking; publish only complete builds."""

    def __init__(
        self, root: Path, *, max_entries: int = 4, max_bytes: int = 64 * 1024**3
    ) -> None:
        """Use a private owned directory and explicit aggregate disk/count ceilings."""
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        mode = root.lstat()
        if root.is_symlink() or mode.st_uid != os.getuid() or mode.st_mode & 0o077:
            raise ValueError("Cache must be an owned private directory (chmod 700)")
        if not 1 <= max_entries <= 64 or max_bytes < 1:
            raise ValueError("Invalid environment cache budget")
        self.root, self.max_entries, self.max_bytes = (
            root.resolve(),
            max_entries,
            max_bytes,
        )
        self.checker = checker_digest()
        self.active_entry: Path | None = None
        self.lock_fd: int | None = None

    def ready(self, recipes: Sequence[Recipe]) -> list[str]:
        """Return cache hints; verify integrity under the lease lock."""
        return [
            r.environment
            for r in recipes
            if (self.root / r.key(self.checker) / "ready.json").is_file()
        ]

    def check_budget(self, entry: Path, recipe: Recipe) -> None:
        """Count entry data, including Git objects and private candidate stages."""
        if sum(p.lstat().st_size for p in _files(entry)) > recipe.max_bytes:
            raise ValueError("Environment exceeds its disk budget")

    @contextmanager
    def prepare(self, recipe: Recipe, run: Runner, monitor: Monitor) -> Iterator[Path]:
        """Prepare or reuse a recipe, holding the lock until checking ends."""
        if recipe.max_bytes > self.max_bytes:
            raise ValueError("Recipe reservation exceeds cache disk budget")
        with (self.root / ".lock").open("a+b") as lock:
            while True:
                monitor()
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.lock_fd = lock.fileno()
                    break
                except BlockingIOError:
                    time.sleep(0.1)
            for abandoned in self.root.glob(".building-*"):
                shutil.rmtree(abandoned)
            entry = self.root / recipe.key(self.checker)
            try:
                self.active_entry = entry
                entries = sorted(
                    (p for p in self.root.iterdir() if p.is_dir() and p != entry),
                    key=lambda p: p.stat().st_mtime,
                )
                # Reserve the full recipe ceiling, including future stage growth.
                while entries and (
                    len(entries) + 1 > self.max_entries
                    or sum(
                        json.loads((e / "ready.json").read_text())["max_bytes"]
                        for e in entries
                    )
                    + recipe.max_bytes
                    > self.max_bytes
                ):
                    shutil.rmtree(entries.pop(0))
                if entry.exists():
                    ready = json.loads((entry / "ready.json").read_text())
                    if ready.get("max_bytes") != recipe.max_bytes or ready.get(
                        "files"
                    ) != _inventory(entry / "project", monitor):
                        raise ValueError("Cached environment integrity check failed")
                else:
                    temporary = Path(
                        tempfile.mkdtemp(prefix=".building-", dir=self.root)
                    )
                    try:
                        self.active_entry = temporary
                        self._build(temporary, recipe, run, monitor)
                        self.check_budget(temporary, recipe)
                        os.replace(temporary, entry)
                        self.active_entry = entry
                    finally:
                        if temporary.exists():
                            shutil.rmtree(temporary)
                self.check_budget(entry, recipe)
                os.utime(entry, None)
                _recover_stage(entry)
                yield entry
                self.check_budget(entry, recipe)
            except BaseException:
                if entry.exists():
                    shutil.rmtree(entry)
                raise
            finally:
                self.active_entry = None
                self.lock_fd = None

    def _build(
        self, entry: Path, recipe: Recipe, run: Runner, monitor: Monitor
    ) -> None:
        project = entry / "project"
        project.mkdir()
        toolchain = _toolchain()
        if not lake_matches(run([str(toolchain / "bin/lake"), "--version"], project)):
            raise ValueError("Installed Lake does not match supported toolchain")
        run(["git", "init", "-q"], project)
        run(
            [
                "git",
                "-c",
                "protocol.file.allow=always",
                "fetch",
                "--no-tags",
                "--depth=1",
                recipe.repository,
                recipe.commit,
            ],
            project,
        )
        run(["git", "checkout", "-q", "--detach", "FETCH_HEAD"], project)
        if (project / "lean-toolchain").is_symlink() or (
            project / "lean-toolchain"
        ).read_text().strip() != TOOLCHAIN:
            raise ValueError("Repository toolchain differs from registered toolchain")
        dependencies = _locked_dependencies(project)
        _verify_git(project, recipe.commit, run)
        manifest = (project / "lake-manifest.json").read_bytes()
        if recipe.mathlib_cache:
            run(
                [
                    str(toolchain / "bin/lake"),
                    "--keep-toolchain",
                    "exe",
                    "cache",
                    "get",
                ],
                project,
            )
        run(
            [str(toolchain / "bin/lake"), "--keep-toolchain", "build", *recipe.targets],
            project,
        )
        monitor()
        _verify_git(project, recipe.commit, run)
        if (
            manifest != (project / "lake-manifest.json").read_bytes()
            or (project / ".lake/package-overrides.json").exists()
        ):
            raise ValueError("Build changed locked dependencies")
        for dependency in dependencies:
            _verify_git(
                project / ".lake/packages" / dependency["name"], dependency["rev"], run
            )
        _materialize_links(project, monitor)
        inventory = _inventory(project, monitor)
        (entry / "ready.json").write_text(
            json.dumps({"files": inventory, "max_bytes": recipe.max_bytes})
        )
        for path in _files(project):
            monitor()
            path.chmod(path.stat().st_mode & ~0o222)
