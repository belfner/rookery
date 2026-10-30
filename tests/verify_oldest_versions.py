"""Find the oldest release each program's install recipe handles and record it.

Run via `make verify-oldest-versions` after changing a recipe. For every program that
installs exact versions from GitHub releases, on each architecture, a binary search over its releases installs
candidates into throwaway roots to find the oldest one that installs. Installs run in a
subprocess under ROOKERY_ARCH, so one host checks both architectures: an install
downloads and unpacks a release, leaving the program itself unrun. Releases whose assets
the program's selector cannot match on an architecture are left out of its search. The
search treats a recipe as working from one release onward, the shape a packaging change
gives a program's history. Programs installed through apt need sudo and change the host,
so the search covers the programs rookery unpacks itself.

Results are written to src/rookery/oldest_versions.json, which rookery reads to keep
`install` and `versions` to releases its recipes handle.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from rookery.arch import Arch
from rookery.config import config
from rookery.deb_program import DebProgram
from rookery.fetching import GitHubFetcher
from rookery.install_resolution import install_resolution
from rookery.program import Program
from rookery.registry import (
    get_program,
    list_programs,
)
from rookery.version_sources import GitHubReleaseSource


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "src" / "rookery" / "oldest_versions.json"
PROBE_TIMEOUT = 900
# A failing install is retried, so a dropped connection or rate limit is not read as a recipe failure
PROBE_ATTEMPTS = 3
# Final lines of failures that show the recipe cannot install a release: a missing asset,
# archive member, or companion download. Any other failure, such as a timeout, rate
# limit, or server error, leaves the release undecided and ends that search.
RECIPE_FAILURES = ("ValueError:", "FileNotFoundError:", "404 Client Error")


@dataclass
class SearchResult:
    """
    Outcome of the oldest-version search for one program on one architecture.

    Attributes
    ----------
    program : str
        Program name.
    arch : Arch
        Architecture searched.
    candidates : int
        Number of releases in the search.
    oldest : str | None
        Oldest version that installed, None when the newest candidate failed or a failure
        left a release undecided.
    first_failure : str | None
        Version just older than ``oldest`` that failed, None when every candidate installed.
    error : str
        Last line of the failing install's output.
    """

    program: str
    arch: Arch
    candidates: int
    oldest: str | None
    first_failure: str | None
    error: str


def searchable(program: Program, arch: Arch) -> bool:
    """
    Report whether a program takes part in the search on an architecture.

    Parameters
    ----------
    program : Program
        Program to test.
    arch : Arch
        Architecture to test.

    Returns
    -------
    bool
        True for a program that installs exact GitHub releases, unpacks itself, and
        builds for ``arch``.
    """
    # A GitHub release listing covers every version the source resolves, so its oldest
    # installing entry bounds what install accepts.
    if not isinstance(program.version_source, GitHubReleaseSource) or not program.supports_exact_versions():
        return False
    if isinstance(program, DebProgram):
        return False
    return program.architectures is None or arch in program.architectures


async def candidate_versions(program: Program, arch: Arch) -> list[str]:
    """
    List a program's stable releases, newest first, that its recipe could install on an architecture.

    Releases of a program that selects a GitHub asset are kept when the selector matches
    one of their assets; every release of a program that downloads from elsewhere is kept.

    Parameters
    ----------
    program : Program
        Program to list releases for.
    arch : Arch
        Architecture the asset selector matches against.

    Returns
    -------
    list[str]
        Display versions, newest first.
    """
    source = program.version_source
    assert isinstance(source, GitHubReleaseSource)
    async with GitHubFetcher() as fetcher:
        releases = await fetcher.list_releases(source.github_repo)
    select_asset = getattr(program, "_select_asset", None)
    if select_asset is None:
        return [source.tag_to_version(release.tag_name) for release in releases]
    # The selectors read the architecture from config. Setting it and selecting run with no
    # await between them, so searches running concurrently cannot interleave here.
    config.arch = arch
    return [source.tag_to_version(release.tag_name) for release in releases if select_asset(release.assets) is not None]


async def installs(name: str, version: str, arch: Arch, semaphore: asyncio.Semaphore) -> tuple[bool, str]:
    """
    Install one version of a program, retrying a failure up to ``PROBE_ATTEMPTS`` times in all.

    Parameters
    ----------
    name : str
        Program name.
    version : str
        Display version to install.
    arch : Arch
        Architecture the install targets.
    semaphore : asyncio.Semaphore
        Bounds how many installs run at once.

    Returns
    -------
    tuple[bool, str]
        Whether an attempt succeeded, and the last line of the final attempt's output.
    """
    ok, error = False, ""
    for _ in range(PROBE_ATTEMPTS):
        ok, error = await _install_once(name, version, arch, semaphore)
        if ok:
            break
    print(f"  {name} {version} on {arch}: {'ok' if ok else 'failed'}", flush=True)
    return ok, error


async def _install_once(name: str, version: str, arch: Arch, semaphore: asyncio.Semaphore) -> tuple[bool, str]:
    """
    Install one version of a program into a throwaway root in a subprocess.

    Parameters
    ----------
    name : str
        Program name.
    version : str
        Display version to install.
    arch : Arch
        Architecture the install targets.
    semaphore : asyncio.Semaphore
        Bounds how many installs run at once.

    Returns
    -------
    tuple[bool, str]
        Whether the install succeeded, and the last line of its output.
    """
    async with semaphore:
        with tempfile.TemporaryDirectory(prefix=f"rookery-oldest-{name}-") as root:
            env = dict(os.environ)
            env.update(
                ROOKERY_INSTALL_DIR=str(Path(root) / "programs"),
                ROOKERY_BIN_DIR=str(Path(root) / "bin"),
                ROOKERY_ARCH=str(arch),
            )
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "tests.verify_oldest_versions",
                "--probe",
                name,
                version,
                cwd=ROOT,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                output, _ = await asyncio.wait_for(process.communicate(), PROBE_TIMEOUT)
            except TimeoutError:
                process.kill()
                await process.wait()
                return False, f"timed out after {PROBE_TIMEOUT}s"
    lines = output.decode(errors="replace").strip().splitlines()
    return process.returncode == 0, lines[-1] if len(lines) > 0 else ""


async def search(program: Program, arch: Arch, semaphore: asyncio.Semaphore) -> SearchResult:
    """
    Binary-search a program's releases for the oldest one that installs on an architecture.

    Parameters
    ----------
    program : Program
        Program to search.
    arch : Arch
        Architecture to search.
    semaphore : asyncio.Semaphore
        Bounds how many installs run at once.

    Returns
    -------
    SearchResult
        The oldest installing version and the failure just past it.
    """
    versions = await candidate_versions(program, arch)
    if len(versions) == 0:
        return SearchResult(program.name, arch, 0, None, None, "no release matches the asset selector")

    newest_ok, newest_error = await installs(program.name, versions[0], arch, semaphore)
    if not newest_ok:
        return SearchResult(program.name, arch, len(versions), None, versions[0], newest_error)
    oldest_ok, oldest_error = await installs(program.name, versions[-1], arch, semaphore)
    if oldest_ok:
        return SearchResult(program.name, arch, len(versions), versions[-1], None, "")
    if not is_recipe_failure(oldest_error):
        return SearchResult(program.name, arch, len(versions), None, versions[-1], f"undecided: {oldest_error}")

    # versions[good] installs and versions[bad] fails; narrow until they are adjacent
    good, bad, bad_error = 0, len(versions) - 1, oldest_error
    while bad - good > 1:
        middle = (good + bad) // 2
        ok, error = await installs(program.name, versions[middle], arch, semaphore)
        if ok:
            good = middle
        elif is_recipe_failure(error):
            bad, bad_error = middle, error
        else:
            return SearchResult(program.name, arch, len(versions), None, versions[middle], f"undecided: {error}")
    return SearchResult(program.name, arch, len(versions), versions[good], versions[bad], bad_error)


def is_recipe_failure(error: str) -> bool:
    """
    Report whether a failed install shows the recipe cannot install that release.

    Parameters
    ----------
    error : str
        Final line of the failed install's output.

    Returns
    -------
    bool
        True when the line names a missing asset, archive member, or companion download.
    """
    return any(marker in error for marker in RECIPE_FAILURES)


async def probe(name: str, version: str) -> None:
    """
    Install one exact version of a program, the work of a single search step.

    The install goes through the program's own version source and install procedure,
    leaving links and the recorded oldest versions out of it.

    Parameters
    ----------
    name : str
        Program name.
    version : str
        Display version to install.
    """
    program = get_program(name)
    assert program.version_source is not None
    resolution = await program.version_source.resolve(version)
    with install_resolution(resolution):
        await program.install(resolution.version)


def merge(results: list[SearchResult]) -> dict[str, dict[str, str]]:
    """
    Fold search results into the recorded oldest versions.

    Entries for programs and architectures outside this run are kept as recorded, as is
    the entry of a search that ended without an oldest version: its newest release
    failed, or a failure left a release undecided.

    Parameters
    ----------
    results : list[SearchResult]
        Results from this run.

    Returns
    -------
    dict[str, dict[str, str]]
        Oldest version per program, per architecture.
    """
    recorded: dict[str, dict[str, str]] = json.loads(OUTPUT.read_text()) if OUTPUT.exists() else {}
    for result in results:
        if result.oldest is not None:
            recorded.setdefault(result.program, {})[result.arch] = result.oldest
    return {name: dict(sorted(per_arch.items())) for name, per_arch in sorted(recorded.items()) if len(per_arch) > 0}


async def run(names: list[str], arches: list[Arch], jobs: int) -> list[SearchResult]:
    """
    Search every selected program on every selected architecture.

    Parameters
    ----------
    names : list[str]
        Program names to search; empty searches every eligible program.
    arches : list[Arch]
        Architectures to search.
    jobs : int
        Maximum installs running at once.

    Returns
    -------
    list[SearchResult]
        One result per program and architecture searched.
    """
    programs = [get_program(name) for name in names] if len(names) > 0 else list_programs()
    semaphore = asyncio.Semaphore(jobs)
    searches = [
        search(program, arch, semaphore) for program in programs for arch in arches if searchable(program, arch)
    ]
    return list(await asyncio.gather(*searches))


def main() -> int:
    """
    Run the search, write the recorded oldest versions, and report each result.

    Returns
    -------
    int
        Process exit status: 0 when every newest release installed, 1 otherwise.
    """
    parser = argparse.ArgumentParser(description="Record the oldest release each install recipe handles.")
    parser.add_argument("--program", action="append", default=[], help="program to search; repeat for more")
    parser.add_argument("--arch", action="append", choices=list(Arch), default=[], help="architecture to search")
    parser.add_argument("--jobs", type=int, default=6, help="installs to run at once")
    parser.add_argument("--probe", nargs=2, metavar=("PROGRAM", "VERSION"), help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.probe is not None:
        asyncio.run(probe(*args.probe))
        return 0

    arches = [Arch(arch) for arch in args.arch] if len(args.arch) > 0 else list(Arch)
    results = asyncio.run(run(args.program, arches, args.jobs))

    OUTPUT.write_text(json.dumps(merge(results), indent=2) + "\n")
    print()
    for result in sorted(results, key=lambda r: (r.program, r.arch)):
        line = f"{result.program:16} {result.arch:8} releases={result.candidates:<4} oldest={result.oldest}"
        if result.first_failure is not None:
            line += f"  first failure={result.first_failure}: {result.error}"
        print(line)
    print(f"wrote {OUTPUT}")
    broken = [result for result in results if result.oldest is None]
    return 1 if len(broken) > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
