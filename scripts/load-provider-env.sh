#!/bin/bash
# Source this from the launch scripts. Keep provider credentials outside git.
# The legacy explicit override remains supported for existing launch scripts.
_claw_provider_env_file="${EMNETS_RA_ENV_FILE:-${CLAW_AI_LAB_ENV_FILE:-${XDG_CONFIG_HOME:-${HOME:-/tmp}/.config}/emnets-ra/provider.env}}"
if [ -f "$_claw_provider_env_file" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$_claw_provider_env_file"
    set +a
fi
unset _claw_provider_env_file
