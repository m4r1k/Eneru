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
        'shutdown:Manual shutdown drills'
        'remote:Inspect configured remote shutdown targets'
        'validate:Validate configuration and show overview'
        'monitor:Launch real-time TUI dashboard'
        'tui:Alias for monitor -- launch real-time TUI dashboard'
        'test-notifications:Send a test notification and exit'
        'self-test:Issue or inspect a UPS battery self-test'
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
                        '--api[enable the embedded read-only API]' \
                        '--api-bind[API listen address]:address:(127.0.0.1 0.0.0.0)' \
                        '--api-port[API listen port]:port:(9191 9100)' \
                        '--exit-after-shutdown[exit after completing shutdown sequence]'
                    ;;
                shutdown)
                    case "$line[2]" in
                        remote)
                            _arguments \
                                '(-c --config)'{-c,--config}'[path to configuration file]:config file:_files' \
                                '--server[remote server name or host]:server:' \
                                '--group[UPS or redundancy group]:group:' \
                                '--dry-run[do not execute configured commands]' \
                                '--i-really-want-to-proceed-with-remote-shutdown[confirm real remote shutdown]' \
                                '--connectivity-check[run harmless SSH probe first]' \
                                '--no-connectivity-check[skip harmless SSH probe]' \
                                '--log-file[append drill log to file]:log file:_files'
                            ;;
                        group)
                            _arguments \
                                '(-c --config)'{-c,--config}'[path to configuration file]:config file:_files' \
                                '--group[UPS group label/name or redundancy group name]:group:' \
                                '--dry-run[log every phase without executing]' \
                                '--i-really-want-to-proceed-with-group-shutdown[confirm real group shutdown]' \
                                '--log-file[append rehearsal log to file]:log file:_files'
                            ;;
                        *)
                            _values 'shutdown command' remote group
                            ;;
                    esac
                    ;;
                remote)
                    if [[ "$line[2]" == "list" ]]; then
                        _arguments \
                            '(-c --config)'{-c,--config}'[path to configuration file]:config file:_files'
                    else
                        _values 'remote command' list
                    fi
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
                        '--events-only[print only the events list]' \
                        '(-v --verbose)'{-v,--verbose}'[increase event verbosity]' \
                        '--length[max events to print with --once]:rows:(0 10 30 60 100 500)'
                    ;;
                completion)
                    _values 'shell' $completion_shells
                    ;;
            esac
            ;;
    esac
}

_eneru "$@"
