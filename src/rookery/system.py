"""System integration for symlinks and desktop entries."""

from __future__ import annotations

import subprocess
import tempfile
from contextlib import suppress
from pathlib import Path

from rookery.config import config
from rookery.file_io import (
    atomic_symlink,
    atomic_write_text,
    temp_name_for,
)
from rookery.path_utils import is_path_writable
from rookery.program import Program
from rookery.state import (
    LinkRecord,
    ProgramState,
    program_state_lock,
)
from rookery.sudo import SudoManager


class IntegrationPermissionError(Exception):
    """A destination needs elevation that the caller did not validate."""


def _render_desktop_entry(entry: dict[str, str]) -> str:
    """
    Render desktop entry fields into the file contents they describe.

    Writing an entry and deciding whether the installed one is still current both go
    through here, so the comparison is against the exact text a write would produce.

    Parameters
    ----------
    entry : dict[str, str]
        Desktop entry fields (Name, Exec, Icon, etc.).

    Returns
    -------
    str
        Full contents of the .desktop file, newline terminated.
    """
    lines = ["[Desktop Entry]"]
    lines.extend(f"{key}={value}" for key, value in entry.items())
    return "\n".join(lines) + "\n"


class SystemLinker:
    """
    Manages symlinks, desktop entries, and man page links.

    Integration directories are expected to be writable by the invoking user; the
    defaults under ``~/.local`` always are. Elevation is chosen per destination, so a
    sudo manager validated for some other reason, such as creating the install root,
    does not cause user-owned paths to be written as root.
    """

    def __init__(
        self,
        bin_dir: Path | None = None,
        desktop_dir: Path | None = None,
        man_dir: Path | None = None,
        sudo_manager: SudoManager | None = None,
    ) -> None:
        """
        Initialize system linker.

        Parameters
        ----------
        bin_dir : Path, optional
            System binary directory. Defaults to value from ROOKERY_BIN_DIR or /usr/local/bin.
        desktop_dir : Path, optional
            Desktop entries directory. Defaults to value from ROOKERY_DESKTOP_DIR or /usr/share/applications.
        man_dir : Path, optional
            System man pages directory. Defaults to value from ROOKERY_MAN_DIR or /usr/share/man.
        sudo_manager : SudoManager | None
            Sudo manager for privilege elevation. If None, operates without sudo.
        """
        self.bin_dir = bin_dir if bin_dir is not None else config.bin_dir
        self.desktop_dir = desktop_dir if desktop_dir is not None else config.desktop_dir
        self.man_dir = man_dir if man_dir is not None else config.man_dir
        self.sudo_manager = sudo_manager

        # Create the integration directories themselves, not their parents, and do so
        # whether or not a manager was supplied: a manager validated for the install
        # root says nothing about these paths.
        for dir_path in [self.bin_dir, self.desktop_dir, self.man_dir]:
            if not dir_path.exists() and is_path_writable(dir_path):
                with suppress(OSError):
                    dir_path.mkdir(parents=True, exist_ok=True)

    def _elevate(self, path: Path) -> SudoManager | None:
        """
        Decide whether writing an entry at a path needs the validated manager.

        Need and availability are separate questions. A destination whose containing
        directory the user can write is never elevated, even when a manager exists for
        an unrelated reason. A destination that does need elevation without a validated
        manager is a disagreement between the privilege plan and this linker, and is
        raised rather than attempted.

        Parameters
        ----------
        path : Path
            Entry being created, replaced, or removed.

        Returns
        -------
        SudoManager | None
            The manager when the operation must be elevated, None otherwise.

        Raises
        ------
        IntegrationPermissionError
            The destination requires elevation and no manager was validated.
        """
        if is_path_writable(path.parent):
            return None
        if self.sudo_manager is None:
            raise IntegrationPermissionError(
                f"{path.parent} is not writable by you. Set the matching ROOKERY_*_DIR "
                "to a directory you own, or pass --no-links to skip system integration."
            )
        return self.sudo_manager

    def _place_as_root(self, manager: SudoManager, source: Path, destination: Path, mode: str) -> None:
        """
        Install a staged file at an elevated destination, replacing it atomically.

        The file is copied into the destination's own directory under a temporary name,
        which is where a rename onto the destination is atomic, and the directory is
        created along the way. A copy that lands but fails to be renamed is cleaned up.

        Parameters
        ----------
        manager : SudoManager
            Validated sudo manager for the destination.
        source : Path
            Staged file holding the contents to install.
        destination : Path
            Final path of the entry.
        mode : str
            Permission bits for the installed file, in the octal form `install` takes.
        """
        staged = temp_name_for(destination)
        try:
            manager.run_as_root(["install", "-D", "-m", mode, "--", str(source), str(staged)])
            manager.run_as_root(["mv", "-fT", "--", str(staged), str(destination)])
        except BaseException:
            with suppress(Exception):
                manager.run_as_root(["rm", "-f", "--", str(staged)])
            raise

    def _link_as_root(self, manager: SudoManager, target: Path, link_path: Path) -> None:
        """
        Point an elevated symlink at a target, replacing it atomically.

        The link is created under a temporary name in its own directory and renamed onto
        the final name, so a caller resolving the link during a relink finds either the
        old target or the new one. The directory is created along the way.

        Parameters
        ----------
        manager : SudoManager
            Validated sudo manager for the destination.
        target : Path
            Path the symlink points at.
        link_path : Path
            Path of the symlink to create.
        """
        manager.run_as_root(["mkdir", "-p", "--", str(link_path.parent)])
        staged = temp_name_for(link_path)
        try:
            manager.run_as_root(["ln", "-s", "--", str(target), str(staged)])
            manager.run_as_root(["mv", "-fT", "--", str(staged), str(link_path)])
        except BaseException:
            with suppress(Exception):
                manager.run_as_root(["rm", "-f", "--", str(staged)])
            raise

    def create_binary_symlink(self, target: Path, name: str | None = None) -> None:
        """
        Create symlink in system binary directory.

        Parameters
        ----------
        target : Path
            Target binary path.
        name : str | None
            Symlink name, defaults to target filename.
        """
        if name is None:
            name = target.name

        link_path = self.bin_dir / name

        manager = self._elevate(link_path)
        if manager is not None:
            self._link_as_root(manager, target, link_path)
        else:
            atomic_symlink(link_path, target)

    def create_desktop_entry(
        self,
        name: str,
        entry: dict[str, str],
    ) -> None:
        """
        Create desktop entry file.

        Parameters
        ----------
        name : str
            Desktop entry filename (without .desktop extension).
        entry : dict[str, str]
            Desktop entry fields (Name, Exec, Icon, etc.).
        """
        desktop_file = self.desktop_dir / f"{name}.desktop"
        content_str = _render_desktop_entry(entry)

        manager = self._elevate(desktop_file)
        if manager is not None:
            with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".desktop") as tmp:
                tmp.write(content_str)
                tmp_path = tmp.name

            try:
                self._place_as_root(manager, Path(tmp_path), desktop_file, "644")
            finally:
                Path(tmp_path).unlink(missing_ok=True)
        else:
            atomic_write_text(desktop_file, content_str, mode=0o644)

    def desktop_entry_is_current(self, name: str, entry: dict[str, str]) -> bool:
        """
        Report whether the installed desktop entry already holds the given fields.

        A program's entry is generated from paths and metadata that move between
        versions, so an entry that merely exists can still name an icon or executable the
        current install no longer provides. An entry that cannot be read is reported as
        out of date, which has the caller rewrite it.

        Parameters
        ----------
        name : str
            Desktop entry filename (without .desktop extension).
        entry : dict[str, str]
            Desktop entry fields the file should hold.

        Returns
        -------
        bool
            True when the file on disk matches what these fields render to.
        """
        desktop_file = self.desktop_dir / f"{name}.desktop"
        try:
            return desktop_file.read_bytes() == _render_desktop_entry(entry).encode()
        except OSError:
            return False

    def update_desktop_database(self) -> None:
        """
        Update desktop database after creating entries.

        Runs update-desktop-database without sudo. For user-local desktop entries, this works fine.
        For system desktop entries (via env var override), this will fail silently.
        For .deb programs, apt/dpkg handles desktop database updates automatically.
        """
        with suppress(FileNotFoundError, subprocess.CalledProcessError):
            subprocess.run(
                ["update-desktop-database", str(self.desktop_dir)],
                check=False,
                capture_output=True,
            )

    def create_man_symlink(self, target: Path, section: str) -> None:
        """
        Create symlink in system man directory.

        Parameters
        ----------
        target : Path
            Target man page file path.
        section : str
            Man section (e.g., "man1", "man8"). May include compound key
            (e.g., "man1:script.1") for multiple pages in same section.
        """
        # Extract actual section from compound key if present
        actual_section = section.split(":")[0] if ":" in section else section
        section_dir = self.man_dir / actual_section
        link_path = section_dir / target.name

        manager = self._elevate(link_path)
        if manager is not None:
            self._link_as_root(manager, target, link_path)
        else:
            atomic_symlink(link_path, target)

    def remove_man_symlink(self, name: str, section: str) -> bool:
        """
        Remove symlink from system man directory.

        Parameters
        ----------
        name : str
            Man page filename.
        section : str
            Man section (e.g., "man1", "man8"). May include compound key
            (e.g., "man1:script.1") for multiple pages in same section.

        Returns
        -------
        bool
            True if symlink was removed, False if it didn't exist.
        """
        # Extract actual section from compound key if present
        actual_section = section.split(":")[0] if ":" in section else section
        link_path = self.man_dir / actual_section / name

        if not link_path.is_symlink():
            return False

        manager = self._elevate(link_path)
        if manager is not None:
            try:
                manager.run_as_root(["rm", "-f", str(link_path)])
                return True
            except subprocess.CalledProcessError:
                return False
        else:
            link_path.unlink()
            return True

    def update_man_database(self) -> None:
        """
        Update man page database after creating/removing man pages.

        Runs mandb without sudo. For user-local man pages, this works fine.
        For system man pages (via env var override), this will fail silently.
        For .deb programs, apt/dpkg handles man page database updates automatically.
        """
        with suppress(FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            subprocess.run(
                ["mandb"],
                check=False,
                capture_output=True,
                timeout=30,
            )

    def remove_binary_symlink(self, name: str) -> bool:
        """
        Remove symlink from system binary directory.

        Parameters
        ----------
        name : str
            Name of symlink to remove.

        Returns
        -------
        bool
            True if symlink was removed, False if it didn't exist.
        """
        link_path = self.bin_dir / name

        if not (link_path.is_symlink() or link_path.exists()):
            return False

        manager = self._elevate(link_path)
        if manager is not None:
            try:
                manager.run_as_root(["rm", "-f", str(link_path)])
                return True
            except subprocess.CalledProcessError:
                return False
        else:
            link_path.unlink()
            return True

    def remove_desktop_entry(self, name: str) -> bool:
        """
        Remove desktop entry file.

        Parameters
        ----------
        name : str
            Desktop entry filename (without .desktop extension).

        Returns
        -------
        bool
            True if desktop entry was removed, False if it didn't exist.
        """
        desktop_file = self.desktop_dir / f"{name}.desktop"

        if not desktop_file.exists():
            return False

        manager = self._elevate(desktop_file)
        if manager is not None:
            try:
                manager.run_as_root(["rm", "-f", str(desktop_file)])
                return True
            except subprocess.CalledProcessError:
                return False
        else:
            desktop_file.unlink()
            return True

    def get_existing_links(self, program: Program) -> dict[str, list[Path]]:
        """
        Find existing symlinks, desktop entries, and man pages for program.

        Parameters
        ----------
        program : Program
            Program to check.

        Returns
        -------
        dict[str, list[Path]]
            Dictionary with "symlinks", "desktop", and "man" keys containing lists of existing paths.
        """
        existing: dict[str, list[Path]] = {"symlinks": [], "desktop": [], "man": []}

        # Check for symlinks
        try:
            for binary_path in program.get_binary_paths():
                link_path = self.bin_dir / binary_path.name
                if link_path.is_symlink() or link_path.exists():
                    existing["symlinks"].append(link_path)
        except FileNotFoundError:
            # Program not fully installed, skip symlink check
            pass

        # Check for desktop entry
        desktop_file = self.desktop_dir / f"{program.name}.desktop"
        if desktop_file.exists():
            existing["desktop"].append(desktop_file)

        # Check for man pages
        try:
            man_pages = program.get_man_pages()
            for section, man_page in man_pages.items():
                # Extract actual section from compound key if present
                actual_section = section.split(":")[0] if ":" in section else section
                link_path = self.man_dir / actual_section / man_page.name
                if link_path.is_symlink() or link_path.exists():
                    existing["man"].append(link_path)
        except FileNotFoundError:
            # Program not fully installed, skip man page check
            pass

        return existing

    def manifest_links(self, program: Program) -> list[LinkRecord] | None:
        """
        Return the links a program's current manifest names, with their targets.

        Parameters
        ----------
        program : Program
            Program to read binary and man page paths from.

        Returns
        -------
        list[LinkRecord] | None
            Links under bin_dir and man_dir, in manifest order. None when the manifest
            cannot be read in full, which a partially installed program answers with,
            so a caller can tell an empty manifest from an unreadable one.
        """
        try:
            binary_paths = program.get_binary_paths()
            man_pages = program.get_man_pages()
        except FileNotFoundError:
            return None

        records = [LinkRecord(str(self.bin_dir / path.name), str(path)) for path in binary_paths]

        for section, man_page in man_pages.items():
            actual_section = section.split(":")[0] if ":" in section else section
            records.append(LinkRecord(str(self.man_dir / actual_section / man_page.name), str(man_page)))

        return records

    def _is_recorded_link(self, record: LinkRecord) -> bool:
        """
        Report whether a recorded path still holds the link that was recorded.

        The target is read as written rather than resolved, so a link left dangling by a
        payload that dropped its target still matches the record that created it.

        Parameters
        ----------
        record : LinkRecord
            Recorded link to check.

        Returns
        -------
        bool
            True when the path is a symlink pointing at the recorded target.
        """
        link_path = Path(record.path)
        if not link_path.is_symlink():
            return False
        return str(link_path.readlink()) == record.target

    def _manages_link_path(self, link_path: Path) -> bool:
        """
        Report whether a link path lies in a directory this linker writes to.

        A record written under a different ROOKERY_BIN_DIR or ROOKERY_MAN_DIR falls
        outside the configured directories, and is left to whoever configured them.

        Parameters
        ----------
        link_path : Path
            Path of the link.

        Returns
        -------
        bool
            True when the path sits directly under bin_dir or a man_dir section.
        """
        return link_path.parent == self.bin_dir or link_path.parent.parent == self.man_dir

    def _remove_recorded_link(self, record: LinkRecord) -> bool:
        """
        Remove one recorded link, dispatching on the directory holding it.

        Parameters
        ----------
        record : LinkRecord
            Recorded link to remove.

        Returns
        -------
        bool
            True when the link was removed.
        """
        link_path = Path(record.path)
        if link_path.parent == self.bin_dir:
            return self.remove_binary_symlink(link_path.name)
        if link_path.parent.parent == self.man_dir:
            return self.remove_man_symlink(link_path.name, link_path.parent.name)
        return False

    def _stale_records(self, recorded: list[LinkRecord], manifest: list[LinkRecord]) -> list[LinkRecord]:
        """
        Select the recorded links a manifest no longer names.

        A record is stale only while the path still holds the exact link that was
        recorded, so an alias someone else made, a path taken over by another program,
        and a path now holding a regular file are all excluded.

        Parameters
        ----------
        recorded : list[LinkRecord]
            Records read from the program's state.
        manifest : list[LinkRecord]
            Links the program's current manifest names.

        Returns
        -------
        list[LinkRecord]
            Records safe to remove.
        """
        current = {record.path for record in manifest}
        return [record for record in recorded if record.path not in current and self._is_recorded_link(record)]

    def stale_recorded_links(self, program: Program) -> list[LinkRecord]:
        """
        Return recorded links the program's current manifest no longer names.

        A manifest that cannot be read yields nothing, since it cannot say which of the
        records it still names.

        Parameters
        ----------
        program : Program
            Program whose recorded links should be compared against its manifest.

        Returns
        -------
        list[LinkRecord]
            Records safe to remove.
        """
        manifest = self.manifest_links(program)
        if manifest is None:
            return []
        return self._stale_records(program.read_state().links, manifest)

    def sync_links(self, program: Program) -> list[Path]:
        """
        Remove the links a program's manifest dropped, then record what it now holds.

        A payload that renames or drops a command leaves a link the manifest no longer
        describes, putting it beyond the reach of both setup and removal. A record whose
        removal did not take is kept, so a later run tries again instead of losing track
        of it, and one whose path lies outside the configured integration directories is
        kept untouched, since this linker does not write there and restoring those
        directories is what reaches it. A manifest that cannot be
        read leaves the records exactly as they are.

        Parameters
        ----------
        program : Program
            Program whose links should be swept and recorded.

        Returns
        -------
        list[Path]
            Link paths that were removed.
        """
        manifest = self.manifest_links(program)
        if manifest is None:
            return []

        removed: list[Path] = []

        def sweep(state: ProgramState) -> None:
            # Reading the records, removing their links, and writing the new set all
            # happen against the state this callback was handed, inside its lock, so no
            # link is removed whose record a later write would fail to account for.
            retained: list[LinkRecord] = []
            for record in self._stale_records(state.links, manifest):
                if not self._manages_link_path(Path(record.path)):
                    # Written under different integration directories; kept so restoring
                    # them and running unlink can still find it.
                    retained.append(record)
                elif self._remove_recorded_link(record):
                    removed.append(Path(record.path))
                else:
                    retained.append(record)
            state.links = manifest + retained

        program.mutate_state(sweep)

        return removed

    def remove_program_links(self, program: Program) -> dict[str, bool]:
        """
        Remove all system links for program including man pages.

        Removes symlinks from binary directory, desktop entry, and man pages.

        Parameters
        ----------
        program : Program
            Program to remove links for.

        Returns
        -------
        dict[str, bool]
            Results dictionary with keys "symlinks", "desktop", and "man".
        """
        # Removal and the record update are one locked scope, so a link recreated by a
        # concurrent setup cannot end up live but unrecorded.
        with program_state_lock(program):
            return self._remove_links_locked(program)

    def _remove_links_locked(self, program: Program) -> dict[str, bool]:
        """
        Remove a program's links and clear their records, with its state lock held.

        Parameters
        ----------
        program : Program
            Program to remove links for.

        Returns
        -------
        dict[str, bool]
            Results dictionary with keys "symlinks", "desktop", and "man".
        """
        results = {"symlinks": False, "desktop": False, "man": False}
        cleared: set[str] = set()

        # Remove the links the current manifest names. Each is checked against the link
        # the manifest says should be there, so a path someone replaced with their own
        # script or repointed elsewhere is theirs and stays. An install predating link
        # recording has nothing in state, and is reached through here alone.
        manifest = self.manifest_links(program)
        for record in manifest if manifest is not None else []:
            if not self._is_recorded_link(record) or not self._manages_link_path(Path(record.path)):
                continue
            if not self._remove_recorded_link(record):
                continue
            results["symlinks" if Path(record.path).parent == self.bin_dir else "man"] = True
            cleared.add(record.path)

        # Remove desktop entry
        if self.remove_desktop_entry(program.name):
            results["desktop"] = True

        def finish(state: ProgramState) -> None:
            # Every recorded link goes, not only the ones the manifest no longer names:
            # this removes all of a program's links, and a manifest that cannot be read
            # (a payload renamed since install) would otherwise name none of them and
            # leave them orphaned once the state recording them is gone. Each is verified
            # against its recorded target first, so a path since repointed or replaced by
            # a regular file belongs to whoever put it there and stays.
            for record in list(state.links):
                if not self._is_recorded_link(record) or not self._manages_link_path(Path(record.path)):
                    continue
                if not self._remove_recorded_link(record):
                    continue
                results["symlinks" if Path(record.path).parent == self.bin_dir else "man"] = True
                cleared.add(record.path)
            state.links = [record for record in state.links if record.path not in cleared]

        program.mutate_state(finish)

        return results

    def setup_program(self, program: Program) -> dict[str, bool]:
        """
        Setup system integration for program including man pages.

        Creates symlinks for binaries, desktop entry, and man pages only if
        they are missing or incorrect. Idempotent - running multiple times
        only creates links that need creation.

        Parameters
        ----------
        program : Program
            Program to setup.

        Returns
        -------
        dict[str, bool]
            Results dictionary with keys "symlinks", "desktop", and "man".
            True indicates this call wrote that kind of link: a symlink or man page
            that was missing or misdirected, or a desktop entry whose contents differed
            from the fields the program declares.
        """
        results = {"symlinks": False, "desktop": False, "man": False}

        # Check if program is installed
        if not program.version_file.exists():
            return results

        # Creating the links and recording them are one locked scope, so a record never
        # outlives a concurrent unlink that removed the link it names.
        with program_state_lock(program):
            # The check above answered before the lock was held, so a concurrent
            # uninstall may have removed the program while this call waited.
            if not program.version_file.exists():
                return results
            return self._setup_locked(program, results)

    def _setup_locked(self, program: Program, results: dict[str, bool]) -> dict[str, bool]:
        """
        Create the program's links and record them, with its state lock already held.

        Parameters
        ----------
        program : Program
            Program to set up.
        results : dict[str, bool]
            Result accumulator with keys "symlinks", "desktop", and "man".

        Returns
        -------
        dict[str, bool]
            The accumulator, with True for each kind of link this call wrote.
        """
        # Check each binary symlink
        for binary_path in program.get_binary_paths():
            if not binary_path.exists():
                continue

            link_path = self.bin_dir / binary_path.name
            needs_create = False

            # Check if link is missing or incorrect
            if not link_path.exists() and not link_path.is_symlink():
                needs_create = True
            elif link_path.is_symlink():
                try:
                    if link_path.resolve() != binary_path.resolve():
                        needs_create = True  # Points to wrong location
                except (OSError, RuntimeError):
                    needs_create = True  # Broken symlink

            if needs_create:
                self.create_binary_symlink(binary_path)
                results["symlinks"] = True

        # Check desktop entry
        desktop_entry = program.get_desktop_entry()
        if desktop_entry is not None and not self.desktop_entry_is_current(program.name, desktop_entry):
            self.create_desktop_entry(program.name, desktop_entry)
            results["desktop"] = True

        # Check man page symlinks
        for section, man_page in program.get_man_pages().items():
            if not man_page.exists():
                continue

            # Extract actual section from compound key if present
            actual_section = section.split(":")[0] if ":" in section else section
            link_path = self.man_dir / actual_section / man_page.name
            needs_create = False

            if not link_path.exists() and not link_path.is_symlink():
                needs_create = True
            elif link_path.is_symlink():
                try:
                    if link_path.resolve() != man_page.resolve():
                        needs_create = True
                except (OSError, RuntimeError):
                    needs_create = True  # Broken symlink

            if needs_create:
                self.create_man_symlink(man_page, section)
                results["man"] = True

        # Sweep the links recorded by an earlier install that this manifest no longer
        # names, then record the current set for the next install to compare against.
        self.sync_links(program)

        return results
