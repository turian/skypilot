"""A script that queries AWS API to get instance types and pricing information.
This script takes about 1 minute to finish.
"""
import argparse
import datetime
import os
import subprocess
from typing import Optional, Set, Tuple, Union

import numpy as np
import pandas as pd
import ray

from sky.adaptors import aws

# Enable most of the regions. Each user's account may have a subset of these
# enabled; this is ok because we take the intersection of the list here with
# the user-specific enabled regions in `aws_catalog`, via the "availability
# zone mapping".
# TODO(zhwu): fix the regions with no supported AMI (maybe by finding AMIs
# similar to the Deep Learning AMI).
ALL_REGIONS = [
    'us-east-1',
    'us-east-2',
    'us-west-1',
    'us-west-2',
    'ca-central-1',
    'eu-central-1',
    # 'eu-central-2', # no supported AMI
    'eu-west-1',
    'eu-west-2',
    'eu-south-1',
    # 'eu-south-2', # no supported AMI
    'eu-west-3',
    'eu-north-1',
    'me-south-1',
    'me-central-1',
    'af-south-1',
    'ap-east-1',
    'ap-southeast-3',
    'ap-south-1',
    # 'ap-south-2', # no supported AMI
    'ap-northeast-3',
    'ap-northeast-2',
    'ap-southeast-1',
    'ap-southeast-2',
    'ap-northeast-1',
]
US_REGIONS = ['us-east-1', 'us-east-2', 'us-west-1', 'us-west-2']

USEFUL_COLUMNS = [
    'InstanceType', 'AcceleratorName', 'AcceleratorCount', 'vCPUs', 'MemoryGiB',
    'GpuInfo', 'Price', 'SpotPrice', 'Region', 'AvailabilityZone'
]

# NOTE: the hard-coded us-east-1 URL is not a typo. AWS pricing endpoint is
# only available in this region, but it serves pricing information for all
# regions.
PRICING_TABLE_URL_FMT = 'https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/{region}/index.csv'  # pylint: disable=line-too-long

regions_enabled: Set[str] = None


def get_enabled_regions() -> Set[str]:
    # Should not be called concurrently.
    global regions_enabled
    if regions_enabled is None:
        aws_client = aws.client('ec2', region_name='us-east-1')
        regions_enabled = aws_client.describe_regions()['Regions']
        regions_enabled = {r['RegionName'] for r in regions_enabled}
        regions_enabled = regions_enabled.intersection(set(ALL_REGIONS))
    return regions_enabled


@ray.remote
def _get_instance_types(region: str) -> pd.DataFrame:
    client = aws.client('ec2', region_name=region)
    paginator = client.get_paginator('describe_instance_types')
    items = []
    for i, resp in enumerate(paginator.paginate()):
        print(f'{region} getting instance types page {i}')
        items += resp['InstanceTypes']

    return pd.DataFrame(items)


@ray.remote
def _get_availability_zones(region: str) -> Optional[pd.DataFrame]:
    client = aws.client('ec2', region_name=region)
    zones = []
    try:
        response = client.describe_availability_zones()
    except aws.botocore_exceptions().ClientError:
        # The user's AWS account may not have access to this region.
        # The error looks like:
        # botocore.exceptions.ClientError: An error occurred
        # (AuthFailure) when calling the DescribeAvailabilityZones
        # operation: AWS was not able to validate the provided
        # access credentials
        return None
    for resp in response['AvailabilityZones']:
        zones.append({
            'AvailabilityZoneName': resp['ZoneName'],
            'AvailabilityZone': resp['ZoneId'],
        })
    return pd.DataFrame(zones)


@ray.remote
def _get_pricing_table(region: str) -> pd.DataFrame:
    print(f'{region} downloading pricing table')
    url = PRICING_TABLE_URL_FMT.format(region=region)
    df = pd.read_csv(url, skiprows=5, low_memory=False)
    df.rename(columns={
        'Instance Type': 'InstanceType',
        'PricePerUnit': 'Price',
    },
              inplace=True)
    return df[(df['TermType'] == 'OnDemand') &
              (df['Operating System'] == 'Linux') &
              df['Pre Installed S/W'].isnull() &
              (df['CapacityStatus'] == 'Used') &
              (df['Tenancy'].isin(['Host', 'Shared'])) & df['Price'] > 0][[
                  'InstanceType', 'Price', 'vCPU', 'Memory'
              ]]


@ray.remote
def _get_spot_pricing_table(region: str) -> pd.DataFrame:
    """Get spot pricing table for a region.

    Example output:
        InstanceType  AvailabilityZoneName  SpotPrice
        p3.2xlarge    us-east-1a            1.0000
    """
    print(f'{region} downloading spot pricing table')
    client = aws.client('ec2', region_name=region)
    paginator = client.get_paginator('describe_spot_price_history')
    response_iterator = paginator.paginate(ProductDescriptions=['Linux/UNIX'],
                                           StartTime=datetime.datetime.utcnow())
    ret = []
    for response in response_iterator:
        # response['SpotPriceHistory'] is a list of dicts, each dict is like:
        # {
        #  'AvailabilityZone': 'us-east-2c',  # This is the AZ name.
        #  'InstanceType': 'm6i.xlarge',
        #  'ProductDescription': 'Linux/UNIX',
        #  'SpotPrice': '0.041900',
        #  'Timestamp': datetime.datetime
        # }
        ret = ret + response['SpotPriceHistory']
    df = pd.DataFrame(ret)[['InstanceType', 'AvailabilityZone', 'SpotPrice']]
    df = df.rename(columns={'AvailabilityZone': 'AvailabilityZoneName'})
    df = df.set_index(['InstanceType', 'AvailabilityZoneName'])
    return df


@ray.remote
def _get_instance_types_df(region: str) -> Union[str, pd.DataFrame]:
    try:
        # Fetch the zone info first to make sure the account has access to the
        # region.
        zone_df = ray.get(_get_availability_zones.remote(region))
        if zone_df is None:
            raise RuntimeError(f'No access to region {region}')

        df, pricing_df, spot_pricing_df = ray.get([
            _get_instance_types.remote(region),
            _get_pricing_table.remote(region),
            _get_spot_pricing_table.remote(region),
        ])
        print(f'{region} Processing dataframes')

        def get_acc_info(row) -> Tuple[str, float]:
            accelerator = None
            for col, info_key in [('GpuInfo', 'Gpus'),
                                  ('InferenceAcceleratorInfo', 'Accelerators'),
                                  ('FpgaInfo', 'Fpgas')]:
                info = row.get(col)
                if isinstance(info, dict):
                    accelerator = info[info_key][0]
            if accelerator is None:
                return None, np.nan
            return accelerator['Name'], accelerator['Count']

        def get_vcpus(row) -> float:
            if not np.isnan(row['vCPU']):
                return float(row['vCPU'])
            return float(row['VCpuInfo']['DefaultVCpus'])

        def get_memory_gib(row) -> float:
            if isinstance(row['MemoryInfo'], dict):
                return row['MemoryInfo']['SizeInMiB'] / 1024
            return float(row['Memory'].split(' GiB')[0])

        def get_additional_columns(row) -> pd.Series:
            acc_name, acc_count = get_acc_info(row)
            # AWS p3dn.24xlarge offers a different V100 GPU.
            # See https://aws.amazon.com/blogs/compute/optimizing-deep-learning-on-p3-and-p3dn-with-efa/ # pylint: disable=line-too-long
            if row['InstanceType'] == 'p3dn.24xlarge':
                acc_name = 'V100-32GB'
            if row['InstanceType'] == 'p4de.24xlarge':
                acc_name = 'A100-80GB'
                acc_count = 8
            return pd.Series({
                'AcceleratorName': acc_name,
                'AcceleratorCount': acc_count,
                'vCPUs': get_vcpus(row),
                'MemoryGiB': get_memory_gib(row),
            })

        # The AWS API may not have all the instance types in the pricing table,
        # so we need to merge the two dataframes.
        df = df.merge(pricing_df, on=['InstanceType'], how='outer')
        df['Region'] = region
        # Cartesian product of instance types and availability zones, so that
        # we can join the spot pricing table per instance type and zone.
        df = df.merge(pd.DataFrame(zone_df), how='cross')

        # Add spot price column, by joining the spot pricing table.
        df = df.merge(spot_pricing_df,
                      left_on=['InstanceType', 'AvailabilityZoneName'],
                      right_index=True,
                      how='outer')

        # Extract vCPUs, memory, and accelerator info from the columns.
        df = pd.concat(
            [df, df.apply(get_additional_columns, axis='columns')],
            axis='columns')
        # patch the GpuInfo for p4de.24xlarge
        df.loc[df['InstanceType'] == 'p4de.24xlarge', 'GpuInfo'] = 'A100-80GB'
        df = df[USEFUL_COLUMNS]
    except Exception as e:  # pylint: disable=broad-except
        print(f'{region} failed with {e}')
        return region
    return df


def get_all_regions_instance_types_df(regions: Set[str]) -> pd.DataFrame:
    df_or_regions = ray.get([_get_instance_types_df.remote(r) for r in regions])
    new_dfs = []
    for df_or_region in df_or_regions:
        if isinstance(df_or_region, str):
            print(f'{df_or_region} failed')
        else:
            new_dfs.append(df_or_region)

    df = pd.concat(new_dfs)
    df.sort_values(['InstanceType', 'Region'], inplace=True)
    return df


# Fetch Images
_GPU_TO_IMAGE_DATE = {
    # https://console.aws.amazon.com/ec2/v2/home?region=us-east-1#Images:visibility=public-images;v=3;search=:64,:Ubuntu%2020,:Deep%20Learning%20AMI%20GPU%20PyTorch # pylint: disable=line-too-long
    # Current AMIs:
    # Deep Learning AMI GPU PyTorch 1.10.0 (Ubuntu 20.04) 20220308
    #   Nvidia driver: 510.47.03, CUDA Version: 11.6 (does not support torch==1.13.0+cu117)
    #
    # Use a list to fallback to newer AMI, as some regions like ap-southeast-3 does not have
    # the older AMI.
    'gpu': ['20220308', '20221101'],
    # Deep Learning AMI GPU PyTorch 1.10.0 (Ubuntu 20.04) 20211208
    # Downgrade the AMI for K80 due as it is only compatible with
    # NVIDIA driver lower than 470.
    'k80': ['20211208']
}
_UBUNTU_VERSION = ['18.04', '20.04']


def _get_image_id(region: str, ubuntu_version: str, creation_date: str) -> str:
    try:
        image_id = subprocess.check_output(f"""\
            aws ec2 describe-images --region {region} --owners amazon \\
                --filters 'Name=name,Values="Deep Learning AMI GPU PyTorch 1.10.0 (Ubuntu {ubuntu_version}) {creation_date}"' \\
                    'Name=state,Values=available' --query 'Images[:1].ImageId' --output text
            """,
                                           shell=True)
    except subprocess.CalledProcessError as e:
        print(f'Failed {region}, {ubuntu_version}, {creation_date}. '
              'Trying next date.')
        print(f'{type(e)}: {e}')
        image_id = None
    else:
        image_id = image_id.decode('utf-8').strip()
    return image_id


@ray.remote
def _get_image_row(region: str, ubuntu_version: str,
                   cpu_or_gpu: str) -> Tuple[str, str, str, str, str, str]:
    print(f'Getting image for {region}, {ubuntu_version}, {cpu_or_gpu}')
    creation_date = _GPU_TO_IMAGE_DATE[cpu_or_gpu]
    date = None
    for date in creation_date:
        image_id = _get_image_id(region, ubuntu_version, date)
        if image_id:
            break
    else:
        # not found
        print(
            f'Failed to find image for {region}, {ubuntu_version}, {cpu_or_gpu}'
        )
    if date is None:
        raise ValueError(f'Could not find the creation date for {cpu_or_gpu}.')
    tag = f'skypilot:{cpu_or_gpu}-ubuntu-{ubuntu_version.replace(".", "")}'
    return tag, region, 'ubuntu', ubuntu_version, image_id, date


def get_all_regions_images_df(regions: Set[str]) -> pd.DataFrame:
    workers = []
    for cpu_or_gpu in _GPU_TO_IMAGE_DATE:
        for ubuntu_version in _UBUNTU_VERSION:
            for region in regions:
                workers.append(
                    _get_image_row.remote(region, ubuntu_version, cpu_or_gpu))

    results = ray.get(workers)
    results = pd.DataFrame(
        results,
        columns=['Tag', 'Region', 'OS', 'OSVersion', 'ImageId', 'CreationDate'])
    results.sort_values(['Tag', 'Region'], inplace=True)
    return results


def fetch_availability_zone_mappings() -> pd.DataFrame:
    """Fetch the availability zone mappings from ID to Name.

    Example output:
        AvailabilityZone  AvailabilityZoneName
        use1-az1          us-east-1b
        use1-az2          us-east-1a
    """
    regions = get_enabled_regions()
    az_mappings = [_get_availability_zones.remote(r) for r in regions]
    az_mappings = ray.get(az_mappings)
    missing_regions = {
        regions[i] for i, m in enumerate(az_mappings) if m is None
    }
    if missing_regions:
        # This could happen if a AWS API glitch happens, it is to make sure
        # that the availability zone does not get lost silently.
        print('WARNING: Missing availability zone mappings for the following '
              f'enabled regions: {missing_regions}')
    # Remove the regions that the user does not have access to.
    az_mappings = [m for m in az_mappings if m is not None]
    az_mappings = pd.concat(az_mappings)
    return az_mappings


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--az-mappings',
        dest='az_mappings',
        action='store_true',
        help='Fetch the mapping from availability zone IDs to zone names.')
    parser.add_argument('--no-az-mappings',
                        dest='az_mappings',
                        action='store_false')
    parser.add_argument(
        '--check-all-regions-enabled-for-account',
        action='store_true',
        help=('Check that this account has enabled "all" global regions '
              'hardcoded in this script. Useful to ensure our automatic '
              'fetcher fetches the expected data.'))
    parser.set_defaults(az_mappings=True)
    args, _ = parser.parse_known_args()

    user_regions = get_enabled_regions()
    if args.check_all_regions_enabled_for_account and set(
            ALL_REGIONS) - user_regions:
        raise RuntimeError('The following regions are not enabled: '
                           f'{set(ALL_REGIONS) - user_regions}')

    def _check_regions_integrity(df: pd.DataFrame, name: str):
        # Check whether the fetched regions match the requested regions to
        # guard against network issues or glitches in the AWS API.
        fetched_regions = set(df['Region'].unique())
        if fetched_regions != user_regions:
            # This is a sanity check to make sure that the regions we
            # requested are the same as the ones we fetched.
            # The mismatch could happen for network issues or glitches
            # in the AWS API.
            raise RuntimeError(
                f'{name}: Fetched regions {fetched_regions} does not match '
                f'requested regions {user_regions}.')

    ray.init()
    instance_df = get_all_regions_instance_types_df(user_regions)
    _check_regions_integrity(instance_df, 'instance types')

    os.makedirs('aws', exist_ok=True)
    instance_df.to_csv('aws/vms.csv', index=False)
    print('AWS Service Catalog saved to aws/vms.csv')

    image_df = get_all_regions_images_df(user_regions)
    _check_regions_integrity(image_df, 'images')

    image_df.to_csv('aws/images.csv', index=False)
    print('AWS Images saved to aws/images.csv')

    if args.az_mappings:
        az_mappings_df = fetch_availability_zone_mappings()
        az_mappings_df.to_csv('aws/az_mappings.csv', index=False)
        print('AWS Availability Zone mapping saved to aws/az_mappings.csv')
