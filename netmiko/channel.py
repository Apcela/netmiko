from os import path
import socket
import functools
import re
import time
from abc import ABC, abstractmethod
import telnetlib
import paramiko
import serial
from typing import Dict, Callable, Any, Optional, List
from typing import Type
from typing import TYPE_CHECKING

from netmiko import log
from netmiko.ssh_exception import (
    NetmikoTimeoutException,
    NetmikoAuthenticationException,
)
from netmiko.netmiko_globals import MAX_BUFFER
from netmiko.telnet_state import TelnetLogin
from netmiko.utilities import write_bytes, strip_ansi_escape_codes

if TYPE_CHECKING:
    from netmiko.session_log import SessionLog


def ansi_strip(func: Callable[..., str]) -> Callable[..., str]:
    @functools.wraps(func)
    def wrapper_decorator(self: object, *args: Any, **kwargs: Any) -> str:
        output: str
        output = func(self, *args, **kwargs)
        output = strip_ansi_escape_codes(output)
        return output

    return wrapper_decorator


def log_reads(func: Callable[..., str]) -> Callable[..., str]:
    """Handle both session_log and log of reads."""

    @functools.wraps(func)
    def wrapper_decorator(self: "Channel", *args: Any, **kwargs: Any) -> str:
        output: str
        output = func(self, *args, **kwargs)
        log.debug(f"read_channel: {output}")
        if self.session_log:
            self.session_log.write(output)
        return output

    return wrapper_decorator


def log_writes(func: Callable[..., None]) -> Callable[..., None]:
    """Handle both session_log and log of writes."""

    @functools.wraps(func)
    def wrapper_decorator(self: "Channel", out_data: str) -> None:
        func(self, out_data)
        try:
            log.debug(
                "write_channel: {}".format(
                    str(write_bytes(out_data, encoding=self.encoding))
                )
            )
            if self.session_log:
                if self.session_log.fin or self.session_log.record_writes:
                    self.session_log.write(out_data)
        except UnicodeDecodeError:
            # Don't log non-ASCII characters; this is null characters and telnet IAC (PY2)
            pass
        return None

    return wrapper_decorator


class SSHClient_noauth(paramiko.SSHClient):
    """Set noauth when manually handling SSH authentication."""

    def _auth(self, username: str, *args: Any, **kwargs: Any) -> None:
        self._transport.auth_none(username)
        return


class Channel(ABC):

    if TYPE_CHECKING:
        session_log: Optional["SessionLog"]
        encoding: str

    @abstractmethod
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    @abstractmethod
    def __repr__(self) -> str:
        pass

    @abstractmethod
    def establish_connection(self, width: int = 511, height: int = 1000) -> None:
        pass

    @abstractmethod
    def login(self) -> None:
        pass

    @abstractmethod
    def write_channel(self, out_data: str) -> None:
        pass

    @abstractmethod
    def read_buffer(self) -> str:
        pass

    @abstractmethod
    def read_channel(self) -> str:
        pass

    def read_channel_expect(
        self, pattern: str, timeout: int = 10, re_flags: int = 0
    ) -> str:
        """Read until pattern or timeout."""

        log.debug(f"Pattern is: {pattern}")
        loop_sleep_time = 0.01
        read_timeout = time.time() + timeout
        output = ""
        while True:
            output += self.read_buffer()
            if re.search(pattern, output, flags=re_flags):
                break
            elif time.time() > read_timeout:
                output = strip_ansi_escape_codes(output)
                msg = f"""

Timed-out reading channel, pattern not found in output: {pattern}
Data retrieved before timeout:\n\n{output}

"""
                raise NetmikoTimeoutException(msg)
            else:
                # Delay and then repeat the loop
                time.sleep(loop_sleep_time)

        output = strip_ansi_escape_codes(output)
        log.debug(f"Pattern found: {pattern} {output}")
        return output

    @abstractmethod
    def close(self) -> None:
        pass

    @abstractmethod
    def read_channel_timing(self, delay_factor: float, timeout: int) -> str:
        pass

    @abstractmethod
    def is_alive(self) -> bool:
        pass


class TelnetChannel(Channel):
    def __init__(
        self,
        telnet_params: Dict["str", Any],
        device_type: str,
        encoding: str = "ascii",
        session_log: Optional["SessionLog"] = None,
        login_class: Type[TelnetLogin] = TelnetLogin,
    ) -> None:
        self.protocol = "telnet"

        self.telnet_params = telnet_params
        self.device_type = device_type
        self.host = self.telnet_params["hostname"]
        self.port = self.telnet_params.get("port", 23)
        self.username = self.telnet_params["username"]
        self.password = self.telnet_params["password"]
        self.timeout = self.telnet_params["timeout"]

        self.encoding = encoding
        self.session_log = session_log

        self.login_class = login_class

        self.remote_conn: Optional[telnetlib.Telnet] = None

    def __repr__(self) -> str:
        return "TelnetChannel()"

    def establish_connection(self, width: int = 511, height: int = 1000) -> None:

        self.remote_conn = telnetlib.Telnet(
            self.host, port=self.port, timeout=self.timeout
        )
        self.login()

    def login(self, addl_patterns: Optional[List[Dict[str, str]]] = None) -> None:

        username_pattern = self.telnet_params["username_pattern"]
        password_pattern = self.telnet_params["pwd_pattern"]
        pri_prompt_terminator = self.telnet_params["pri_prompt_terminator"]
        alt_prompt_terminator = self.telnet_params["alt_prompt_terminator"]

        login = self.login_class(
            channel=self,
            username=self.username,
            password=self.password,
            username_pattern=username_pattern,
            password_pattern=password_pattern,
            pri_prompt_terminator=pri_prompt_terminator,
            alt_prompt_terminator=alt_prompt_terminator,
            login_timeout=self.timeout,
            addl_patterns=addl_patterns,
        )

        # Start the state machine login process
        login.start()

        # Cleanup the state machine
        del login

    def close(self) -> None:
        try:
            if self.remote_conn is not None:
                self.remote_conn.close()
        except Exception:
            # There have been race conditions observed on disconnect.
            pass
        finally:
            self.remote_conn = None

    @log_writes
    def write_channel(self, out_data: str) -> None:
        if self.remote_conn is not None:
            self.remote_conn.write(write_bytes(out_data, encoding=self.encoding))

    @log_reads
    def read_buffer(self) -> str:
        """
        Single read of available data. No sleeps.

        From telnetlib documentation on `read_eager`:
        ---
        Read readily available data.

        Raise EOFError if connection closed and no cooked data available. Return '' if no cooked
        data available otherwise. Do not block unless in the midst of an IAC sequence.
        """
        assert isinstance(self.remote_conn, telnetlib.Telnet)
        return self.remote_conn.read_eager().decode("utf-8", "ignore")

    @ansi_strip
    @log_reads
    def read_channel(self) -> str:
        """Read all of the available data from the telnet channel. No sleeps"""
        assert isinstance(self.remote_conn, telnetlib.Telnet)
        output = self.remote_conn.read_very_eager().decode("utf-8", "ignore")
        return output

    def read_channel_expect(
        self, pattern: str, timeout: int = 10, re_flags: int = 0
    ) -> str:
        """Read until pattern or timeout."""
        return super().read_channel_expect(
            pattern=pattern, timeout=timeout, re_flags=re_flags
        )

    def read_channel_timing(self, delay_factor: float, timeout: int) -> str:
        # FIX: needs implemented
        pass

    def is_alive(self) -> bool:
        """Returns a boolean flag with the state of the connection."""

        if self.remote_conn is None:
            log.error("Connection is not initialised, is_alive returns False")
            return False

        try:
            # Try sending IAC + NOP (IAC is telnet way of sending command)
            # IAC = Interpret as Command; it comes before the NOP.
            log.debug("Sending IAC + NOP")
            telnet_socket = self.remote_conn.get_socket()
            # Need to send multiple times to test connection
            telnet_socket.sendall(telnetlib.IAC + telnetlib.NOP)
            telnet_socket.sendall(telnetlib.IAC + telnetlib.NOP)
            telnet_socket.sendall(telnetlib.IAC + telnetlib.NOP)
            return True
        except AttributeError:
            return False

        return False


class SSHChannel(Channel):
    def __init__(
        self,
        ssh_params: Dict["str", Any],
        device_type: str,
        ssh_hostkey_args: Optional[Dict["str", Any]] = None,
        encoding: str = "ascii",
        session_log: Optional["SessionLog"] = None,
        use_keys: bool = False,  # needed for making decision to use no_auth in some subclasses
        password: Optional[
            str
        ] = None,  # needed for making decision to use no_auth in some subclasses
    ) -> None:
        self.ssh_params = ssh_params
        self.blocking_timeout = ssh_params.pop("blocking_timeout", 20)
        self.keepalive = ssh_params.pop("keepalive", 0)
        self.device_type = device_type
        if ssh_hostkey_args is None:
            self.ssh_hostkey_args = {}
        else:
            self.ssh_hostkey_args = ssh_hostkey_args
        self.protocol = "ssh"
        self.remote_conn = None
        self.session_log = session_log
        self.encoding = encoding
        self.use_keys = use_keys
        self.password = password

    def __repr__(self) -> str:
        return "SSHChannel(ssh_params)"

    def _build_ssh_client(self, no_auth: bool = False) -> paramiko.SSHClient:
        """Prepare for Paramiko SSH connection."""
        # Create instance of SSHClient object

        # 'no_auth' is usually because a device has a non-standard auth mechanism
        # that needs handled directly by Netmiko.
        if no_auth:
            remote_conn_pre = SSHClient_noauth()
        else:
            remote_conn_pre = paramiko.SSHClient()

        # Load host_keys for better SSH security
        if self.ssh_hostkey_args.get("system_host_keys"):
            remote_conn_pre.load_system_host_keys()
        if self.ssh_hostkey_args.get("alt_host_keys"):
            alt_key_file = self.ssh_hostkey_args["alt_key_file"]
            if path.isfile(alt_key_file):
                remote_conn_pre.load_host_keys(alt_key_file)

        # Default is to automatically add untrusted hosts (make sure appropriate for your env)
        if not self.ssh_hostkey_args.get("ssh_strict", False):
            key_policy = paramiko.AutoAddPolicy()
        else:
            key_policy = paramiko.RejectPolicy()

        remote_conn_pre.set_missing_host_key_policy(key_policy)
        return remote_conn_pre

    def establish_connection(self, width: int = 511, height: int = 1000) -> None:
        self.remote_conn_pre = self._build_ssh_client()
        host = self.ssh_params.get("hostname")
        port = self.ssh_params.get("port", 22)

        # initiate SSH connection
        try:
            self.remote_conn_pre.connect(**self.ssh_params)
        except socket.error as conn_error:
            self.close()
            msg = f"""TCP connection to device failed.

Common causes of this problem are:
1. Incorrect hostname or IP address.
2. Wrong TCP port.
3. Intermediate firewall blocking access.

Device settings: {self.device_type} {host}:{port}

"""

            # Handle DNS failures separately
            if "Name or service not known" in str(conn_error):
                msg = (
                    f"DNS failure--the hostname you provided was not resolvable "
                    f"in DNS: {host}:{port}"
                )

            msg = msg.lstrip()
            raise NetmikoTimeoutException(msg)
        except paramiko.ssh_exception.SSHException as no_session_err:
            self.close()
            if "No existing session" in str(no_session_err):
                msg = (
                    "Paramiko: 'No existing session' error: "
                    "try increasing 'conn_timeout' to 10 seconds or larger."
                )
                raise NetmikoTimeoutException(msg)
            else:
                raise
        except paramiko.ssh_exception.AuthenticationException as auth_err:
            self.close()
            msg = f"""Authentication to device failed.

Common causes of this problem are:
1. Invalid username and password
2. Incorrect SSH-key file
3. Connecting to the wrong device

Device settings: {self.device_type} {host}:{port}

"""

            msg += "\n" + str(auth_err)
            raise NetmikoAuthenticationException(msg)

        # Use invoke_shell to establish an 'interactive session'
        self.remote_conn = self.remote_conn_pre.invoke_shell(
            term="vt100", width=width, height=height
        )

        assert isinstance(self.remote_conn, paramiko.channel.Channel)
        self.remote_conn.settimeout(self.blocking_timeout)
        if self.keepalive:
            self.remote_conn.transport.set_keepalive(self.keepalive)

    def close(self) -> None:
        """Cleanup Paramiko to try to gracefully handle SSH session ending."""
        try:
            self.remote_conn_pre.close()
        # There have been race conditions observed on disconnect.
        finally:
            del self.remote_conn
            del self.remote_conn_pre

    def login(self) -> None:
        """Generally not used with SSH."""
        return

    @log_writes
    def write_channel(self, out_data: str) -> None:
        if self.remote_conn is not None:
            self.remote_conn.sendall(write_bytes(out_data, encoding=self.encoding))

    @log_reads
    def read_buffer(self) -> str:
        """Single read of available data. No sleeps."""
        output = ""
        if self.remote_conn is not None and self.remote_conn.recv_ready():
            outbuf = self.remote_conn.recv(MAX_BUFFER)
            if len(outbuf) == 0:
                raise EOFError("Channel stream closed by remote device.")
            output += outbuf.decode("utf-8", "ignore")
        return output

    @ansi_strip
    def read_channel(self) -> str:
        """Read all of the available data from the SSH channel. No sleeps."""
        output = ""
        while True:
            new_output = self.read_buffer()
            output += new_output
            if new_output == "":
                break
        return output

    def read_channel_expect(
        self, pattern: str, timeout: int = 10, re_flags: int = 0
    ) -> str:
        """Read until pattern or timeout."""
        return super().read_channel_expect(
            pattern=pattern, timeout=timeout, re_flags=re_flags
        )

    def read_channel_timing(self, delay_factor: float = 1.0, timeout: int = 10) -> str:
        """
        Read data on the channel based on timing delays.

        This is really a network device specific behavior where we are trying to guess when we
        are done based on:
        1. We have read some data.
        2. And there is no more present after some amount of waiting.
        3. Will completely give up after timeout seconds regardless of whether there is more data
           or not.
        """
        # Time to sleep in each read loop
        loop_sleep_time = 0.01
        final_delay = 2
        read_timeout = time.time() + timeout

        channel_data = ""
        while True:
            time.sleep(loop_sleep_time * delay_factor)
            new_data = self.read_channel()
            if new_data:
                channel_data += new_data
            else:
                # Safeguard to make sure really done
                time.sleep(final_delay * delay_factor)
                new_data = self.read_channel()
                if not new_data:
                    break
                else:
                    channel_data += new_data

            if time.time() > read_timeout:
                # FIX: should it raise an exception or return incomplete data
                # Probably should raise an exception and recommend they increase
                # the timeout.
                break
        return channel_data

    def is_alive(self) -> bool:
        """Returns a boolean flag with the state of the connection."""
        null = chr(0)
        if self.remote_conn is None:
            log.error("Connection is not initialised, is_alive returns False")
            return False

        try:
            # Try sending ASCII null byte to maintain the connection alive
            log.debug("Sending the NULL byte")
            self.write_channel(null)
            return self.remote_conn.transport.is_active()
        except (socket.error, EOFError):
            log.error("Unable to send", exc_info=True)
            # If unable to send, we can tell for sure that the connection is unusable
            return False

        return False


class SerialChannel(Channel):
    def __init__(
        self,
        serial_settings: Dict["str", Any],
        device_type: str,
        encoding: str = "ascii",
        session_log: Optional["SessionLog"] = None,
    ) -> None:
        self.protocol = "serial"
        self.serial_settings = serial_settings
        self.device_type = device_type
        self.remote_conn = None
        self.encoding = encoding
        self.session_log = session_log

    def __repr__(self) -> str:
        return "SerialChannel()"

    def establish_connection(self, width: int = 511, height: int = 1000) -> None:
        self.remote_conn = serial.Serial(**self.serial_settings)
        self.login()

    def login(self) -> None:
        # FIX: Needs implemented
        pass

    def close(self) -> None:
        try:
            if self.remote_conn is not None:
                self.remote_conn.close()
        finally:
            # There have been race conditions observed on disconnect.
            self.remote_conn = None

    @log_writes
    def write_channel(self, out_data: str) -> None:
        if self.remote_conn is not None:
            self.remote_conn.write(write_bytes(out_data, encoding=self.encoding))
            self.remote_conn.flush()

    def read_buffer(self) -> str:
        # FIX: needs implemented
        return ""

    @ansi_strip
    @log_reads
    def read_channel(self) -> str:
        """Read all of the available data from the serial channel."""
        output = ""
        if self.remote_conn is None:
            return output
        while self.remote_conn.in_waiting > 0:
            output += self.remote_conn.read(self.remote_conn.in_waiting).decode(
                "utf-8", "ignore"
            )
        return output

    def read_channel_expect(
        self, pattern: str, timeout: int = 10, re_flags: int = 0
    ) -> str:
        # FIX: needs implemented
        pass

    def read_channel_timing(self, delay_factor: float, timeout: int) -> str:
        # FIX: needs implemented
        pass

    def is_alive(self) -> bool:
        # FIX: needs implemented
        pass
