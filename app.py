#! /usr/bin/python3
"""Python application to scrape data from Prometheus and store it to a long term block storage."""
import argparse
import bz2
from urllib.parse import urlparse
import boto3
import datetime
from time import sleep
from sys import getsizeof

import botocore
import requests
import json
import os

# Memory Management
import gc

# Disable SSL warnings
from requests.packages.urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)


# Defining some macros
DEBUG = False
DATA_CHUNK_SIZE = 3600  # For 1 hour chunk size
NET_DATA_SIZE = 3600 * 24  # To get the data for the past 24 hours
MAX_REQUEST_RETRIES = 5


CONNECTION_RETRY_WAIT_TIME = (
    1
)  # Time to wait before a retry in case of a connection error
# TOTAL_ERRORS = 0    # Count total connection errors after retries

# TODO: don't try to reconnect for each metric if initial connection to endpoint fails


class PrometheusBackup:
    """Implementation of Prometheus scrapper."""

    def __init__(self, url="", end_time=None, token=None):
        """Initalization function."""
        self.headers = {"Authorization": "bearer {}".format(token)}
        self.url = url
        self.prometheus_host = urlparse(self.url).netloc
        self._all_metrics = None
        self.connection_errors_count = 0  # Count total connection errors after retries
        self.data_chunk_size = "1h"
        self.stored_data_range = "6h"

        self.DATA_CHUNK_SIZE_STR = {
            60: "1m",
            1800: "30m",
            3600: "1h",
            10800: "3h",
            21600: "6h",
            43200: "12h",
            86400: "1d",
        }
        self.DATA_CHUNK_SIZE_LIST = {
            "1m": 60,
            "30m": 1800,
            "1h": 3600,
            "3h": 10800,
            "6h": 21600,
            "12h": 43200,
            "1d": 86400,
        }

        if end_time:
            end_time = str(end_time)
        else:
            end_time = (datetime.date.today() - datetime.timedelta(1)).strftime(
                "%Y%m%d"
            )  # default to previous day

        # parse 20171231 into timestamp
        if len(end_time) == 8:
            end_time = "{} 23:59:59".format(
                end_time
            )  # we repeat every 24H and get previous day's data
            end_time = datetime.datetime.strptime(
                end_time, "%Y%m%d %H:%M:%S"
            ).timestamp()

        self.end_time = datetime.datetime.fromtimestamp(int(end_time))
        self.start_time = self.end_time - datetime.timedelta(minutes=1440)

        self.boto_settings = {
            "access_key": os.getenv("BOTO_ACCESS_KEY"),
            "secret_key": os.getenv("BOTO_SECRET_KEY"),
            "object_store": os.getenv("BOTO_OBJECT_STORE"),
            "object_store_endpoint": os.getenv("BOTO_STORE_ENDPOINT"),
        }
        # print(self.boto_settings)

    def store_metric_values(self, name, values):
        """Function to store metrics to ceph."""
        if not values:
            return "No values for {}".format(name)
        # Create a session with CEPH (or any black storage) storage with the stored credentials
        session = boto3.Session(
            aws_access_key_id=self.boto_settings["access_key"],
            aws_secret_access_key=self.boto_settings["secret_key"],
        )

        s3 = session.resource(
            "s3", endpoint_url=self.boto_settings["object_store_endpoint"], verify=False
        )

        object_path = self.metric_filename(name)
        payload = bz2.compress(values.encode("utf-8"))
        rv = s3.meta.client.put_object(
            Body=payload, Bucket=self.boto_settings["object_store"], Key=object_path
        )
        gc.collect()  # Manually call the garbage collector
        if rv["ResponseMetadata"]["HTTPStatusCode"] == 200:
            return object_path
        else:
            return str(rv)

    def metric_filename(self, name):
        """Add a timestamp to the filename before it is stored in ceph."""
        directory_name = self.end_time.strftime("%Y%m%d")
        timestamp = self.end_time.strftime("%Y%m%d%H%M")
        object_path = (
            self.prometheus_host
            + "/"
            + name
            + "/"
            + directory_name
            + "/"
            + timestamp
            + ".json.bz2"
        )
        return object_path

    def all_metrics(self):
        """Get the list of all the metrics that the prometheus host has."""
        if not self._all_metrics:
            response = requests.get(
                "{0}/api/v1/label/__name__/values".format(self.url),
                verify=False,  # Disable ssl certificate verification temporarily
                headers=self.headers,
            )
            if DEBUG:
                print("Headers -> ", self.headers)
                print("URL => ", response.url)
            if response.status_code == 200:
                self._all_metrics = response.json()["data"]
            else:
                raise Exception(
                    "HTTP Status Code {} {} ({})".format(
                        response.status_code,
                        requests.status_codes._codes[response.status_code][0],
                        response.content,
                    )
                )
        return self._all_metrics

    def get_metric(self, name):
        """Get all of the metrics."""
        if name not in self.all_metrics():
            raise Exception("{} is not a valid metric".format(name))
        elif DEBUG:
            print("Metric is valid.")
        # if DATA_CHUNK_SIZE > self.DATA_CHUNK_SIZE_LIST[] :
        #     print("Invalid Chunk Size")
        #     exit(1)

        num_chunks = int(
            self.DATA_CHUNK_SIZE_LIST[self.stored_data_range]
            / self.DATA_CHUNK_SIZE_LIST[self.data_chunk_size]
        )  # Calculate the number of chunks using total data size and chunk size.
        # print(num_chunks)
        if DEBUG:
            print("Getting metric from Prometheus")

        metrics = self.get_metrics_from_prom(name, num_chunks)
        if metrics:
            return metrics

    def get_metrics_from_prom(self, name, chunks):
        """Get metrics from prometheus host in chunks."""
        if name not in self.all_metrics():
            raise Exception("{} is not a valid metric".format(name))

        # start = self.start_time.timestamp()
        end_timestamp = self.end_time.timestamp()
        chunk_size = self.DATA_CHUNK_SIZE_LIST[self.data_chunk_size]
        start = (
            end_timestamp
            - self.DATA_CHUNK_SIZE_LIST[self.stored_data_range]
            + chunk_size
        )
        data = []
        for i in range(chunks):
            gc.collect()  # Garbage collect to save Memory
            if DEBUG:
                print("Getting chunk: ", i)
                print("Start Time: ", datetime.datetime.fromtimestamp(start))

            tries = 0
            while tries < MAX_REQUEST_RETRIES:  # Retry code in case of errors
                response = requests.get(
                    "{0}/api/v1/query".format(
                        self.url
                    ),  # using the query API to get raw data
                    params={
                        "query": name + "[" + self.data_chunk_size + "]",
                        "time": start,
                    },
                    verify=False,  # Disable ssl certificate verification temporarily
                    headers=self.headers,
                )
                if DEBUG:
                    print(response.url)
                    pass

                tries += 1
                if response.status_code == 200:
                    data += response.json()["data"]["result"]

                    if DEBUG:
                        # print("Size of recent chunk = ",getsizeof(data))
                        # print(data)
                        print(
                            datetime.datetime.fromtimestamp(
                                response.json()["data"]["result"][0]["values"][0][0]
                            )
                        )
                        print(
                            datetime.datetime.fromtimestamp(
                                response.json()["data"]["result"][0]["values"][-1][0]
                            )
                        )
                        pass

                    del response
                    tries = MAX_REQUEST_RETRIES
                elif response.status_code == 504:
                    if tries >= MAX_REQUEST_RETRIES:
                        self.connection_errors_count += 1
                        return False
                    else:
                        print("Retry Count: ", tries)
                        sleep(
                            CONNECTION_RETRY_WAIT_TIME
                        )  # Wait for a second before making a new request
                else:
                    if tries >= MAX_REQUEST_RETRIES:
                        self.connection_errors_count += 1
                        raise Exception(
                            "HTTP Status Code {} {} ({})".format(
                                response.status_code,
                                requests.status_codes._codes[response.status_code][0],
                                response.content,
                            )
                        )
                    else:
                        print("Retry Count: ", tries)
                        sleep(CONNECTION_RETRY_WAIT_TIME)

            start += chunk_size

        return json.dumps(data)

    def metric_already_stored(self, metric):
        """Check if metric is already stored in the storage."""
        session = boto3.Session(
            aws_access_key_id=self.boto_settings["access_key"],
            aws_secret_access_key=self.boto_settings["secret_key"],
        )
        s3 = session.resource(
            "s3", endpoint_url=self.boto_settings["object_store_endpoint"], verify=False
        )

        object_path = self.metric_filename(metric)
        try:
            s3.Object(self.boto_settings["object_store"], object_path).load()
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return False
            else:
                raise
        else:
            if DEBUG:
                print(object_path)
                pass

            return True


if __name__ == "__main__":
    # parse the required input arguments, if no arguments print help
    parser = argparse.ArgumentParser(description="Backup Prometheus metrics")
    parser.add_argument(
        "--day",
        type=int,
        help="the day to backup in YYYYMMDD (defaults to previous day)",
    )
    parser.add_argument(
        "--url",
        type=str,
        default="https://prometheus-openshift-devops-monitor.1b7d.free-stg.openshiftapps.com",
        help="URL of the prometheus server default: %(default)s",
    )
    parser.add_argument("--token", type=str, help="Bearer token for prometheus")
    parser.add_argument("--backup-all", action="store_true", help="Backup all metrics")
    parser.add_argument(
        "--list-metrics", action="store_true", help="List metrics from prometheus"
    )
    parser.add_argument(
        "metric", nargs="*", help="Name of the metric, e.g. ALERTS - or --backup-all"
    )
    parser.add_argument(
        "--chunk-size",
        type=str,
        default="1h",
        help="Size of the chunk downloaded at an instance."
        " Accepted values are 30m, 1h, 6h, 12h, 1d default: %(default)s."
        " This value cannot be bigger than stored-data-range.",
    )
    parser.add_argument(
        "--stored-data-range",
        type=str,
        default="3h",
        help="Size of the data stored to the storage endpoint."
        "For example, 6h will divide the 24 hour data in 4 parts of 6 hours."
        "Accepted values are 30m, 1h, 3h, 6h, 12h, 1d default: %(default)s",
    )
    parser.add_argument("--debug", action="store_true", help="Enable Debug Mode")
    parser.add_argument(
        "--replace", action="store_true", help="Replace existing file with the current"
    )

    args = parser.parse_args()

    # override from ENV
    token = os.getenv("BEARER_TOKEN", args.token)
    url = os.getenv("URL", args.url)
    backup_all = os.getenv("PROM_BACKUP_ALL", args.backup_all)

    # print("Token => ",token)

    p = PrometheusBackup(url=url, end_time=args.day, token=token)

    if args.chunk_size not in p.DATA_CHUNK_SIZE_LIST:
        print("Invalid Chunk Size.", args.chunk_size)
        exit(1)
    if args.stored_data_range not in p.DATA_CHUNK_SIZE_LIST:
        print("Invalid Data store range.", args.chunk_size)
        exit(1)
    if (
        p.DATA_CHUNK_SIZE_LIST[args.chunk_size]
        > p.DATA_CHUNK_SIZE_LIST[args.stored_data_range]
    ):
        print("Chunk Size cannot be bigger than stored data range")
        exit(1)

    p.data_chunk_size = args.chunk_size
    p.stored_data_range = args.stored_data_range

    DEBUG = args.debug
    if args.list_metrics:
        metrics = p.all_metrics()
        print(metrics)
        exit()

    metrics = []
    if backup_all:
        metrics = p.all_metrics()
    else:
        metrics = args.metric

    # check for metrics in arguments
    if not metrics:
        parser.print_help()
        exit(1)

    num_of_file_parts = int(
        NET_DATA_SIZE / (p.DATA_CHUNK_SIZE_LIST[p.stored_data_range])
    )
    temp_end_time = p.end_time

    current_metric_num = 0
    total_num_metrics = len(metrics)
    for metric in metrics:
        try:
            current_metric_num += 1
            print("\n---------------------------------------")
            print(
                ("{} of {}.......".format(current_metric_num, total_num_metrics)),
                metric,
            )
            # print(metric)
            p.end_time = temp_end_time

            for parts in range(num_of_file_parts):
                # if DEBUG:
                # print("This End Time = ",p.end_time)

                if p.metric_already_stored(metric) and not args.replace:
                    print(
                        "Part {}/{}... already downloaded".format(
                            parts + 1, num_of_file_parts
                        )
                    )
                else:
                    # print("scraping metric: ",metric)
                    values = p.get_metric(metric)
                    print(
                        "Part {}/{}...metric collected".format(
                            parts + 1, num_of_file_parts
                        )
                    )
                    # print("Metrics-> ",metric,json.dumps(json.loads(values), indent = 4, sort_keys = True))

                    print(p.store_metric_values(metric, values))
                    del values
                p.end_time = datetime.datetime.fromtimestamp(
                    p.end_time.timestamp()
                    - int(p.DATA_CHUNK_SIZE_LIST[p.stored_data_range])
                )

        except Exception as ex:
            print("Error: {}".format(ex))
    if DEBUG:
        print("Total number of connection errors: ", p.connection_errors_count)
    exit(0)
