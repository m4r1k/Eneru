# fish completion for eneru.
#
# Auto-loaded when installed at:
#   /usr/share/fish/vendor_completions.d/eneru.fish
# Or sourced manually:
#   eneru completion fish | source

# Helper: true when no subcommand has been typed yet.
function __eneru_no_subcommand
    set -l cmd (commandline -opc)
    if test (count $cmd) -lt 2
        return 0
    end
    for word in $cmd[2..-1]
        switch $word
            case '-*'
                continue
            case '*'
                return 1
        end
    end
    return 0
end

# Helper: true when the given subcommand has already been typed.
function __eneru_using
    set -l want $argv[1]
    set -l cmd (commandline -opc)
    for word in $cmd[2..-1]
        switch $word
            case '-*'
                continue
            case $want
                return 0
            case '*'
                return 1
        end
    end
    return 1
end

# Subcommands.
complete -c eneru -n '__eneru_no_subcommand' -f -a 'run' -d 'Start the monitoring daemon'
complete -c eneru -n '__eneru_no_subcommand' -f -a 'validate' -d 'Validate configuration and show overview'
complete -c eneru -n '__eneru_no_subcommand' -f -a 'monitor' -d 'Launch real-time TUI dashboard'
complete -c eneru -n '__eneru_no_subcommand' -f -a 'tui' -d 'Alias for monitor'
complete -c eneru -n '__eneru_no_subcommand' -f -a 'test-notifications' -d 'Send a test notification'
complete -c eneru -n '__eneru_no_subcommand' -f -a 'completion' -d 'Print shell completion script'
complete -c eneru -n '__eneru_no_subcommand' -f -a 'version' -d 'Show version information'

# Global options.
complete -c eneru -s h -l help -d 'Show help and exit'

# Shared --config / -c (run, validate, monitor, tui, test-notifications).
for sub in run validate monitor tui test-notifications
    complete -c eneru -n "__eneru_using $sub" -s c -l config -r -d 'Path to configuration file'
end

# `run` options.
complete -c eneru -n '__eneru_using run' -l dry-run -d 'Run in dry-run mode'
complete -c eneru -n '__eneru_using run' -l exit-after-shutdown -d 'Exit after shutdown sequence'

# `monitor` / `tui` options.
for sub in monitor tui
    complete -c eneru -n "__eneru_using $sub" -l once -d 'Print status snapshot and exit'
    complete -c eneru -n "__eneru_using $sub" -l interval -r -d 'Refresh interval in seconds'
    complete -c eneru -n "__eneru_using $sub" -l graph -r -fa 'charge load voltage runtime' -d 'Metric to graph'
    complete -c eneru -n "__eneru_using $sub" -l time -r -fa '1h 6h 24h 7d 30d' -d 'Time range'
    complete -c eneru -n "__eneru_using $sub" -l events-only -d 'Print only the events list'
end

# `completion` argument.
complete -c eneru -n '__eneru_using completion' -f -a 'bash zsh fish' -d 'Shell to emit completion for'
