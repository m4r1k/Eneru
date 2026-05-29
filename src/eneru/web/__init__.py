"""Static assets for the embedded browser dashboard (v6.0).

This is a real package (not just a directory) so the dashboard files ship in the
wheel and can be located with ``importlib.resources.files("eneru.web")``
regardless of install method (pip vs deb/rpm). The HTML/CSS/JS are served as-is
by the embedded API server; there is no build step and no third-party JavaScript.
"""
