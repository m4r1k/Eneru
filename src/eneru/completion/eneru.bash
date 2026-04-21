# bash completion for eneru
#
# Self-contained: does not depend on the bash-completion package or its
# helpers (_init_completion, _filedir). Uses bash builtins only so it
# works on minimal systems where bash-completion isn't installed.
#
# Auto-loaded by bash-completion if installed at:
#   /usr/share/bash-completion/completions/eneru
# Or sourced manually:
#   source <(eneru completion bash)

_eneru() {
    local cur prev words cword
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"

    local subcommands="run validate monitor tui test-notifications completion version"
    local global_opts="-h --help"
    local config_opts="-c --config"
    local monitor_opts="--once --interval --graph --time --events-only"
    local run_opts="--dry-run --exit-after-shutdown"
    local graph_choices="charge load voltage runtime"
    local time_choices="1h 6h 24h 7d 30d"
    local completion_shells="bash zsh fish"

    # Find the subcommand (first non-flag word after `eneru`).
    local subcmd=""
    local i
    for ((i=1; i < COMP_CWORD; i++)); do
        case "${COMP_WORDS[i]}" in
            -*) continue ;;
            *)  subcmd="${COMP_WORDS[i]}"; break ;;
        esac
    done

    # Value-of-flag completion is shared across subcommands.
    case "$prev" in
        -c|--config)
            COMPREPLY=( $(compgen -f -- "$cur") )
            return 0
            ;;
        --graph)
            COMPREPLY=( $(compgen -W "$graph_choices" -- "$cur") )
            return 0
            ;;
        --time)
            COMPREPLY=( $(compgen -W "$time_choices" -- "$cur") )
            return 0
            ;;
        --interval)
            # Numeric; offer common defaults but allow free input.
            COMPREPLY=( $(compgen -W "1 2 5 10 30 60" -- "$cur") )
            return 0
            ;;
    esac

    # No subcommand yet: complete subcommands.
    if [[ -z "$subcmd" ]]; then
        COMPREPLY=( $(compgen -W "$subcommands $global_opts" -- "$cur") )
        return 0
    fi

    # Within a subcommand: offer the right options.
    case "$subcmd" in
        run)
            COMPREPLY=( $(compgen -W "$config_opts $run_opts $global_opts" -- "$cur") )
            ;;
        validate|test-notifications)
            COMPREPLY=( $(compgen -W "$config_opts $global_opts" -- "$cur") )
            ;;
        monitor|tui)
            COMPREPLY=( $(compgen -W "$config_opts $monitor_opts $global_opts" -- "$cur") )
            ;;
        completion)
            COMPREPLY=( $(compgen -W "$completion_shells $global_opts" -- "$cur") )
            ;;
        version)
            COMPREPLY=( $(compgen -W "$global_opts" -- "$cur") )
            ;;
        *)
            COMPREPLY=()
            ;;
    esac
}

complete -F _eneru eneru
