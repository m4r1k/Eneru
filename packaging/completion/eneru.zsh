#compdef eneru
#
# zsh completion for eneru.
#
# Self-contained: uses only zsh builtins (_arguments, _values, _files).
# Auto-loaded when installed at:
#   /usr/share/zsh/site-functions/_eneru
# Or sourced manually:
#   source <(eneru completion zsh)

_eneru() {
    local -a subcommands graph_choices time_choices completion_shells

    subcommands=(
        'run:Start the monitoring daemon'
        'validate:Validate configuration and show overview'
        'monitor:Launch real-time TUI dashboard'
        'tui:Alias for monitor -- launch real-time TUI dashboard'
        'test-notifications:Send a test notification and exit'
        'completion:Print shell completion script (bash/zsh/fish)'
        'version:Show version information'
    )
    graph_choices=(charge load voltage runtime)
    time_choices=(1h 6h 24h 7d 30d)
    completion_shells=(bash zsh fish)

    local context state state_descr line
    typeset -A opt_args

    _arguments -C \
        '(-h --help)'{-h,--help}'[show help and exit]' \
        '1: :->subcmd' \
        '*::arg:->args'

    case $state in
        subcmd)
            _describe -t subcommands 'eneru subcommand' subcommands
            ;;
        args)
            case $line[1] in
                run)
                    _arguments \
                        '(-c --config)'{-c,--config}'[path to configuration file]:config file:_files' \
                        '--dry-run[run in dry-run mode (overrides config)]' \
                        '--exit-after-shutdown[exit after completing shutdown sequence]'
                    ;;
                validate|test-notifications)
                    _arguments \
                        '(-c --config)'{-c,--config}'[path to configuration file]:config file:_files'
                    ;;
                monitor|tui)
                    _arguments \
                        '(-c --config)'{-c,--config}'[path to configuration file]:config file:_files' \
                        '--once[print status snapshot and exit (no TUI)]' \
                        '--interval[refresh interval in seconds]:seconds:(1 2 5 10 30 60)' \
                        "--graph[render graph for metric]:metric:($graph_choices)" \
                        "--time[time range]:range:($time_choices)" \
                        '--events-only[print only the events list]'
                    ;;
                completion)
                    _values 'shell' $completion_shells
                    ;;
            esac
            ;;
    esac
}

_eneru "$@"
