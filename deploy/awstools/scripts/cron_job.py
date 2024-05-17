import boto3
import psutil
import os
import click
import pickle
from itertools import chain
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict

CRON_PERSISTENT_DATA = Path("~/.cron/idle_timeouts.pickle").expanduser()
MAX_F1_IDLE_TIME = timedelta(minutes=5)
MAX_F1_IDLE_TIME_BEFORE_SIMULATION = timedelta(minutes=15)
MAX_MANAGER_IDLE_TIME = timedelta(minutes=30)

# Read context data from previous run
if CRON_PERSISTENT_DATA.exists():
    with CRON_PERSISTENT_DATA.open("rb") as fd:
        LAST_IDLE = pickle.load(fd)
else:
    CRON_PERSISTENT_DATA.parent.mkdir(exist_ok=True, parents=True)
    LAST_IDLE = {}
NEW_IDLE = {}

EC2 = boto3.client("ec2")


@click.group()
def cli():
    """
    CLI entrypoint
    """
    pass


def request_instances() -> List[Dict]:
    """
    Returns information about EC2 instances.
    """
    instances = EC2.describe_instances()
    return list(chain(*[i["Instances"] for i in instances["Reservations"]]))


def get_manager_instance(instances) -> Dict:
    """
    Returns FireSim manager information.
    """
    return next(
        filter(
            lambda x: x["State"]["Name"] in ("running", "stopping", "shutting-down")
            and x["SecurityGroups"][0]["GroupName"] == "firesim",
            instances,
        )
    )


def get_worker_instances(instances) -> List[Dict]:
    """
    Returns workers information.
    """
    return list(
        filter(
            lambda x: x["State"]["Name"] in ("running", "pending")
            and x["SecurityGroups"][0]["GroupName"] == "for-farms-only-firesim",
            instances,
        )
    )


def terminate_old_workers(farm_instances):
    """
    Detects which worker (instance spawned by FireSim) runs simulation
    and terminates it if it is idle for more than `MAX_F1_IDLE_TIME`.
    """
    terminate = []
    for worker in farm_instances:
        worker_id = worker["InstanceId"]
        private_ip = worker["PrivateIpAddress"]
        idle_start = LAST_IDLE.get(worker_id, {}).get("IdleStart", datetime.now())
        after_simulation = LAST_IDLE.get(worker_id, {}).get("Simulation", False)
        # Check whether simulation is running
        simulation = not os.system(
            f"ssh -oStrictHostKeyChecking=no -i /home/centos/firesim.pem centos@{private_ip} screen -S fsim0 -Q select ."
        )
        if not simulation:
            if idle_start and (
                datetime.now() - idle_start
                >= (
                    MAX_F1_IDLE_TIME
                    if after_simulation
                    else MAX_F1_IDLE_TIME_BEFORE_SIMULATION
                )
            ):
                terminate.append(worker_id)
                continue
            NEW_IDLE[worker_id] = {
                "IdleStart": idle_start if idle_start else datetime.now(),
                "Simulation": after_simulation,
            }
        else:
            NEW_IDLE[worker_id] = {
                "IdleStart": None,
                "Simulation": True,
            }

    if terminate:
        EC2.terminate_instances(
            InstanceIds=terminate,
        )


def terminate_all_workers(farm_instances, firesim_instance):
    """
    Terminates all instances spawned by FireSim.
    """
    if farm_instances:
        EC2.terminate_instances(
            InstanceIds=[worker["InstanceId"] for worker in farm_instances]
        )
    NEW_IDLE[firesim_instance["InstanceId"]] = LAST_IDLE.get(
        firesim_instance["InstanceId"], {}
    )


def stop_idle_manager(firesim_instance, farm_instances):
    """
    Detects whether someone is connected with manager instance.
    If not, stops it after `MAX_MANAGER_IDLE_TIME`.
    """
    manager_id = firesim_instance["InstanceId"]
    # Skip if workers are running
    if len(farm_instances) != 0:
        NEW_IDLE[manager_id] = {"IdleStart": None}
        return
    idle_start = LAST_IDLE.get(manager_id, {}).get("IdleStart", None)
    sshd_processes = len(
        [p for p in psutil.process_iter(["name"]) if p.info["name"].startswith("sshd")]
    )
    # Skip if there is some SSH connections
    if sshd_processes > 1:
        NEW_IDLE[manager_id] = {"IdleStart": None}
        return

    if idle_start and datetime.now() - idle_start > MAX_MANAGER_IDLE_TIME:
        EC2.stop_instances(InstanceIds=[manager_id])
    else:
        NEW_IDLE[manager_id] = {
            "IdleStart": (idle_start if idle_start else datetime.now())
        }


@cli.command()
def check_instances():
    """
    Periodical check stopping/terminating idle instances.
    """
    instances = request_instances()
    farm_instances = get_worker_instances(instances)
    firesim_instance = get_manager_instance(instances)
    terminate_old_workers(farm_instances)
    stop_idle_manager(firesim_instance, farm_instances)

    with CRON_PERSISTENT_DATA.open("wb") as fd:
        pickle.dump(NEW_IDLE, fd)


@cli.command()
def cleanup():
    """
    Cleanup hook, which terminates all worker instances.
    """
    instances = request_instances()
    farm_instances = get_worker_instances(instances)
    firesim_instance = get_manager_instance(instances)
    terminate_all_workers(farm_instances, firesim_instance)
    CRON_PERSISTENT_DATA.unlink(missing_ok=True)


if __name__ == "__main__":
    cli()
