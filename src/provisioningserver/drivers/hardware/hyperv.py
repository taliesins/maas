# Copyright 2016 Cloudbase Solutions Srl
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import sys

from winrm import protocol
from provisioningserver.logger import get_maas_logger
maaslog = get_maas_logger("drivers.power.hyperv")

AUTH_BASIC = "basic"
AUTH_KERBEROS = "kerberos"
AUTH_CERTIFICATE = "certificate"

DEFAULT_PORT_HTTP = 5985
DEFAULT_PORT_HTTPS = 5986

CODEPAGE_UTF8 = 65001

AUTH_TRANSPORT_MAP = {
    AUTH_BASIC: 'plaintext',
    AUTH_KERBEROS: 'kerberos',
    AUTH_CERTIFICATE: 'ssl'
}


VM_RUNNING = "Running"
VM_PAUSED = "Paused"
VM_STOPPED = "Off"
VM_STARTING = "Starting"
VM_STOPPING = "Stopping"

VM_STATE_TO_POWER_STATE = {
    VM_RUNNING: "on",
    VM_STARTING: "on",
    VM_STOPPING: "on",
    VM_STOPPED: "off",
    VM_PAUSED: "off",
}


class HypervCmdError(Exception):
    """Failed to run command on remote Hyper-V node"""


class WinRM(object):

    def __init__(self, power_address, username, password, use_ssl=True):
        self.hostname = power_address
        self.use_ssl = use_ssl
        maaslog.warning("HV init")

        self.protocol = self._protocol(username, password)

    def _protocol(self, username, password):
        protocol.Protocol.DEFAULT_TIMEOUT = "PT3600S"
        p = protocol.Protocol(endpoint=self._url,
                              transport=AUTH_TRANSPORT_MAP[AUTH_BASIC],
                              username=username,
                              password=password,
                              server_cert_validation='ignore')
        return p

    def _run_command(self, cmd):
        if type(cmd) is not list:
            raise ValueError("command must be a list")
        maaslog.warning("HV run cmd: %s", cmd)
        try:
            shell_id = self.protocol.open_shell(codepage=CODEPAGE_UTF8)
        except:
            maaslog.warning("HV crapat: %s", sys.exc_info()[0])
        command_id = self.protocol.run_command(shell_id, cmd[0], cmd[1:])
        std_out, std_err, status_code = self.protocol.get_command_output(shell_id, command_id)
        self.protocol.cleanup_command(shell_id, command_id)

        self.protocol.close_shell(shell_id)

        if status_code:  
            raise HypervCmdError("Failed to run command: %s. Error message: %s" % (" ".join(cmd), std_err))
        return std_out

    def run_command(self, command):
        return self._run_command(command)

    def run_powershell_command(self, command):
        pscommand = ["powershell.exe", "-ExecutionPolicy", "RemoteSigned",
                     "-NonInteractive", "-Command"] + command
        return self._run_command(pscommand)

    def get_vm_state(self, machine):
        state = self.run_powershell_command(['(Get-VM %s -ErrorAction SilentlyContinue).State' % machine, ])
        maaslog.warning(state.strip())

        if not state:
            raise HypervCmdError("Machine %s was not found on hypervisor %s" % (machine, self.hostname))
        return state.strip().decode("utf-8")

    @property
    def _url(self):
        proto = "http"
        port = DEFAULT_PORT_HTTP
        if self.use_ssl:
            proto = "https"
            port = DEFAULT_PORT_HTTPS
        return "%s://%s:%s/wsman" % (proto, self.hostname, port)

    def status(self, vm):
        url = self._url


def power_state_hyperv(poweraddr, machine, username, password):
    """Return the power state for the VM using WinRM."""

    conn = WinRM(poweraddr, username, password)
    state = conn.get_vm_state(machine)
    maaslog.warning("hv: %s", state)
    maaslog.warning(VM_STATE_TO_POWER_STATE)
    try:
        return VM_STATE_TO_POWER_STATE[state]
    except KeyError:
        raise HypervCmdError('Unknown state: %s' % state)


def power_control_hyperv(poweraddr, machine, power_change, username, password):
    """Power controls a VM using WinRM."""

    conn = WinRM(poweraddr, username, password)
    state = conn.get_vm_state(machine)
    maaslog.warning("HV status: %s", state)

    if state == VM_STOPPED:
        if power_change == 'on':
            startCmd = '(Set-VMFirmware %s -BootOrder (Get-VMNetworkAdapter %s)); (Start-VM %s)' % (machine, machine, machine)
            conn.run_powershell_command([startCmd, ])
    elif state == VM_RUNNING:
        if power_change == 'off':
            conn.run_powershell_command(['Stop-VM %s -Force' % machine,])
