#!/bin/bash

export GG_GIT_HELPERS=${PWD}

fail() {
    echo $* >&2

    exit 1
}
try () {
    echo $*
    $* || fail $*
}

install_gg_cli() {
    echo "Install gg CLI tool"
    if command -v uv >/dev/null 2>&1; then
        try uv tool install --reinstall -e "${GG_GIT_HELPERS}"
    else
        echo "WARNING: uv not found. Install uv first, then run:"
        echo "  uv tool install --reinstall -e ${GG_GIT_HELPERS}"
    fi
}

usage() {
    cat <<EOF
Usage: install.sh [OPTION]

Install git-helpers: symlink ~/.bashrc.gitgo and install the gg CLI via uv,
then print the gitconfig and .bashrc snippets to add manually.

Options:
  (no args)   Full install: ~/.bashrc.gitgo symlink + gg CLI + print snippets
  --gg        Only (re)install the gg CLI via 'uv tool install -e'
  -h, --help  Show this help and exit
EOF
}

case "$1" in
    -h|--help)
        usage
        exit 0
        ;;
    --gg)
        install_gg_cli
        exit 0
        ;;
esac

echo "Install .bashrc.gitgo"
try rm -f ~/.bashrc.gitgo
try ln -s ${GG_GIT_HELPERS}/bashrc.gitgo ~/.bashrc.gitgo

echo ""
install_gg_cli

echo -e "\n\n"
echo -e "!!!Add the below into your $HOME/.bashrc \n\n"

cat <<EOF
################## GIT-HELPERS ################
# vgit show
export GG_GIT_HELPERS=$GG_GIT_HELPERS
. \${GG_GIT_HELPERS}/bashrc.vgit
EOF

echo -e "\n\n"
echo -e "Add the below to your .gitconfig\n\n"

cat <<EOF
################## GIT-HELPERS ################
[gc]
	reflogExpire = 2 years

[merge]
	conflictstyle = diff3

[include]
  path = ${GG_GIT_HELPERS}/gitconfig.go
  path = ${GG_GIT_HELPERS}/gitconfig.tree
  path = ${GG_GIT_HELPERS}/gitconfig.alias

EOF

echo "If you're on mac - make sure gsed is in PATH"
