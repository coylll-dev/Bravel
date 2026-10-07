#!/usr/bin/env bash
set -e
source "$(dirname "${BASH_SOURCE[0]}")/../bravel/integrations/bravel.bash"
READLINE_LINE='# show networks; $(touch SHOULD_NOT_EXIST)'
READLINE_POINT=0
_aic_prepare_line
[[ $READLINE_LINE == ai* ]]
ai() { [[ $1 == ' show networks; $(touch SHOULD_NOT_EXIST)' ]]; }
eval "$READLINE_LINE"
[[ ! -e SHOULD_NOT_EXIST ]]
READLINE_LINE='printf ordinary'
_aic_prepare_line
[[ $READLINE_LINE == 'printf ordinary' ]]
bravel_disable
[[ ${_AIC_LOADED:-0} == 0 ]]
printf 'Bash integration smoke test passed\n'
