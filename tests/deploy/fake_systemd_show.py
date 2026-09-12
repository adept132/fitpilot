"""Controlled systemctl-show boundary shared by executable deployment tests."""
from __future__ import annotations

import sys


def properties(unit: str, case: str, fragment: str, drop_in: str) -> str:
    def shell_path(value: str) -> str:
        value = value.replace("\\", "/")
        return f"/{value[0].lower()}{value[2:]}" if len(value) > 2 and value[1] == ":" else value
    fragment, drop_in = shell_path(fragment), shell_path(drop_in)
    if unit == "docker.service":
        values = {
            "LoadState": "loaded", "Transient": "no", "NeedDaemonReload": "no",
            "FragmentPath": "/usr/lib/systemd/system/docker.service",
            "DropInPaths": drop_in, "Requires": "sysinit.target eurith-release-views.service",
            "After": "network-online.target eurith-release-views.service",
        }
        mutations = {
            "docker_dependency": ("Requires", "sysinit.target"),
            "docker_order": ("After", "network-online.target"),
            "docker_dropin": ("DropInPaths", "/run/systemd/system/docker.service.d/override.conf"),
            "docker_transient": ("Transient", "yes"),
        }
    else:
        values = {
            "LoadState": "loaded", "Transient": "no", "NeedDaemonReload": "no",
            "FragmentPath": fragment, "SourcePath": "", "DropInPaths": "",
            "Type": "oneshot", "RemainAfterExit": "yes", "User": "", "Group": "",
            "ExecStart": "{ path=/usr/bin/env ; argv[]=/usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C /usr/local/libexec/eurith-release-views ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }",
            "Before": "shutdown.target docker.service", "RequiresMountsFor": "/opt/eurith/releases /opt/eurith/release-caddy-probe-source",
            "Environment": "", "EnvironmentFiles": "", "PassEnvironment": "",
            "RootDirectory": "", "RootImage": "", "WorkingDirectory": "",
            "ExecStartPre": "", "ExecStartPost": "", "ExecCondition": "",
            "ExecStop": "", "ExecStopPost": "", "PrivateMounts": "no",
            "PrivateTmp": "no", "ProtectSystem": "no", "BindPaths": "",
            "BindReadOnlyPaths": "", "TemporaryFileSystem": "", "InaccessiblePaths": "",
            "ReadOnlyPaths": "", "ReadWritePaths": "",
        }
        mutations = {
            "release_dropin": ("DropInPaths", "/etc/systemd/system/eurith-release-views.service.d/override.conf"),
            "release_transient": ("Transient", "yes"),
            "release_fragment": ("FragmentPath", "/run/systemd/transient/eurith-release-views.service"),
            "release_stale": ("NeedDaemonReload", "yes"),
            "release_exec": ("ExecStart", "{ path=/bin/true ; argv[]=/bin/true ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"),
            "release_environment": ("Environment", "BASH_ENV=/tmp/override"),
            "release_environment_file": ("EnvironmentFiles", "/tmp/override (ignore_errors=no)"),
            "release_namespace": ("PrivateMounts", "yes"),
            "release_root": ("RootDirectory", "/tmp/other-root"),
            "release_order": ("Before", "shutdown.target"),
            "release_mount_dependency": ("RequiresMountsFor", "/opt/eurith/releases"),
        }
    if case in mutations:
        key, value = mutations[case]
        values[key] = value
    # systemd v255 systemctl-show.c emits nothing for empty struct arrays.
    for key in ("EnvironmentFiles", "ExecStartPre", "ExecStartPost", "ExecCondition", "ExecStop", "ExecStopPost"):
        if not values.get(key):
            values.pop(key, None)
    return "".join(f"{key}={value}\n" for key, value in values.items())


if __name__ == "__main__":
    sys.stdout.reconfigure(newline="\n")
    sys.stdout.write(properties(*sys.argv[1:]))
