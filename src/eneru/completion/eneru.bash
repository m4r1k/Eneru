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

    local subcommands="run shutdown remote validate monitor tui test-notifications completion version"
    local global_opts="-h --help"
    local config_opts="-c --config"
    local monitor_opts="--once --interval --graph --time --events-only -v --verbose --length"
    local run_opts="--dry-run --exit-after-shutdown"
    local shutdown_remote_opts="$config_opts --server --group --dry-run --i-really-want-to-proceed-with-remote-shutdown --connectivity-check --no-connectivity-check --log-file"
    local shutdown_group_opts="$config_opts --group --dry-run --i-really-want-to-proceed-with-group-shutdown --log-file"
    local remote_list_opts="$config_opts"
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
            # mapfile -t preserves filenames containing spaces or other
            # word-splitting metacharacters; the bare $(...) form would
            # split each name into separate completions.
            mapfile -t COMPREPLY < <(compgen -f -- "$cur")
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
        --interval|--length)
            # Numeric; offer common defaults but allow free input.
            COMPREPLY=( $(compgen -W "1 2 5 10 30 60" -- "$cur") )
            return 0
            ;;
        --server|--group)
            COMPREPLY=()
            return 0
            ;;
        --log-file)
            mapfile -t COMPREPLY < <(compgen -f -- "$cur")
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
        shutdown)
            local shutdown_leaf=""
            for ((i=2; i < COMP_CWORD; i++)); do
                case "${COMP_WORDS[i]}" in
                    -*) continue ;;
                    remote|group) shutdown_leaf="${COMP_WORDS[i]}"; break ;;
                esac
            done
            case "$shutdown_leaf" in
                remote)
                    COMPREPLY=( $(compgen -W "$shutdown_remote_opts $global_opts" -- "$cur") )
                    ;;
                group)
                    COMPREPLY=( $(compgen -W "$shutdown_group_opts $global_opts" -- "$cur") )
                    ;;
                *)
                    mapfile -t COMPREPLY < <(compgen -W "remote group $global_opts" -- "$cur")
                    ;;
            esac
            ;;
        remote)
            local remote_leaf=""
            for ((i=2; i < COMP_CWORD; i++)); do
                case "${COMP_WORDS[i]}" in
                    -*) continue ;;
                    list) remote_leaf="list"; break ;;
                esac
            done
            if [[ "$remote_leaf" == "list" ]]; then
                COMPREPLY=( $(compgen -W "$remote_list_opts $global_opts" -- "$cur") )
            else
                mapfile -t COMPREPLY < <(compgen -W "list $global_opts" -- "$cur")
            fi
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
