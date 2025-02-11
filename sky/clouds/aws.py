"""Amazon Web Services."""

# pylint: disable=import-outside-toplevel

import json
import os
import subprocess
import typing
from typing import Dict, Iterator, List, Optional, Tuple

from sky import clouds
from sky import exceptions
from sky.adaptors import aws
from sky.clouds import service_catalog
from sky.utils import common_utils
from sky.utils import ux_utils

if typing.TYPE_CHECKING:
    # renaming to avoid shadowing variables
    from sky import resources as resources_lib

# Minimum set of files under ~/.aws that grant AWS access.
_CREDENTIAL_FILES = [
    'credentials',
]


def _run_output(cmd):
    proc = subprocess.run(cmd,
                          shell=True,
                          check=True,
                          stderr=subprocess.PIPE,
                          stdout=subprocess.PIPE)
    return proc.stdout.decode('ascii')


# TODO(zhwu): Move the default AMI size to the catalog instead.
DEFAULT_AMI_GB = 45


@clouds.CLOUD_REGISTRY.register
class AWS(clouds.Cloud):
    """Amazon Web Services."""

    _REPR = 'AWS'
    _regions: List[clouds.Region] = []

    _STATIC_CREDENTIAL_HELP_STR = (
        'Run the following commands:'
        '\n      $ pip install boto3'
        '\n      $ aws configure'
        '\n    For more info: '
        'https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-quickstart.html'  # pylint: disable=line-too-long
    )

    _SSO_CREDENTIAL_HELP_STR = (
        'Run the following commands (must use aws v2 CLI):'
        '\n      $ aws configure sso'
        '\n      $ aws sso login --profile <profile_name>'
        '\n    For more info: '
        'https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html'  # pylint: disable=line-too-long
    )

    #### Regions/Zones ####

    @classmethod
    def regions(cls):
        if not cls._regions:
            # https://aws.amazon.com/premiumsupport/knowledge-center/vpc-find-availability-zone-options/
            cls._regions = [
                clouds.Region('us-west-1').set_zones([
                    clouds.Zone('us-west-1a'),
                    clouds.Zone('us-west-1b'),
                ]),
                clouds.Region('us-west-2').set_zones([
                    clouds.Zone('us-west-2a'),
                    clouds.Zone('us-west-2b'),
                    clouds.Zone('us-west-2c'),
                    clouds.Zone('us-west-2d'),
                ]),
                clouds.Region('us-east-2').set_zones([
                    clouds.Zone('us-east-2a'),
                    clouds.Zone('us-east-2b'),
                    clouds.Zone('us-east-2c'),
                ]),
                clouds.Region('us-east-1').set_zones([
                    clouds.Zone('us-east-1a'),
                    clouds.Zone('us-east-1b'),
                    clouds.Zone('us-east-1c'),
                    clouds.Zone('us-east-1d'),
                    clouds.Zone('us-east-1e'),
                    clouds.Zone('us-east-1f'),
                ]),
            ]
        return cls._regions

    @classmethod
    def region_zones_provision_loop(
        cls,
        *,
        instance_type: Optional[str] = None,
        accelerators: Optional[Dict[str, int]] = None,
        use_spot: bool,
    ) -> Iterator[Tuple[clouds.Region, List[clouds.Zone]]]:
        # AWS provisioner can handle batched requests, so yield all zones under
        # each region.
        del accelerators  # unused

        if instance_type is None:
            # fallback to manually specified region/zones
            regions = cls.regions()
        else:
            regions = service_catalog.get_region_zones_for_instance_type(
                instance_type, use_spot, 'aws')
        for region in regions:
            yield region, region.zones

    @classmethod
    def get_default_ami(cls, region_name: str, instance_type: str) -> str:
        acc = cls.get_accelerators_from_instance_type(instance_type)
        image_id = service_catalog.get_image_id_from_tag(
            'skypilot:gpu-ubuntu-2004', region_name, clouds='aws')
        if acc is not None:
            assert len(acc) == 1, acc
            acc_name = list(acc.keys())[0]
            if acc_name == 'K80':
                image_id = service_catalog.get_image_id_from_tag(
                    'skypilot:k80-ubuntu-2004', region_name, clouds='aws')
        if image_id is not None:
            return image_id
        # Raise ResourcesUnavailableError to make sure the failover in
        # CloudVMRayBackend will be correctly triggered.
        # TODO(zhwu): This is a information leakage to the cloud implementor,
        # we need to find a better way to handle this.
        raise exceptions.ResourcesUnavailableError(
            'No image found in catalog for region '
            f'{region_name}. Try setting a valid image_id.')

    @classmethod
    def _get_image_id(
        cls,
        image_id: Optional[Dict[str, str]],
        region_name: str,
    ) -> str:
        if image_id is None:
            return None
        if None in image_id:
            image_id = image_id[None]
        else:
            assert region_name in image_id, image_id
            image_id = image_id[region_name]
        if image_id.startswith('skypilot:'):
            image_id = service_catalog.get_image_id_from_tag(image_id,
                                                             region_name,
                                                             clouds='aws')
            if image_id is None:
                # Raise ResourcesUnavailableError to make sure the failover
                # in CloudVMRayBackend will be correctly triggered.
                # TODO(zhwu): This is a information leakage to the cloud
                # implementor, we need to find a better way to handle this.
                raise exceptions.ResourcesUnavailableError(
                    f'No image found for region {region_name}')
        return image_id

    def get_image_size(self, image_id: str, region: Optional[str]) -> float:
        if image_id.startswith('skypilot:'):
            return DEFAULT_AMI_GB
        assert region is not None, (image_id, region)
        client = aws.client('ec2', region_name=region)
        try:
            image_info = client.describe_images(ImageIds=[image_id])
            image_info = image_info['Images'][0]
            image_size = image_info['BlockDeviceMappings'][0]['Ebs'][
                'VolumeSize']
        except aws.botocore_exceptions().NoCredentialsError:
            # Fallback to default image size if no credentials are available.
            # The credentials issue will be caught when actually provisioning
            # the instance and appropriate errors will be raised there.
            return DEFAULT_AMI_GB
        except aws.botocore_exceptions().ClientError:
            with ux_utils.print_exception_no_traceback():
                raise ValueError(f'Image {image_id!r} not found in '
                                 f'AWS region {region}') from None
        return image_size

    @classmethod
    def get_zone_shell_cmd(cls) -> Optional[str]:
        # The command for getting the current zone is from:
        # https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/instance-identity-documents.html  # pylint: disable=line-too-long
        command_str = (
            'curl -s http://169.254.169.254/latest/dynamic/instance-identity/document'  # pylint: disable=line-too-long
            ' | python3 -u -c "import sys, json; '
            'print(json.load(sys.stdin)[\'availabilityZone\'])"')
        return command_str

    #### Normal methods ####

    def instance_type_to_hourly_cost(self, instance_type: str, use_spot: bool):
        return service_catalog.get_hourly_cost(instance_type,
                                               region=None,
                                               use_spot=use_spot,
                                               clouds='aws')

    def accelerators_to_hourly_cost(self, accelerators,
                                    use_spot: bool) -> float:
        # AWS includes accelerators as part of the instance type.  Implementing
        # this is also necessary for e.g., the instance may have 4 GPUs, while
        # the task specifies to use 1 GPU.
        return 0

    def get_egress_cost(self, num_gigabytes: float):
        # In general, query this from the cloud:
        #   https://aws.amazon.com/s3/pricing/
        # NOTE: egress from US East (Ohio).
        # NOTE: Not accurate as the pricing tier is based on cumulative monthly
        # usage.
        if num_gigabytes > 150 * 1024:
            return 0.05 * num_gigabytes
        cost = 0.0
        if num_gigabytes >= 50 * 1024:
            cost += (num_gigabytes - 50 * 1024) * 0.07
            num_gigabytes -= 50 * 1024

        if num_gigabytes >= 10 * 1024:
            cost += (num_gigabytes - 10 * 1024) * 0.085
            num_gigabytes -= 10 * 1024

        if num_gigabytes > 1:
            cost += (num_gigabytes - 1) * 0.09

        cost += 0.0
        return cost

    def is_same_cloud(self, other: clouds.Cloud):
        return isinstance(other, AWS)

    @classmethod
    def get_default_instance_type(cls) -> str:
        # 8 vCpus, 32 GB RAM. 3rd generation Intel Xeon. General Purpose.
        return 'm6i.2xlarge'

    # TODO: factor the following three methods, as they are the same logic
    # between Azure and AWS.
    @classmethod
    def get_accelerators_from_instance_type(
        cls,
        instance_type: str,
    ) -> Optional[Dict[str, int]]:
        return service_catalog.get_accelerators_from_instance_type(
            instance_type, clouds='aws')

    @classmethod
    def get_vcpus_from_instance_type(
        cls,
        instance_type: str,
    ) -> float:
        return service_catalog.get_vcpus_from_instance_type(instance_type,
                                                            clouds='aws')

    def make_deploy_resources_variables(
            self, resources: 'resources_lib.Resources',
            region: Optional['clouds.Region'],
            zones: Optional[List['clouds.Zone']]) -> Dict[str, str]:
        if region is None:
            assert zones is None, (
                'Set either both or neither for: region, zones.')
            region = self._get_default_region()
            zones = region.zones
        else:
            assert zones is not None, (
                'Set either both or neither for: region, zones.')

        region_name = region.name
        zones = [zone.name for zone in zones]

        r = resources
        # r.accelerators is cleared but .instance_type encodes the info.
        acc_dict = self.get_accelerators_from_instance_type(r.instance_type)
        if acc_dict is not None:
            custom_resources = json.dumps(acc_dict, separators=(',', ':'))
        else:
            custom_resources = None

        image_id = self._get_image_id(r.image_id, region_name)
        if image_id is None:
            image_id = self.get_default_ami(region_name, r.instance_type)

        return {
            'instance_type': r.instance_type,
            'custom_resources': custom_resources,
            'use_spot': r.use_spot,
            'region': region_name,
            'zones': ','.join(zones),
            'image_id': image_id,
        }

    def get_feasible_launchable_resources(self,
                                          resources: 'resources_lib.Resources'):
        fuzzy_candidate_list = []
        if resources.instance_type is not None:
            assert resources.is_launchable(), resources
            # Treat Resources(AWS, p3.2x, V100) as Resources(AWS, p3.2x).
            resources = resources.copy(accelerators=None)
            return ([resources], fuzzy_candidate_list)

        def _make(instance_list):
            resource_list = []
            for instance_type in instance_list:
                r = resources.copy(
                    cloud=AWS(),
                    instance_type=instance_type,
                    # Setting this to None as AWS doesn't separately bill /
                    # attach the accelerators.  Billed as part of the VM type.
                    accelerators=None,
                )
                resource_list.append(r)
            return resource_list

        # Currently, handle a filter on accelerators only.
        accelerators = resources.accelerators
        if accelerators is None:
            # No requirements to filter, so just return a default VM type.
            return (_make([AWS.get_default_instance_type()]),
                    fuzzy_candidate_list)

        assert len(accelerators) == 1, resources
        acc, acc_count = list(accelerators.items())[0]
        (instance_list, fuzzy_candidate_list
        ) = service_catalog.get_instance_type_for_accelerator(acc,
                                                              acc_count,
                                                              clouds='aws')
        if instance_list is None:
            return ([], fuzzy_candidate_list)
        return (_make(instance_list), fuzzy_candidate_list)

    def check_credentials(self) -> Tuple[bool, Optional[str]]:
        """Checks if the user has access credentials to this cloud."""
        try:
            # pylint: disable=top-level-import-outside-toplevel,unused-import
            import boto3
            import botocore
        except ImportError:
            raise ImportError('Fail to import dependencies for AWS.'
                              'Try pip install "skypilot[aws]"') from None
        # This file is required because it will be synced to remote VMs for
        # `aws` to access private storage buckets.
        # `aws configure list` does not guarantee this file exists.
        if not os.path.isfile(os.path.expanduser('~/.aws/credentials')):
            return (False, '~/.aws/credentials does not exist. ' +
                    self._STATIC_CREDENTIAL_HELP_STR)

        # Checks if the AWS CLI is installed properly
        try:
            _run_output('aws configure list')
        except subprocess.CalledProcessError:
            return False, (
                'AWS CLI is not installed properly.'
                # TODO(zhwu): Change the installation hint to from PyPI.
                ' Run the following commands in the SkyPilot codebase:'
                '\n     $ pip install .[aws]'
                '\n   Credentials may also need to be set. ' +
                self._STATIC_CREDENTIAL_HELP_STR)

        # Checks if AWS credentials 1) exist and 2) are valid.
        # https://stackoverflow.com/questions/53548737/verify-aws-credentials-with-boto3
        try:
            self.get_current_user_identity()
        except exceptions.CloudUserIdentityError as e:
            return False, str(e)

        # Fetch the AWS availability zones mapping from ID to name.
        from sky.clouds.service_catalog import aws_catalog  # pylint: disable=import-outside-toplevel,unused-import
        return True, None

    def get_current_user_identity(self) -> Optional[str]:
        """Returns the identity of the user on this cloud."""
        try:
            sts = aws.client('sts')
            # The caller identity contains 3 fields: UserId, AccountId, Arn.
            # 'UserId' is unique across all AWS entity, which looks like
            # "AROADBQP57FF2AEXAMPLE:role-session-name"
            # 'AccountId' can be shared by multiple users under the same
            # organization
            # 'Arn' is the full path to the user, which can be reused when
            # the user is deleted and recreated.
            # Refer to https://docs.aws.amazon.com/cli/latest/reference/sts/get-caller-identity.html # pylint: disable=line-too-long
            # and https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_variables.html#principaltable # pylint: disable=line-too-long
            user_id = sts.get_caller_identity()['UserId']
        except aws.botocore_exceptions().NoCredentialsError:
            with ux_utils.print_exception_no_traceback():
                raise exceptions.CloudUserIdentityError(
                    f'AWS credentials are not set. {self._STATIC_CREDENTIAL_HELP_STR}'
                ) from None
        except aws.botocore_exceptions().ClientError:
            with ux_utils.print_exception_no_traceback():
                raise exceptions.CloudUserIdentityError(
                    'Failed to access AWS services with credentials. '
                    'Make sure that the access and secret keys are correct.'
                    f' {self._STATIC_CREDENTIAL_HELP_STR}') from None
        except aws.botocore_exceptions().TokenRetrievalError:
            # This is raised when the access token is expired, which mainly
            # happens when the user is using temporary credentials or SSO
            # login.
            with ux_utils.print_exception_no_traceback():
                raise exceptions.CloudUserIdentityError(
                    'AWS access token is expired.'
                    f' {self._SSO_CREDENTIAL_HELP_STR}') from None
        except Exception as e:  # pylint: disable=broad-except
            with ux_utils.print_exception_no_traceback():
                raise exceptions.CloudUserIdentityError(
                    f'Failed to get AWS user.\n'
                    f'  Reason: [{common_utils.class_fullname(e.__class__)}] {e}.'
                ) from None
        return user_id

    def get_credential_file_mounts(self) -> Dict[str, str]:
        return {
            f'~/.aws/{filename}': f'~/.aws/{filename}'
            for filename in _CREDENTIAL_FILES
        }

    def instance_type_exists(self, instance_type):
        return service_catalog.instance_type_exists(instance_type, clouds='aws')

    def accelerator_in_region_or_zone(self,
                                      accelerator: str,
                                      acc_count: int,
                                      region: Optional[str] = None,
                                      zone: Optional[str] = None) -> bool:
        return service_catalog.accelerator_in_region_or_zone(
            accelerator, acc_count, region, zone, 'aws')
