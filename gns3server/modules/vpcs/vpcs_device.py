# -*- coding: utf-8 -*-
#
# Copyright (C) 2014 GNS3 Technologies Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
vpcs device management (creates command line, processes, files etc.) in
order to run an vpcs instance.
"""

import os
import re
import signal
import subprocess
import argparse
import threading
import configparser
import sys
import socket
from .vpcs_error import vpcsError
from .adapters.ethernet_adapter import EthernetAdapter
from .nios.nio_udp import NIO_UDP
from .nios.nio_tap import NIO_TAP

import logging
log = logging.getLogger(__name__)


class vpcsDevice(object):
    """
    vpcs device implementation.

    :param path: path to vpcs executable
    :param working_dir: path to a working directory
    :param host: host/address to bind for console and UDP connections
    :param name: name of this vpcs device
    """

    _instances = []

    def __init__(self, path, working_dir, host="127.0.0.1", name=None):

        # find an instance identifier (1 <= id <= 255)
        # This 255 limit is due to a restriction on the number of possible
        # mac addresses given in vpcs using the -m option
        self._id = 0
        for identifier in range(1, 256):
            if identifier not in self._instances:
                self._id = identifier
                self._instances.append(self._id)
                break

        if self._id == 0:
            raise vpcsError("Maximum number of vpcs instances reached")

        if name:
            self._name = name
        else:
            self._name = "vpcs{}".format(self._id)
        self._path = path
        self._console = None
        self._working_dir = None
        self._command = []
        self._process = None
        self._vpcs_stdout_file = ""
        self._host = "127.0.0.1"
        self._started = False

        # vpcs settings
        self._script_file = ""
        self._ethernet_adapters = [EthernetAdapter()]  # one adapter = 1 interfaces
        self._slots = self._ethernet_adapters
        
        # update the working directory
        self.working_dir = working_dir

        log.info("vpcs device {name} [id={id}] has been created".format(name=self._name,
                                                                       id=self._id))

    def defaults(self):
        """
        Returns all the default attribute values for vpcs.

        :returns: default values (dictionary)
        """

        vpcs_defaults = {"name": self._name,
                        "path": self._path,
                        "script_file": self._script_file,
                        "console": self._console}

        return vpcs_defaults

    @property
    def id(self):
        """
        Returns the unique ID for this vpcs device.

        :returns: id (integer)
        """

        return(self._id)

    @classmethod
    def reset(cls):
        """
        Resets allocated instance list.
        """

        cls._instances.clear()

    @property
    def name(self):
        """
        Returns the name of this vpcs device.

        :returns: name
        """

        return self._name

    @name.setter
    def name(self, new_name):
        """
        Sets the name of this vpcs device.

        :param new_name: name
        """

        self._name = new_name
        log.info("vpcs {name} [id={id}]: renamed to {new_name}".format(name=self._name,
                                                                      id=self._id,
                                                                      new_name=new_name))

    @property
    def path(self):
        """
        Returns the path to the vpcs executable.

        :returns: path to vpcs
        """

        return(self._path)

    @path.setter
    def path(self, path):
        """
        Sets the path to the vpcs executable.

        :param path: path to vpcs
        """

        self._path = path
        log.info("vpcs {name} [id={id}]: path changed to {path}".format(name=self._name,
                                                                      id=self._id,
                                                                      path=path))

    @property
    def working_dir(self):
        """
        Returns current working directory

        :returns: path to the working directory
        """

        return self._working_dir

    @working_dir.setter
    def working_dir(self, working_dir):
        """
        Sets the working directory for vpcs.

        :param working_dir: path to the working directory
        """

        # create our own working directory
        working_dir = os.path.join(working_dir, "vpcs", "device-{}".format(self._id))
        try:
            os.makedirs(working_dir)
        except FileExistsError:
            pass
        except OSError as e:
            raise vpcsError("Could not create working directory {}: {}".format(working_dir, e))

        self._working_dir = working_dir
        log.info("vpcs {name} [id={id}]: working directory changed to {wd}".format(name=self._name,
                                                                                    id=self._id,
                                                                                    wd=self._working_dir))

    @property
    def console(self):
        """
        Returns the TCP console port.

        :returns: console port (integer)
        """

        return self._console

    @console.setter
    def console(self, console):
        """
        Sets the TCP console port.

        :param console: console port (integer)
        """

        self._console = console
        log.info("vpcs {name} [id={id}]: console port set to {port}".format(name=self._name,
                                                                         id=self._id,
                                                                         port=console))

    def command(self):
        """
        Returns the vpcs command line.

        :returns: vpcs command line (string)
        """

        return " ".join(self._build_command())

    def delete(self):
        """
        Deletes this vpcs device.
        """

        self.stop()
        self._instances.remove(self._id)
        log.info("vpcs device {name} [id={id}] has been deleted".format(name=self._name,
                                                                       id=self._id))

    @property
    def started(self):
        """
        Returns either this vpcs device has been started or not.

        :returns: boolean
        """

        return self._started

    def start(self):
        """
        Starts the vpcs process.
        """

        if not self.is_running():

            if not os.path.isfile(self._path):
                raise vpcsError("vpcs image '{}' is not accessible".format(self._path))

            if not os.access(self._path, os.X_OK):
                raise vpcsError("vpcs image '{}' is not executable".format(self._path))

            self._command = self._build_command()
            try:
                log.info("starting vpcs: {}".format(self._command))
                self._vpcs_stdout_file = os.path.join(self._working_dir, "vpcs.log")
                log.info("logging to {}".format(self._vpcs_stdout_file))
                with open(self._vpcs_stdout_file, "w") as fd:
                    self._process = subprocess.Popen(self._command,
                                                     stdout=fd,
                                                     stderr=subprocess.STDOUT,
                                                     cwd=self._working_dir)
                log.info("vpcs instance {} started PID={}".format(self._id, self._process.pid))
                self._started = True
            except OSError as e:
                vpcs_stdout = self.read_vpcs_stdout()
                log.error("could not start vpcs {}: {}\n{}".format(self._path, e, vpcs_stdout))
                raise vpcsError("could not start vpcs {}: {}\n{}".format(self._path, e, vpcs_stdout))

    def stop(self):
        """
        Stops the vpcs process.
        """

        # stop the vpcs process
        if self.is_running():
            log.info("stopping vpcs instance {} PID={}".format(self._id, self._process.pid))
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.connect((self._host, self._console))
                sock.send(bytes("quit\n", 'UTF-8')) 
                sock.close() 
            except TypeError as e:
                log.warn("vpcs instance {} PID={} is still running.  Error: {}".format(self._id,
                                                                          self._process.pid, e))
        self._process = None
        self._started = False


    def read_vpcs_stdout(self):
        """
        Reads the standard output of the vpcs process.
        Only use when the process has been stopped or has crashed.
        """

        output = ""
        if self._vpcs_stdout_file:
            try:
                with open(self._vpcs_stdout_file) as file:
                    output = file.read()
            except OSError as e:
                log.warn("could not read {}: {}".format(self._vpcs_stdout_file, e))
        return output

    def is_running(self):
        """
        Checks if the vpcs process is running

        :returns: True or False
        """

        if self._process:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.connect((self._host, self._console))
                sock.close() 
                return True
            except:
                e = sys.exc_info()[0]
                log.warn("Could not connect to {}:{}.  Error: {}".format(self._host, self._console, e))
                return False
        return False


    def slot_add_nio_binding(self, slot_id, port_id, nio):
        """
        Adds a slot NIO binding.

        :param slot_id: slot ID
        :param port_id: port ID
        :param nio: NIO instance to add to the slot/port
        """

        try:
            adapter = self._slots[slot_id]
        except IndexError:
            raise vpcsError("Slot {slot_id} doesn't exist on vpcs {name}".format(name=self._name,
                                                                               slot_id=slot_id))

        if not adapter.port_exists(port_id):
            raise vpcsError("Port {port_id} doesn't exist in adapter {adapter}".format(adapter=adapter,
                                                                                      port_id=port_id))

        adapter.add_nio(port_id, nio)
        log.info("vpcs {name} [id={id}]: {nio} added to {slot_id}/{port_id}".format(name=self._name,
                                                                                   id=self._id,
                                                                                   nio=nio,
                                                                                   slot_id=slot_id,
                                                                                   port_id=port_id))

    def slot_remove_nio_binding(self, slot_id, port_id):
        """
        Removes a slot NIO binding.

        :param slot_id: slot ID
        :param port_id: port ID
        """

        try:
            adapter = self._slots[slot_id]
        except IndexError:
            raise vpcsError("Slot {slot_id} doesn't exist on vpcs {name}".format(name=self._name,
                                                                               slot_id=slot_id))

        if not adapter.port_exists(port_id):
            raise vpcsError("Port {port_id} doesn't exist in adapter {adapter}".format(adapter=adapter,
                                                                                      port_id=port_id))

        nio = adapter.get_nio(port_id)
        adapter.remove_nio(port_id)
        log.info("vpcs {name} [id={id}]: {nio} removed from {slot_id}/{port_id}".format(name=self._name,
                                                                                       id=self._id,
                                                                                       nio=nio,
                                                                                       slot_id=slot_id,
                                                                                       port_id=port_id))

    def _build_command(self):
        """
        Command to start the vpcs process.
        (to be passed to subprocess.Popen())

        vpcs command line:
        usage: vpcs [options] [scriptfile]
        Option:
            -h         print this help then exit
            -v         print version information then exit

            -p port    run as a daemon listening on the tcp 'port'
            -m num     start byte of ether address, default from 0
            -r file    load and execute script file
                       compatible with older versions, DEPRECATED.

            -e         tap mode, using /dev/tapx (linux only)
            -u         udp mode, default

        udp mode options:
            -s port    local udp base port, default from 20000
            -c port    remote udp base port (dynamips udp port), default from 30000
            -t ip      remote host IP, default 127.0.0.1

        hypervisor mode option:
            -H port    run as the hypervisor listening on the tcp 'port'

          If no 'scriptfile' specified, vpcs will read and execute the file named
          'startup.vpc' if it exsits in the current directory.

        """

        command = [self._path]
        command.extend(["-p", str(self._console)])
        
        for adapter in self._slots:
            for unit in adapter.ports.keys():
                nio = adapter.get_nio(unit)
                if nio:
                    if isinstance(nio, NIO_UDP):
                        # UDP tunnel
                        command.extend(["-s", str(nio.lport)])
                        command.extend(["-c", str(nio.rport)])
                        command.extend(["-t", str(nio.rhost)])
                    
                    elif isinstance(nio, NIO_TAP):
                        # TAP interface
                        command.extend(["-e"]) #, str(nio.tap_device)])  #TODO: Fix, currently vpcs doesn't allow specific tap_device
                                                
        command.extend(["-m", str(self._id)]) #The unique ID is used to set the mac address offset
        command.extend(["-i", str(1)]) #Option to start only one pc instance
        if self._script_file:
            command.extend([self._script_file])
        return command

    @property
    def script_file(self):
        """
        Returns the script-file for this vpcs instance.

        :returns: path to script-file file
        """

        return self._script_file

    @script_file.setter
    def script_file(self, script_file):
        """
        Sets the script-file for this vpcs instance.

        :param script_file: path to script-file file
        """

        self._script_file = script_file
        log.info("vpcs {name} [id={id}]: script_file set to {config}".format(name=self._name,
                                                                                 id=self._id,
                                                                                 config=self._script_file))


