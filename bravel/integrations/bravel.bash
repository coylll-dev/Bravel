# Source in an interactive Bash shell. Regular commands stay in Bash.
[[ $- == *i* ]] || return 0
[[ ${_AIC_LOADED:-0} == 1 ]] && return 0
_AIC_LOADED=1
_AIC_PREVIOUS_NOT_FOUND=$(declare -f command_not_found_handle || true)
_AIC_PREVIOUS_ENTER=$(bind -s | grep '^"\\C-m":' || true)
_AIC_PREVIOUS_ENTER_FUNCTION=$(bind -p | grep '^"\\C-m":' || true)

ai() {
    local approved status
    approved=$(command bravel ask --shell bash --emit-command -- "$*")
    status=$?
    (( status == 0 )) || return "$status"
    [[ -n $approved ]] || return 0
    eval "$approved"
}

command_not_found_handle() {
    local request='' part approved status
    # Bash runs this hook in a subshell. cd/export here cannot affect the parent.
    for part in "$@"; do
        printf -v part '%q' "$part"
        request+="${request:+ }$part"
    done
    approved=$(command bravel fix --shell bash --emit-command -- "$request")
    status=$?
    (( status == 0 )) || return 127
    [[ -n $approved ]] || return 127
    eval "$approved"
}

_aic_prepare_line() {
    local line=${READLINE_LINE#"${READLINE_LINE%%[![:space:]]*}"} prompt
    [[ $line == \#* && $line != *$'\n'* ]] || return 0
    prompt=${line:1}
    [[ -n ${prompt//[[:space:]]/} ]] || return 0
    printf -v READLINE_LINE 'ai %q' "$prompt"
    READLINE_POINT=${#READLINE_LINE}
}

# Prepare the line, then use Bash's normal accept-line (preserves pipes, cd,
# completion, native error reporting, and normal execution semantics).
# Ctrl-J bypasses # interpretation when a literal comment is wanted.
bind -x '"\C-x\C-a":_aic_prepare_line'
bind '"\C-m":"\C-x\C-a\C-j"'

bravel_disable() {
    bind -r '\C-x\C-a'
    if [[ -n $_AIC_PREVIOUS_ENTER ]]; then bind "$_AIC_PREVIOUS_ENTER"
    elif [[ -n $_AIC_PREVIOUS_ENTER_FUNCTION ]]; then bind "$_AIC_PREVIOUS_ENTER_FUNCTION"
    else bind '"\C-m":accept-line'; fi
    unset -f command_not_found_handle
    [[ -z $_AIC_PREVIOUS_NOT_FOUND ]] || eval "$_AIC_PREVIOUS_NOT_FOUND"
    unset -f ai _aic_prepare_line bravel_disable
    unset _AIC_LOADED _AIC_PREVIOUS_NOT_FOUND _AIC_PREVIOUS_ENTER _AIC_PREVIOUS_ENTER_FUNCTION
}

printf '\033[96m  ◆ Bravel подключён · # запрос · ai запрос · bravel_disable\033[0m\n'
