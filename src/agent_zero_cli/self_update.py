from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata
import json
import os
from pathlib import Path
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
