"""Shutdown-phase mixins for UPSGroupMonitor.

Each mixin owns one phase of the controlled shutdown sequence:

* :class:`~eneru.shutdown.vms.VMShutdownMixin` - libvirt virtual machines
* :class:`~eneru.shutdown.containers.ContainerShutdownMixin` - docker/podman
* :class:`~eneru.shutdown.filesystems.FilesystemShutdownMixin` - sync + unmount
* :class:`~eneru.shutdown.remote.RemoteShutdownMixin` - SSH-based remote servers

Mixins assume the host class provides ``self.config``, ``self.state``,
``self._log_message``, ``self._send_notification``, and (for containers)
``self._container_runtime`` / ``self._compose_available``.
"""
