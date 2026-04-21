"""Shell completion scripts for the ``eneru`` CLI.

The ``.bash``, ``.zsh``, and ``.fish`` files in this package are the
single source of truth -- read at runtime by ``eneru completion <shell>``
via ``importlib.resources``, and dropped at canonical FHS paths by
``nfpm.yaml`` so the deb/rpm install auto-loads them when the host's
shell-completion framework is present.
"""
