try:  # py3
    from shlex import quote
except ImportError:  # py2
    from pipes import quote
import click
import hashlib
import logging
import os
import subprocess
import sys
import time

from threading import Thread
from getpass import getuser

from ray.autoscaler.tags import TAG_RAY_NODE_STATUS, TAG_RAY_RUNTIME_CONFIG, \
    STATUS_UP_TO_DATE, STATUS_UPDATE_FAILED, STATUS_WAITING_FOR_SSH, \
    STATUS_SETTING_UP, STATUS_SYNCING_FILES
from ray.autoscaler.log_timer import LogTimer

logger = logging.getLogger(__name__)

# How long to wait for a node to start, in seconds
NODE_START_WAIT_S = 300
READY_CHECK_INTERVAL = 5
HASH_MAX_LENGTH = 10
KUBECTL_RSYNC = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "kubernetes/kubectl-rsync.sh")


def with_interactive(cmd):
    force_interactive = ("true && source ~/.bashrc && "
                         "export OMP_NUM_THREADS=1 PYTHONWARNINGS=ignore && ")
    return ["bash", "--login", "-c", "-i", quote(force_interactive + cmd)]


class KubernetesCommandRunner:
    def __init__(self, log_prefix, namespace, node_id, auth_config,
                 process_runner):

        self.log_prefix = log_prefix
        self.process_runner = process_runner
        self.node_id = node_id
        self.namespace = namespace
        self.kubectl = ["kubectl", "-n", self.namespace]

    def run(self,
            cmd=None,
            timeout=120,
            allocate_tty=False,
            exit_on_fail=False,
            port_forward=None,
            with_output=False):
        if cmd and port_forward:
            raise Exception(
                "exec with Kubernetes can't forward ports and execute"
                "commands together.")

        if port_forward:
            if not isinstance(port_forward, list):
                port_forward = [port_forward]
            port_forward_cmd = self.kubectl + [
                "port-forward",
                self.node_id,
            ] + [
                "{}:{}".format(local, remote) for local, remote in port_forward
            ]
            logger.info("Port forwarding with: {}".format(
                " ".join(port_forward_cmd)))
            port_forward_process = subprocess.Popen(port_forward_cmd)
            port_forward_process.wait()
            # We should never get here, this indicates that port forwarding
            # failed, likely because we couldn't bind to a port.
            pout, perr = port_forward_process.communicate()
            exception_str = " ".join(
                port_forward_cmd) + " failed with error: " + perr
            raise Exception(exception_str)
        else:
            logger.info(self.log_prefix + "Running {}...".format(cmd))
            final_cmd = self.kubectl + [
                "exec",
                "-it" if allocate_tty else "-i",
                self.node_id,
                "--",
            ] + with_interactive(cmd)
            try:
                if with_output:
                    return self.process_runner.check_output(
                        " ".join(final_cmd), shell=True)
                else:
                    self.process_runner.check_call(
                        " ".join(final_cmd), shell=True)
            except subprocess.CalledProcessError:
                if exit_on_fail:
                    quoted_cmd = " ".join(final_cmd[:-1] +
                                          [quote(final_cmd[-1])])
                    logger.error(
                        self.log_prefix +
                        "Command failed: \n\n  {}\n".format(quoted_cmd))
                    sys.exit(1)
                else:
                    raise

    def run_rsync_up(self, source, target):
        if target.startswith("~"):
            target = "/root" + target[1:]

        try:
            self.process_runner.check_call([
                KUBECTL_RSYNC,
                "-avz",
                source,
                "{}@{}:{}".format(self.node_id, self.namespace, target),
            ])
        except Exception as e:
            logger.warning(self.log_prefix +
                           "rsync failed: '{}'. Falling back to 'kubectl cp'"
                           .format(e))
            self.process_runner.check_call(self.kubectl + [
                "cp", source, "{}/{}:{}".format(self.namespace, self.node_id,
                                                target)
            ])

    def run_rsync_down(self, source, target):
        if target.startswith("~"):
            target = "/root" + target[1:]

        try:
            self.process_runner.check_call([
                KUBECTL_RSYNC,
                "-avz",
                "{}@{}:{}".format(self.node_id, self.namespace, source),
                target,
            ])
        except Exception as e:
            logger.warning(self.log_prefix +
                           "rsync failed: '{}'. Falling back to 'kubectl cp'"
                           .format(e))
            self.process_runner.check_call(self.kubectl + [
                "cp", "{}/{}:{}".format(self.namespace, self.node_id, source),
                target
            ])

    def remote_shell_command_str(self):
        return "{} exec -it {} bash".format(" ".join(self.kubectl),
                                            self.node_id)


class SSHCommandRunner:
    def __init__(self, log_prefix, node_id, provider, auth_config,
                 cluster_name, process_runner, use_internal_ip):

        ssh_control_hash = hashlib.md5(cluster_name.encode()).hexdigest()
        ssh_user_hash = hashlib.md5(getuser().encode()).hexdigest()
        ssh_control_path = "/tmp/ray_ssh_{}/{}".format(
            ssh_user_hash[:HASH_MAX_LENGTH],
            ssh_control_hash[:HASH_MAX_LENGTH])

        self.log_prefix = log_prefix
        self.process_runner = process_runner
        self.node_id = node_id
        self.use_internal_ip = use_internal_ip
        self.provider = provider
        self.ssh_private_key = auth_config["ssh_private_key"]
        self.ssh_user = auth_config["ssh_user"]
        self.ssh_control_path = ssh_control_path
        self.ssh_ip = None

    def get_default_ssh_options(self, connect_timeout):
        OPTS = [
            ("ConnectTimeout", "{}s".format(connect_timeout)),
            ("StrictHostKeyChecking", "no"),
            ("ControlMaster", "auto"),
            ("ControlPath", "{}/%C".format(self.ssh_control_path)),
            ("ControlPersist", "10s"),
            # Try fewer extraneous key pairs.
            ("IdentitiesOnly", "yes"),
            # Abort if port forwarding fails (instead of just printing to
            # stderr).
            ("ExitOnForwardFailure", "yes"),
            # Quickly kill the connection if network connection breaks (as
            # opposed to hanging/blocking).
            ("ServerAliveInterval", 5),
            ("ServerAliveCountMax", 3),
        ]

        return ["-i", self.ssh_private_key] + [
            x for y in (["-o", "{}={}".format(k, v)] for k, v in OPTS)
            for x in y
        ]

    def get_node_ip(self):
        raise DeprecationWarning("This is unused and can be deprecated?")

    def wait_for_ip(self, deadline):
        ip_getter = ProviderIpGetter(self.log_prefix, self.node_id, self.provider, self.use_internal_ip)
        return ip_getter.wait_for_ip(deadline=deadline)

    def set_ssh_ip_if_required(self):
        if self.ssh_ip is not None:
            return

        # We assume that this never changes.
        #   I think that's reasonable.
        deadline = time.time() + NODE_START_WAIT_S
        with LogTimer(self.log_prefix + "Got IP"):
            ip = self.wait_for_ip(deadline)
            assert ip is not None, "Unable to find IP of node"

        self.ssh_ip = ip

        # This should run before any SSH commands and therefore ensure that
        #   the ControlPath directory exists, allowing SSH to maintain
        #   persistent sessions later on.
        try:
            self.process_runner.check_call(
                ["mkdir", "-p", self.ssh_control_path])
        except subprocess.CalledProcessError as e:
            logger.warning(e)

        try:
            self.process_runner.check_call(
                ["chmod", "0700", self.ssh_control_path])
        except subprocess.CalledProcessError as e:
            logger.warning(self.log_prefix + str(e))

    def run(self,
            cmd,
            timeout=120,
            allocate_tty=False,
            exit_on_fail=False,
            port_forward=None,
            with_output=False):

        self.set_ssh_ip_if_required()

        ssh = ["ssh"]
        if allocate_tty:
            ssh.append("-tt")

        if port_forward:
            if not isinstance(port_forward, list):
                port_forward = [port_forward]
            for local, remote in port_forward:
                logger.info(self.log_prefix + "Forwarding " +
                            "{} -> localhost:{}".format(local, remote))
                ssh += ["-L", "{}:localhost:{}".format(remote, local)]

        final_cmd = ssh + self.get_default_ssh_options(timeout) + [
            "{}@{}".format(self.ssh_user, self.ssh_ip)
        ]
        if cmd:
            logger.info(self.log_prefix +
                        "Running {} on {}...".format(cmd, self.ssh_ip))
            final_cmd += with_interactive(cmd)
        else:
            # We do this because `-o ControlMaster` causes the `-N` flag to
            # still create an interactive shell in some ssh versions.
            final_cmd.append("while true; do sleep 86400; done")
        try:
            if with_output:
                return self.process_runner.check_output(final_cmd)
            else:
                self.process_runner.check_call(final_cmd)
        except subprocess.CalledProcessError:
            if exit_on_fail:
                quoted_cmd = " ".join(final_cmd[:-1] + [quote(final_cmd[-1])])
                raise click.ClickException(
                    "Command failed: \n\n  {}\n".format(quoted_cmd))
            else:
                raise

    def run_rsync_up(self, source, target):
        self.set_ssh_ip_if_required()
        self.process_runner.check_call([
            "rsync", "--rsh",
            " ".join(["ssh"] + self.get_default_ssh_options(120)), "-avz",
            source, "{}@{}:{}".format(self.ssh_user, self.ssh_ip, target)
        ])

    def run_rsync_down(self, source, target):
        self.set_ssh_ip_if_required()
        self.process_runner.check_call([
            "rsync", "--rsh",
            " ".join(["ssh"] + self.get_default_ssh_options(120)), "-avz",
            "{}@{}:{}".format(self.ssh_user, self.ssh_ip, source), target
        ])

    def remote_shell_command_str(self):
        return "ssh -o IdentitiesOnly=yes -i {} {}@{}\n".format(
            self.ssh_private_key, self.ssh_user, self.ssh_ip)


class NodeUpdater:
    """A process for syncing files and running init commands on a node."""

    def __init__(self,
                 node_id,
                 provider_config,
                 provider,
                 auth_config,
                 cluster_name,
                 file_mounts,
                 initialization_commands,
                 setup_commands,
                 ray_start_commands,
                 runtime_hash,
                 process_runner=subprocess,
                 use_internal_ip=False,
                 remove_node_from_known_hosts_before_use=True):
        """
        Args:
            remove_node_from_known_hosts_before_use (bool): If True, node ip will be removed from ssh-keygen known_hosts
                                                            before connecting in order to avoid man-in-the-middle
                                                            warnings
        """

        self.use_internal_ip = use_internal_ip
        self.log_prefix = "NodeUpdater: {}: ".format(node_id)
        if provider_config["type"] == "kubernetes":
            self.cmd_runner = KubernetesCommandRunner(
                self.log_prefix, provider.namespace, node_id, auth_config,
                process_runner)
        else:
            self.use_internal_ip = (self.use_internal_ip or provider_config.get(
                "use_internal_ips", False))
            self.cmd_runner = SSHCommandRunner(
                self.log_prefix, node_id, provider, auth_config, cluster_name,
                process_runner, self.use_internal_ip)

        self.daemon = True
        self.process_runner = process_runner
        self.node_id = node_id
        self.provider = provider
        self.file_mounts = {
            remote: os.path.expanduser(local)
            for remote, local in file_mounts.items()
        }
        self.initialization_commands = initialization_commands
        self.setup_commands = setup_commands
        self.ray_start_commands = ray_start_commands
        self.runtime_hash = runtime_hash
        self.remove_node_from_known_hosts_before_use = remove_node_from_known_hosts_before_use

    def run(self):
        logger.info(self.log_prefix +
                    "Updating to {}".format(self.runtime_hash))
        try:
            with LogTimer(self.log_prefix +
                          "Applied config {}".format(self.runtime_hash)):
                self.do_update()
        except Exception as e:
            error_str = str(e)
            if hasattr(e, "cmd"):
                error_str = "(Exit Status {}) {}".format(
                    e.returncode, " ".join(e.cmd))
            logger.error(self.log_prefix +
                         "Error updating {}".format(error_str))
            self.provider.set_node_tags(
                self.node_id, {TAG_RAY_NODE_STATUS: STATUS_UPDATE_FAILED})
            raise e

        self.provider.set_node_tags(
            self.node_id, {
                TAG_RAY_NODE_STATUS: STATUS_UP_TO_DATE,
                TAG_RAY_RUNTIME_CONFIG: self.runtime_hash
            })

        self.exitcode = 0

    def sync_file_mounts(self, sync_cmd):
        # Rsync file mounts
        for remote_path, local_path in self.file_mounts.items():
            assert os.path.exists(local_path), local_path
            if os.path.isdir(local_path):
                if not local_path.endswith("/"):
                    local_path += "/"
                if not remote_path.endswith("/"):
                    remote_path += "/"

            with LogTimer(self.log_prefix +
                          "Synced {} to {}".format(local_path, remote_path)):
                self.cmd_runner.run("mkdir -p {}".format(
                    os.path.dirname(remote_path)))
                sync_cmd(local_path, remote_path)

    def wait_ready(self, deadline):
        with LogTimer(self.log_prefix + "Got remote shell"):
            logger.info(self.log_prefix + "Waiting for remote shell...")

            while time.time() < deadline and \
                    not self.provider.is_terminated(self.node_id):
                try:
                    logger.debug(self.log_prefix +
                                 "Waiting for remote shell...")

                    self.cmd_runner.run("uptime", timeout=5)
                    logger.debug("Uptime succeeded.")
                    return True

                except Exception as e:
                    retry_str = str(e)
                    if hasattr(e, "cmd"):
                        retry_str = "(Exit Status {}): {}".format(
                            e.returncode, " ".join(e.cmd))
                    logger.debug(self.log_prefix +
                                 "Node not up, retrying: {}".format(retry_str))
                    time.sleep(READY_CHECK_INTERVAL)

        assert False, "Unable to connect to node"

    def do_update(self):
        if self.remove_node_from_known_hosts_before_use:
            self.remove_node_from_known_hosts()

        self.provider.set_node_tags(
            self.node_id, {TAG_RAY_NODE_STATUS: STATUS_WAITING_FOR_SSH})
        deadline = time.time() + NODE_START_WAIT_S
        self.wait_ready(deadline)

        node_tags = self.provider.node_tags(self.node_id)
        logger.debug("Node tags: {}".format(str(node_tags)))
        if node_tags.get(TAG_RAY_RUNTIME_CONFIG) == self.runtime_hash:
            logger.info(self.log_prefix +
                        "{} already up-to-date, skip to ray start".format(
                            self.node_id))
        else:
            self.provider.set_node_tags(
                self.node_id, {TAG_RAY_NODE_STATUS: STATUS_SYNCING_FILES})
            self.sync_file_mounts(self.rsync_up)

            # Run init commands
            self.provider.set_node_tags(
                self.node_id, {TAG_RAY_NODE_STATUS: STATUS_SETTING_UP})
            with LogTimer(self.log_prefix +
                          "Initialization commands completed"):
                for cmd in self.initialization_commands:
                    self.cmd_runner.run(cmd)

            with LogTimer(self.log_prefix + "Setup commands completed"):
                for cmd in self.setup_commands:
                    self.cmd_runner.run(cmd)

        with LogTimer(self.log_prefix + "Ray start commands completed"):
            for cmd in self.ray_start_commands:
                self.cmd_runner.run(cmd)

    def rsync_up(self, source, target):
        logger.info(self.log_prefix +
                    "Syncing {} to {}...".format(source, target))
        self.cmd_runner.run_rsync_up(source, target)

    def rsync_down(self, source, target):
        logger.info(self.log_prefix +
                    "Syncing {} from {}...".format(source, target))
        self.cmd_runner.run_rsync_down(source, target)

    def remove_node_from_known_hosts(self):
        logger.info(self.log_prefix + "Removing node IP from known_hosts to avoid man in middle warnings")
        logger.info(self.log_prefix + "Getting node ip for removal")
        node_ip = ProviderIpGetter(self.log_prefix, self.node_id, self.provider, self.use_internal_ip)\
            .wait_for_ip(timeout=NODE_START_WAIT_S)
        logger.info(self.log_prefix + "Found node ip {} for removal from known_hosts".format(node_ip))
        try:
            forget_known_host(node_ip)
        except TimeoutError as e:
            logger.info(self.log_prefix + "Unable to remove {} from ssh-keygen known_hosts - command timed out")
        except FileNotFoundError as e:
            logger.info(self.log_prefix + "Unable to remove {} from ssh-keygen known_hosts - ssh-keygen not found")


class NodeUpdaterThread(NodeUpdater, Thread):
    def __init__(self, *args, **kwargs):
        Thread.__init__(self)
        NodeUpdater.__init__(self, *args, **kwargs)
        self.exitcode = -1


class ProviderIpGetter:
    """Helper to get IP address from node_id from the cloud provider"""
    def __init__(self, log_prefix, node_id, provider, use_internal_ip):
        self.log_prefix = log_prefix
        self.node_id = node_id
        self.provider = provider
        self.use_internal_ip = use_internal_ip

    def wait_for_ip(self, timeout=None, deadline=None, sleep_between_gets=10):
        """
        Returns node IP address or None if it is not found.

        Can optionally be constrained to a timelimit, either by a timeout duration or
        an absolute deadline

        Args:
            timeout (int): Time in seconds to wait for the ip before returning None
            deadline (int): The absolute time (compared to time.time()) after which
                            the request is considered timed out and returns None
            sleep_between_gets (int): Time in integer seconds to sleep between attempts
                                      to get IP address

        Returns:
              (str or None): IP Address, or None if it cannot be obtained
        """
        if timeout and deadline:
            raise ValueError("At most one of timeout and deadline can be specified")
        else:
            if timeout:
                deadline = time.time() + timeout

        while time.time() < deadline and not self.provider.is_terminated(self.node_id):
            logger.info(self.log_prefix + "Waiting for IP...")
            ip = self.get_ip()
            if ip is not None:
                return ip
            time.sleep(sleep_between_gets)
        return None

    def get_ip(self):
        if self.use_internal_ip:
            return self.provider.internal_ip(self.node_id)
        else:
            return self.provider.external_ip(self.node_id)


def forget_known_host(host, timeout=3):
    subprocess.run(["ssh-keygen", "-R", host], timeout=timeout)
