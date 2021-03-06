# Copyright (c) 2019 Boston Dynamics, Inc.  All rights reserved.
#
# Downloading, reproducing, distributing or otherwise using the SDK Software
# is subject to the terms and conditions of the Boston Dynamics Software
# Development Kit License (20191101-BDSDK-SL).

"""Sdk is  a repository for settings typically common to a single developer and/or robot fleet."""
from __future__ import absolute_import
import glob
import logging
import os
import platform

import datetime
import jwt
import pkg_resources

from .auth import AuthClient
from .exceptions import Error
from .directory import DirectoryClient
from .estop import EstopClient
from .image import ImageClient
from .lease import LeaseClient
from .log_annotation import LogAnnotationClient
from .payload import PayloadClient
from .power import PowerClient
from .processors import AddRequestHeader
from .robot import Robot
from .robot_command import RobotCommandClient
from .robot_id import RobotIdClient
from .robot_state import RobotStateClient
from .spot_check import SpotCheckClient
from .time_sync import TimeSyncClient


class SdkError(Error):
    """General class of errors to handle non-response non-grpc errors."""


class UnsetAppTokenError(SdkError):
    """Path to app token not set."""


class UnableToLoadAppTokenError(SdkError):
    """Cannot load the provided app token path."""


_LOGGER = logging.getLogger(__name__)

BOSDYN_RESOURCE_ROOT = os.environ.get('BOSDYN_RESOURCE_ROOT',
                                      os.path.join(os.path.expanduser('~'), '.bosdyn'))


def generate_client_name(prefix=''):
    """Returns a descriptive client name for API clients with an optional prefix."""
    import __main__
    try:
        process_info = '{}-{}'.format(os.path.basename(__main__.__file__), os.getpid())
    except AttributeError:
        process_info = '{}'.format(os.getpid())
    machine_name = platform.node()
    if not machine_name:
        import getpass
        try:
            user_name = getpass.getuser()
        # pylint: disable=broad-except
        except Exception:
            _LOGGER.warn('Could not get username')
            user_name = '<unknown host>'
    # Use the name of the host if available, username otherwise.
    return '{}{}:{}'.format(prefix, machine_name or user_name, process_info)


_DEFAULT_SERVICE_CLIENTS = [
    AuthClient,
    DirectoryClient,
    EstopClient,
    ImageClient,
    LeaseClient,
    LogAnnotationClient,
    PayloadClient,
    PowerClient,
    RobotCommandClient,
    RobotIdClient,
    RobotStateClient,
    SpotCheckClient,
    TimeSyncClient,
]


def create_standard_sdk(client_name_prefix, service_clients=None, cert_resource_glob=None):
    """Return an Sdk with the most common configuration.

    Args:
        client_name_prefix -- prefix to pass to generate_client_name()
        service_clients -- List of service client classes to register in addition to the defaults.
        cert_resource_glob -- Glob expression matching robot certificate(s).
                              Default None to use distributed certificate.
    """
    _LOGGER.debug('Creating standard Sdk, cert glob: "%s"', cert_resource_glob)
    sdk = Sdk(name=client_name_prefix)
    client_name = generate_client_name(client_name_prefix)
    sdk.load_robot_cert(cert_resource_glob)
    sdk.request_processors.append(AddRequestHeader(lambda: client_name))

    all_service_clients = _DEFAULT_SERVICE_CLIENTS
    if service_clients:
        all_service_clients += service_clients
    for client in all_service_clients:
        sdk.register_service_client(client)
    return sdk


class Sdk(object):
    """Repository for settings typically common to a single developer and/or robot fleet.
    See also Robot for robot-specific settings.
    """

    def __init__(self, name=None):
        self.app_token = None
        self.cert = None
        self.logger = logging.getLogger(name or 'bosdyn.Sdk')
        self.request_processors = []
        self.response_processors = []
        self.request_processors = []
        self.service_client_factories_by_type = {}
        self.service_type_by_name = {}
        # Robots created by this Sdk, keyed by address.
        self.robots = {}

        #self.app_token_processor = None


    def create_robot(self, address, name=None):
        """Get a Robot initialized with this Sdk, creating it if it does not yet exist.

        Args:
            address -- Network-resolvable address of the robot, e.g. '192.168.80.3'
            name -- A unique identifier for the robot, e.g. 'My First Robot'. Default None to
                        use the address as the name.
        Returns:
            A Robot initialized with the current Sdk settings.
        """
        if self.app_token is None:
            raise UnsetAppTokenError
        if address in self.robots:
            return self.robots[address]
        robot = Robot(name=name or address)
        robot.address = address
        robot.update_from(self)
        self.robots[address] = robot
        return robot

    def register_service_client(self, creation_func, service_type=None, service_name=None):
        """Tell the Sdk how to create a specific type of service client.

        Args:
            creation_func -- Callable that returns a client. Typically just the class.
            service_type -- Type of the service. If None (default), will try to get the name from
                creation_func.
            service_name -- Name of the service. If None (default), will try to get the name from
                creation_func.
        """

        service_name = service_name or creation_func.default_service_name
        service_type = service_type or creation_func.service_type

        self.service_type_by_name[service_name] = service_type
        self.service_client_factories_by_type[service_type] = creation_func

    def load_robot_cert(self, resource_path_glob=None):
        """Load the SSL certificate for the robot.

        Args:
            resource_path_glob -- Optional path to certificate resource(s).
                If None, will load the certificate in the 'resources' package.
                Otherwise, should be a glob expression to match certificates.
                Defaults to None.
        """
        self.cert = None
        if resource_path_glob is None:
            self.cert = pkg_resources.resource_stream('bosdyn.client.resources', 'robot.pem').read()
        else:
            cert_paths = [c for c in glob.glob(resource_path_glob) if os.path.isfile(c)]
            if not cert_paths:
                raise IOError('No files matched "{}"'.format(resource_path_glob))
            self.cert = bytes()
            for cert_path in cert_paths:
                with open(cert_path, 'rb') as cert_file:
                    self.cert += cert_file.read()

    def load_app_token(self, resource_path):
        """Load an app token from a file, and set it on the SDK.

        Arguments:
            resource_path: Path to app token file on file system.

        Raises:
            UnsetAppTokenError: If resource_path is not set.
            UnableToLoadAppTokenError: If the file exists, but is unloadable.
        """
        if not resource_path:
            raise UnsetAppTokenError
        try:
            with open(os.path.expanduser(resource_path), 'rb') as token_file:
                token = token_file.read().decode().strip()
                log_token_time_remaining(token)
        except IOError as e:
            _LOGGER.exception(e)
            raise UnableToLoadAppTokenError(
                'Unable to retrieve app token from "{}".'.format(resource_path))
        except TypeError as e:
            _LOGGER.exception(e)
            raise UnsetAppTokenError

        self.app_token = token


def decode_token(token):
    """Decodes a JWT token without verification.

    Args:
        token: A string representing a token.

    Returns:
       Dictionary containing information about the token.
       Empty dictionary if failed to load token.

    Raises:
        UnableToLoadAppTokenError: If the token cannot be read.
    """
    try:
        values = jwt.decode(token, verify=False)
        return values
    except jwt.exceptions.DecodeError as e:
        raise UnableToLoadAppTokenError('Incorrectly formatted token {} --- {}'.format(token, e))
    except Exception as e:
        raise UnableToLoadAppTokenError('Problem decoding token {} --- {}'.format(token, e))


def log_token_time_remaining(token):
    """Log the time remaining until app token expires.

    Arguments:
        token: A jwt token
    
    Raises:
        UnableToLoadAppTokenError: If the token expiration information cannot be retrieved.
    """
    token_values = decode_token(token)
    if 'exp' not in token_values:
        raise UnableToLoadAppTokenError("Unknown token expiration")

    # Log time to expiration, with varying levels based on nearness.
    expire_time = datetime.datetime.fromtimestamp(token_values['exp'])
    time_to_expiration = expire_time - datetime.datetime.utcnow()
    if time_to_expiration < datetime.timedelta(seconds=0):
        _LOGGER.error('Your application token has expired. Please contact '
                      'support@bostondynamics.com to request a new token.')
    elif time_to_expiration <= datetime.timedelta(days=30):
        _LOGGER.warning('Application token expires in {} days on {}. Please contact '
                        'support@bostondynamics.com to request a new token before the '
                        'current token expires.'.format(
                            time_to_expiration.days,
                            datetime.datetime.strftime(expire_time, '%Y/%m/%d')))
    else:
        _LOGGER.info('Application token expires on {}.'.format(
            datetime.datetime.strftime(expire_time, '%Y/%m/%d')))
