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

function __eneru_using_shutdown_remote
    set -l cmd (commandline -opc)
    set -l saw_shutdown 0
    for word in $cmd[2..-1]
        switch $word
            case '-*'
                continue
            case shutdown
                set saw_shutdown 1
            case remote
                test $saw_shutdown -eq 1; and return 0
                return 1
            case '*'
                test $saw_shutdown -eq 1; and return 1
        end
    end
    return 1
end

function __eneru_using_shutdown_group
    set -l cmd (commandline -opc)
    set -l saw_shutdown 0
    for word in $cmd[2..-1]
        switch $word
            case '-*'
                continue
            case shutdown
                set saw_shutdown 1
            case group
                test $saw_shutdown -eq 1; and return 0
                return 1
            case '*'
                test $saw_shutdown -eq 1; and return 1
        end
    end
    return 1
end

function __eneru_using_remote_list
    set -l cmd (commandline -opc)
    set -l saw_remote 0
    for word in $cmd[2..-1]
        switch $word
            case '-*'
                continue
            case remote
                set saw_remote 1
            case list
                test $saw_remote -eq 1; and return 0
                return 1
            case '*'
                test $saw_remote -eq 1; and return 1
        end
    end
    return 1
end

# Subcommands.
complete -c eneru -n '__eneru_no_subcommand' -f -a 'run' -d 'Start the monitoring daemon'
complete -c eneru -n '__eneru_no_subcommand' -f -a 'shutdown' -d 'Manual shutdown drills'
complete -c eneru -n '__eneru_no_subcommand' -f -a 'remote' -d 'Inspect configured remote shutdown targets'
complete -c eneru -n '__eneru_no_subcommand' -f -a 'validate' -d 'Validate configuration and show overview'
complete -c eneru -n '__eneru_no_subcommand' -f -a 'monitor' -d 'Launch real-time TUI dashboard'
complete -c eneru -n '__eneru_no_subcommand' -f -a 'tui' -d 'Alias for monitor'
complete -c eneru -n '__eneru_no_subcommand' -f -a 'test-notifications' -d 'Send a test notification'
complete -c eneru -n '__eneru_no_subcommand' -f -a 'self-test' -d 'Issue or inspect a UPS battery self-test'
complete -c eneru -n '__eneru_no_subcommand' -f -a 'completion' -d 'Print shell completion script'
complete -c eneru -n '__eneru_no_subcommand' -f -a 'version' -d 'Show version information'

# `self-test` subcommands.
complete -c eneru -n '__eneru_using self-test' -f -a 'run' -d 'Issue a self-test'
complete -c eneru -n '__eneru_using self-test' -f -a 'status' -d 'Show the latest self-test result'
complete -c eneru -n '__eneru_using self-test' -l ups -r -d 'UPS name'
complete -c eneru -n '__eneru_using self-test' -s c -l config -r -d 'Path to configuration file'
complete -c eneru -n '__eneru_using self-test' -l direct -d 'Issue directly via NUT (no daemon)'
complete -c eneru -n '__eneru_using self-test' -l url -r -d 'Daemon API base URL'
complete -c eneru -n '__eneru_using self-test' -l token -r -d 'Bearer session token for the daemon API'
complete -c eneru -n '__eneru_using self-test' -l api-key -r -d 'API key for the daemon API'

# Global options.
complete -c eneru -s h -l help -d 'Show help and exit'

# Shared --config / -c (run, validate, monitor, tui, test-notifications).
for sub in run validate monitor tui test-notifications
    complete -c eneru -n "__eneru_using $sub" -s c -l config -r -d 'Path to configuration file'
end

# `run` options.
complete -c eneru -n '__eneru_using run' -l dry-run -d 'Run in dry-run mode'
complete -c eneru -n '__eneru_using run' -l api -d 'Enable the embedded read-only API'
complete -c eneru -n '__eneru_using run' -l api-bind -r -fa '127.0.0.1 0.0.0.0' -d 'API listen address'
complete -c eneru -n '__eneru_using run' -l api-port -r -fa '9191 9100' -d 'API listen port'
complete -c eneru -n '__eneru_using run' -l exit-after-shutdown -d 'Exit after shutdown sequence'

# `shutdown` subcommands.
complete -c eneru -n '__eneru_using shutdown' -f -a 'remote' -d 'Run one remote shutdown drill'
complete -c eneru -n '__eneru_using shutdown' -f -a 'group' -d 'Rehearse the full configured shutdown sequence for one group'

# `shutdown remote` options.
complete -c eneru -n '__eneru_using_shutdown_remote' -s c -l config -r -d 'Path to configuration file'
complete -c eneru -n '__eneru_using_shutdown_remote' -l server -r -d 'Remote server name or host'
complete -c eneru -n '__eneru_using_shutdown_remote' -l group -r -d 'UPS or redundancy group'
complete -c eneru -n '__eneru_using_shutdown_remote' -l dry-run -d 'Do not execute configured commands'
complete -c eneru -n '__eneru_using_shutdown_remote' -l i-really-want-to-proceed-with-remote-shutdown -d 'Confirm real remote shutdown'
complete -c eneru -n '__eneru_using_shutdown_remote' -l connectivity-check -d 'Run harmless SSH probe first'
complete -c eneru -n '__eneru_using_shutdown_remote' -l no-connectivity-check -d 'Skip harmless SSH probe'
complete -c eneru -n '__eneru_using_shutdown_remote' -l log-file -r -d 'Append drill log to file'

# `shutdown group` options.
complete -c eneru -n '__eneru_using_shutdown_group' -s c -l config -r -d 'Path to configuration file'
complete -c eneru -n '__eneru_using_shutdown_group' -l group -r -d 'UPS group label/name or redundancy group name'
complete -c eneru -n '__eneru_using_shutdown_group' -l dry-run -d 'Log every phase without executing'
complete -c eneru -n '__eneru_using_shutdown_group' -l i-really-want-to-proceed-with-group-shutdown -d 'Confirm real group shutdown'
complete -c eneru -n '__eneru_using_shutdown_group' -l log-file -r -d 'Append rehearsal log to file'

# `remote` subcommands.
complete -c eneru -n '__eneru_using remote' -f -a 'list' -d 'List configured remote shutdown targets'

# `remote list` options.
complete -c eneru -n '__eneru_using_remote_list' -s c -l config -r -d 'Path to configuration file'

# `monitor` / `tui` options.
for sub in monitor tui
    complete -c eneru -n "__eneru_using $sub" -l once -d 'Print status snapshot and exit'
    complete -c eneru -n "__eneru_using $sub" -l interval -r -d 'Refresh interval in seconds'
    complete -c eneru -n "__eneru_using $sub" -l graph -r -fa 'charge load voltage runtime' -d 'Metric to graph'
    complete -c eneru -n "__eneru_using $sub" -l time -r -fa '1h 6h 24h 7d 30d' -d 'Time range'
    complete -c eneru -n "__eneru_using $sub" -l events-only -d 'Print only the events list'
    complete -c eneru -n "__eneru_using $sub" -s v -l verbose -d 'Increase event verbosity'
    complete -c eneru -n "__eneru_using $sub" -l length -r -fa '0 10 30 60 100 500' -d 'Max events to print with --once'
end

# `completion` argument.
complete -c eneru -n '__eneru_using completion' -f -a 'bash zsh fish' -d 'Shell to emit completion for'
