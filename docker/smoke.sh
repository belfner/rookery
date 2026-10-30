#!/usr/bin/env bash
# Exercise rookery inside the test image on x86_64 or aarch64: architecture detection, installs of
# every program the architecture supports with a run of each installed command, refusal of the
# rest, the apt and sudo-created install root paths, and the bulk commands. Exits 1 when any check
# fails.
set -u

failures=0
arch=$(uname -m)
# Programs whose upstream publishes Linux builds for x86_64 alone
x86_only=(blender netron storageexplorer)

pass() { printf 'PASS  %s\n' "$1"; }
fail() {
    printf 'FAIL  %s\n' "$1"
    failures=$((failures + 1))
}
section() { printf '\n== %s\n' "$1"; }

# check NAME COMMAND...: pass when COMMAND exits 0, printing the head of its output either way
check() {
    local name=$1 output
    shift
    if output=$("$@" 2>&1); then pass "$name"; else fail "$name"; fi
    printf '%s\n' "$output" | head -n 3 | sed 's/^/      /'
}

# expect_text NAME TEXT OUTPUT: pass when OUTPUT contains TEXT
expect_text() {
    if grep -qF -- "$2" <<<"$3"; then pass "$1"; else fail "$1 (missing: $2)"; fi
}

# expect_native NAME PATH: pass when PATH, after following links, is an ELF binary for this architecture
expect_native() {
    local kind
    kind=$(file -bL "$2" 2>&1)
    if grep -qF "$elf_arch" <<<"$kind"; then pass "$1 is $arch"; else fail "$1 is $arch (file: $kind)"; fi
}

section "Host"
case "$arch" in
    x86_64) elf_arch="x86-64" other_alias=arm64 other=aarch64 ;;
    aarch64) elf_arch="ARM aarch64" other_alias=amd64 other=x86_64 ;;
    *)
        fail "uname -m reports $arch, which the smoke test does not cover"
        exit 1
        ;;
esac
pass "uname -m reports $arch"
rookery --version

section "Detection"
output=$(rookery info 2>&1)
printf '%s\n' "$output"
expect_text "info detects $arch" "Architecture: $arch (detected)" "$output"
expect_text "ROOKERY_ARCH=$other_alias overrides detection" "Architecture: $other (ROOKERY_ARCH)" \
    "$(ROOKERY_ARCH=$other_alias rookery info 2>&1)"
expect_text "invalid ROOKERY_ARCH is a configuration error" "ROOKERY_ARCH must be one of" \
    "$(ROOKERY_ARCH=armv7 rookery list 2>&1)"

section "Catalog"
output=$(rookery list --all 2>&1)
printf '%s\n' "$output"
if [ "$arch" = aarch64 ]; then
    for program in "${x86_only[@]}"; do
        expect_text "list explains $program" "Unavailable: $program has no build for aarch64" "$output"
    done
elif grep -qF "Unavailable:" <<<"$output"; then
    fail "list shows every program available on x86_64"
else
    pass "list shows every program available on x86_64"
fi

section "x86_64-only programs"
if [ "$arch" = aarch64 ]; then
    for program in "${x86_only[@]}"; do
        output=$(rookery install "$program" 2>&1)
        status=$?
        if [ "$status" -eq 1 ] && grep -qF "has no build for aarch64" <<<"$output"; then
            pass "install $program refused"
        else
            fail "install $program refused (exit $status: $output)"
        fi
    done
else
    check "install blender" rookery install blender
    expect_native "blender" ~/.local/share/rookery-programs/blender/blender/blender
    check "install storageexplorer" rookery install storageexplorer
    # StorageExplorer is upstream's shell launcher for the StorageExplorerExe binary
    expect_native "storageexplorer" ~/.local/share/rookery-programs/storageexplorer/StorageExplorer/StorageExplorerExe
    # apt asks before pulling in netron's dependencies; the answer arrives on stdin
    check "install netron through sudo apt" bash -c "printf 'y\n' | rookery install netron"
    check "dpkg records netron" dpkg -s netron
    expect_native "netron" "$(command -v netron)"
    # uninstall offers to autoremove the dependencies apt pulled in; the answer arrives on stdin
    check "uninstall netron through sudo apt" bash -c "printf 'y\n' | rookery uninstall netron"
    # apt remove keeps the package's config files, so dpkg still lists netron as deinstalled
    netron_state=$(dpkg-query -W -f='${db:Status-Status}' netron 2>/dev/null)
    if [ "$netron_state" = installed ]; then fail "apt removed netron"; else pass "apt removed netron"; fi
fi

section "Per-architecture builds"
# program and the command that proves its binary runs
runs=(
    "bat|bat --version"
    "dust|dust --version"
    "dysk|dysk --version"
    "eza|eza --version"
    "fastfetch|fastfetch --version"
    "fzf|fzf --version"
    "gdu|gdu --version"
    "gping|gping --version"
    "helm|helm version --short"
    "hyperfine|hyperfine --version"
    "just|just --version"
    "mc|mc --version"
    "nvim|nvim --version"
    "yazi|yazi --version"
)
for entry in "${runs[@]}"; do
    program=${entry%%|*}
    read -ra command <<<"${entry#*|}"
    check "install $program" rookery install "$program"
    expect_native "$program" "$(command -v "${command[0]}")"
    check "run ${command[*]}" "${command[@]}"
done
# The AppImage needs FUSE and a display to run, so its build is checked by file type alone
check "install drawio" rookery install drawio
expect_native "drawio AppImage" "$(find ~/.local/share/rookery-programs/drawio -name '*.AppImage' | head -n 1)"

section "Any-architecture programs"
for program in clip cuda-run fasttarutils imcat kpod meetrec mtmux tarssh; do
    check "install $program" rookery install "$program"
done
expect_native "imcat (built from source)" "$(command -v imcat)"
for command in clip ftar tarssh cuda-run; do
    check "run $command --help" "$command" --help
done

section "Install root created with sudo"
# /opt is root-owned, so rookery creates the root with sudo and hands it to the test user. A
# separate bin directory keeps these links apart from the default root's.
opt_root=/opt/rookery-programs
check "install tarssh into $opt_root" \
    env ROOKERY_INSTALL_DIR=$opt_root ROOKERY_BIN_DIR="$HOME/opt-bin" rookery install tarssh
owner=$(stat -c %U "$opt_root" 2>&1)
if [ "$owner" = tester ]; then pass "$opt_root is owned by tester"; else fail "$opt_root is owned by tester ($owner)"; fi
if [ -x "$opt_root/tarssh/tarssh" ]; then pass "tarssh installed under $opt_root"; else fail "tarssh installed under $opt_root"; fi
# The default bin directory links into the default root, which is foreign to $opt_root
output=$(ROOKERY_INSTALL_DIR=$opt_root rookery link tarssh 2>&1)
status=$?
if [ "$status" -eq 1 ] && grep -qF "Rerun with --allow-foreign-root" <<<"$output"; then
    pass "link into a shared bin directory stops at the foreign-root gate"
else
    fail "link into a shared bin directory stops at the foreign-root gate (exit $status: $output)"
fi

section "Bulk commands"
output=$(rookery install --default 2>&1)
printf '%s\n' "$output"
if [ "$arch" = aarch64 ]; then
    for program in "${x86_only[@]}"; do
        expect_text "install --default skips $program" "Skipped $program: has no build for aarch64" "$output"
    done
fi
check "update every installed program" rookery update

section "Summary"
if [ "$failures" -eq 0 ]; then
    echo "All checks passed"
else
    echo "$failures check(s) failed"
    exit 1
fi
