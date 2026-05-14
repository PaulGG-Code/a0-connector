from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from textwrap import dedent
from typing import Callable, Mapping
from urllib.parse import quote, urlparse
from urllib.request import Request, url2pathname, urlopen


PACKAGE_NAME = "a0"
GITHUB_REPOSITORY = "agent0ai/a0-connector"
LATEST_RELEASE_API_URL = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
RELEASE_ARCHIVE_URL_TEMPLATE = (
    f"https://github.com/{GITHUB_REPOSITORY}/archive/refs/tags/{{tag}}.zip"
)
DEFAULT_PYTHON_SPEC = "3.11"
_GITHUB_API_TIMEOUT = 10.0
_DISABLED_ENV_VALUES = frozenset({"0", "false", "no", "off", "disabled"})
_VERSION_PATTERN = re.compile(r"^v?(?P<version>\d+(?:\.\d+)*)(?:[-+].*)?$", re.IGNORECASE)


class LatestReleaseError(RuntimeError):
    """Raised when the updater cannot resolve the latest release tag."""


@dataclass(frozen=True)
class InstallProvenance:
    source_url: str | None = None
    local_path: str | None = None
    editable: bool = False

    @property
    def is_local_checkout(self) -> bool:
        return self.editable or self.local_path is not None


@dataclass(frozen=True)
class UpdateCheckResult:
    current_version: str
    latest_version: str
    latest_tag: str
    is_local_checkout: bool = False


def package_spec_for_release_tag(tag: str) -> str:
    clean_tag = tag.strip()
    if not clean_tag:
        raise LatestReleaseError("latest release response did not include a tag name")
    archive_url = RELEASE_ARCHIVE_URL_TEMPLATE.format(tag=quote(clean_tag, safe=""))
    return f"{PACKAGE_NAME} @ {archive_url}"


def fetch_latest_release_tag(
    *,
    api_url: str = LATEST_RELEASE_API_URL,
    timeout: float = _GITHUB_API_TIMEOUT,
) -> str:
    request = Request(
        api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "a0-cli-self-update",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LatestReleaseError(f"could not fetch latest GitHub release: {exc}") from exc

    tag = payload.get("tag_name") if isinstance(payload, dict) else None
    if not isinstance(tag, str) or not tag.strip():
        raise LatestReleaseError("latest GitHub release response did not include tag_name")
    return tag.strip()


def resolve_package_spec(
    env: Mapping[str, str] | None = None,
    *,
    latest_release_resolver: Callable[[], str] | None = None,
) -> str:
    source = os.environ if env is None else env
    if "A0_PACKAGE_SPEC" in source:
        return source["A0_PACKAGE_SPEC"]
    resolver = latest_release_resolver or fetch_latest_release_tag
    return package_spec_for_release_tag(resolver())


def resolve_python_spec(env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    if "A0_PYTHON_SPEC" in source:
        return source["A0_PYTHON_SPEC"]
    return DEFAULT_PYTHON_SPEC


def update_check_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    value = source.get("A0_UPDATE_CHECK", "").strip().lower()
    return value not in _DISABLED_ENV_VALUES


def normalized_release_version(value: str) -> str:
    text = value.strip()
    match = _VERSION_PATTERN.match(text)
    if match:
        return match.group("version")
    if text.lower().startswith("v"):
        return text[1:]
    return text


def is_newer_release_version(latest: str, current: str) -> bool:
    latest_key = _numeric_version_key(latest)
    current_key = _numeric_version_key(current)
    if latest_key is None or current_key is None:
        return False
    return latest_key > current_key


def check_for_update(
    current_version: str,
    env: Mapping[str, str] | None = None,
    *,
    latest_release_resolver: Callable[[], str] | None = None,
    provenance_resolver: Callable[[], InstallProvenance] | None = None,
) -> UpdateCheckResult | None:
    if not update_check_enabled(env):
        return None

    resolver = latest_release_resolver or fetch_latest_release_tag
    latest_tag = resolver()
    latest_version = normalized_release_version(latest_tag)
    if not is_newer_release_version(latest_version, current_version):
        return None

    resolve_provenance = provenance_resolver or detect_install_provenance
    provenance = resolve_provenance()
    return UpdateCheckResult(
        current_version=current_version,
        latest_version=latest_version,
        latest_tag=latest_tag,
        is_local_checkout=provenance.is_local_checkout,
    )


def format_update_available_message(result: UpdateCheckResult) -> str:
    if result.is_local_checkout:
        return (
            f"a0 CLI update available: {result.latest_version} "
            f"(current checkout reports {result.current_version}). "
            "Pull this checkout to update this runtime, or run `a0 update` for the standalone tool channel."
        )
    return (
        f"a0 CLI update available: {result.latest_version} "
        f"(installed {result.current_version}). Run `a0 update` after exiting to upgrade."
    )


def detect_install_provenance(distribution_name: str = "a0") -> InstallProvenance:
    try:
        dist = metadata.distribution(distribution_name)
    except metadata.PackageNotFoundError:
        return InstallProvenance()

    try:
        direct_url_text = dist.read_text("direct_url.json")
    except OSError:
        return InstallProvenance()

    if not direct_url_text:
        return InstallProvenance()

    try:
        payload = json.loads(direct_url_text)
    except json.JSONDecodeError:
        return InstallProvenance()

    source_url = payload.get("url") if isinstance(payload.get("url"), str) else None
    dir_info = payload.get("dir_info")
    editable = isinstance(dir_info, dict) and bool(dir_info.get("editable"))
    return InstallProvenance(
        source_url=source_url,
        local_path=_file_url_to_path(source_url) if source_url else None,
        editable=editable,
    )


def run_self_update_handoff(
    *,
    env: Mapping[str, str] | None = None,
    temp_dir: str | os.PathLike[str] | None = None,
) -> int:
    python_spec = resolve_python_spec(env)
    provenance = detect_install_provenance()
    if provenance.is_local_checkout:
        print(_format_local_checkout_notice(provenance))

    uv_executable = shutil.which("uv")
    if uv_executable is None:
        print("uv is required for `a0 update`. Install uv or rerun the existing installer.")
        return 1

    try:
        package_spec = resolve_package_spec(env)
    except LatestReleaseError as exc:
        print(f"Failed to resolve the latest a0 release: {exc}")
        print("Set A0_PACKAGE_SPEC to install from a specific package source.")
        return 1

    script_path = _write_updater_script(temp_dir=temp_dir)
    argv = [sys.executable, str(script_path), str(os.getpid()), package_spec, python_spec]
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL)
    except OSError as exc:
        _best_effort_remove(script_path)
        print(f"Failed to launch the updater handoff: {exc}")
        return 1

    print("Handing off update to a separate process. The updater will continue here after a0 exits.")
    return 0


def _build_updater_script() -> str:
    return dedent(
        """\
        import os
        from pathlib import Path
        import shutil
        import subprocess
        import sys
        import time


        def _wait_for_parent_exit(parent_pid):
            if parent_pid <= 0:
                return
            if os.name == "nt":
                import ctypes

                wait_timeout = 258
                synchronize = 0x00100000
                kernel32 = ctypes.windll.kernel32
                kernel32.OpenProcess.restype = ctypes.c_void_p
                handle = kernel32.OpenProcess(synchronize, False, parent_pid)
                if not handle:
                    return
                try:
                    while True:
                        result = kernel32.WaitForSingleObject(handle, 100)
                        if result != wait_timeout:
                            return
                finally:
                    kernel32.CloseHandle(handle)
                return

            while True:
                try:
                    os.kill(parent_pid, 0)
                except ProcessLookupError:
                    return
                except PermissionError:
                    return
                time.sleep(0.1)


        def main(argv):
            if len(argv) != 3:
                print("Invalid updater invocation.", file=sys.stderr)
                return 2

            try:
                parent_pid = int(argv[0])
            except ValueError:
                print("Invalid parent PID.", file=sys.stderr)
                return 2

            package_spec = argv[1]
            python_spec = argv[2]
            _wait_for_parent_exit(parent_pid)

            uv_executable = shutil.which("uv")
            if uv_executable is None:
                print("uv is required for `a0 update`. Install uv or rerun the existing installer.")
                return 1

            try:
                result = subprocess.run(
                    [
                        uv_executable,
                        "tool",
                        "install",
                        "--python",
                        python_spec,
                        "--managed-python",
                        "--upgrade",
                        package_spec,
                    ],
                    check=False,
                )
            except OSError as exc:
                print(f"Failed to run uv: {exc}")
                return 1

            if result.returncode == 0:
                print("Update complete. Run a0.")
            return result.returncode


        if __name__ == "__main__":
            exit_code = 1
            try:
                exit_code = main(sys.argv[1:])
            finally:
                try:
                    Path(__file__).unlink()
                except OSError:
                    pass
            raise SystemExit(exit_code)
        """
    )


def _write_updater_script(temp_dir: str | os.PathLike[str] | None = None) -> Path:
    fd, script_path = tempfile.mkstemp(
        prefix="a0-update-",
        suffix=".py",
        text=True,
        dir=temp_dir,
    )
    path = Path(script_path)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(_build_updater_script())
    return path


def _file_url_to_path(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme != "file":
        return None

    path = url2pathname(parsed.path)
    if parsed.netloc:
        return f"//{parsed.netloc}{path}"
    return path


def _numeric_version_key(value: str) -> tuple[int, ...] | None:
    version = normalized_release_version(value)
    parts = version.split(".")
    if not parts or any(not part.isdigit() for part in parts):
        return None
    numbers = tuple(int(part) for part in parts)
    return numbers + (0,) * max(0, 3 - len(numbers))


def _format_local_checkout_notice(provenance: InstallProvenance) -> str:
    location = provenance.local_path or "this checkout"
    return (
        f"Notice: current a0 runtime comes from a local or editable checkout at {location}. "
        "`a0 update` updates the standalone uv-managed tool channel and will not modify this checkout."
    )


def _best_effort_remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
