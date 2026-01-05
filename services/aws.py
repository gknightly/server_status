# services/aws.py
"""AWS EC2 service for managing server instances."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import boto3
from botocore.exceptions import ClientError, WaiterError

from constants import EC2_HEALTH_TIMEOUT_SEC, EC2_START_TIMEOUT_SEC
from errors import AWSError

if TYPE_CHECKING:
    from config import ServerCfg


@dataclass
class InstanceHealth:
    """Health status of an EC2 instance."""

    state: str  # running, stopped, pending, etc.
    system_status: str | None  # ok, impaired, initializing, None if not available
    instance_status: str | None  # ok, impaired, initializing, None if not available

    @property
    def is_healthy(self) -> bool:
        """True if instance is running and all health checks pass."""
        return (
            self.state == "running"
            and self.system_status == "ok"
            and self.instance_status == "ok"
        )


class EC2Service:
    """Manages EC2 operations across multiple regions."""

    def __init__(self, aws_key: str, aws_secret: str) -> None:
        self._aws_key = aws_key
        self._aws_secret = aws_secret
        self._clients: dict[str, Any] = {}  # region -> boto3 EC2 client

    def _get_client(self, region: str) -> Any:
        """Get or create an EC2 client for the specified region."""
        if region not in self._clients:
            logging.info(f"Creating EC2 client for region: {region}")
            self._clients[region] = boto3.client(
                "ec2",
                region_name=region,
                aws_access_key_id=self._aws_key,
                aws_secret_access_key=self._aws_secret,
            )
        return self._clients[region]

    async def get_state(self, server: ServerCfg) -> str:
        """Get the current state of an EC2 instance."""
        try:
            client = self._get_client(server.region)
            response = await asyncio.to_thread(
                client.describe_instances,
                InstanceIds=[server.instance_id],
            )
            state = response["Reservations"][0]["Instances"][0]["State"]["Name"]
            return state
        except ClientError as e:
            logging.error(f"AWS error getting state for {server.instance_id}: {e}")
            raise AWSError(f"Failed to get instance state: {e}") from e
        except (KeyError, IndexError) as e:
            logging.error(f"Unexpected response format for {server.instance_id}: {e}")
            raise AWSError(f"Invalid AWS response: {e}") from e

    async def get_public_ip(self, server: ServerCfg) -> str | None:
        """Get the public IP address of a running EC2 instance."""
        try:
            client = self._get_client(server.region)
            response = await asyncio.to_thread(
                client.describe_instances,
                InstanceIds=[server.instance_id],
            )
            instance = response["Reservations"][0]["Instances"][0]
            return instance.get("PublicIpAddress")
        except ClientError as e:
            logging.error(f"AWS error getting public IP for {server.instance_id}: {e}")
            raise AWSError(f"Failed to get public IP: {e}") from e
        except (KeyError, IndexError) as e:
            logging.error(f"Unexpected response format for {server.instance_id}: {e}")
            raise AWSError(f"Invalid AWS response: {e}") from e

    async def get_health(self, server: ServerCfg) -> InstanceHealth:
        """Get detailed health status of an EC2 instance."""
        try:
            client = self._get_client(server.region)

            # Get basic state
            state = await self.get_state(server)

            # Only running instances have status checks
            if state != "running":
                return InstanceHealth(
                    state=state,
                    system_status=None,
                    instance_status=None,
                )

            # Get detailed status
            response = await asyncio.to_thread(
                client.describe_instance_status,
                InstanceIds=[server.instance_id],
            )

            if not response.get("InstanceStatuses"):
                # Instance exists but no status yet (just started)
                return InstanceHealth(
                    state=state,
                    system_status="initializing",
                    instance_status="initializing",
                )

            status = response["InstanceStatuses"][0]
            return InstanceHealth(
                state=state,
                system_status=status["SystemStatus"]["Status"],
                instance_status=status["InstanceStatus"]["Status"],
            )
        except AWSError:
            raise
        except ClientError as e:
            logging.error(f"AWS error getting health for {server.instance_id}: {e}")
            raise AWSError(f"Failed to get instance health: {e}") from e

    async def start_instance(self, server: ServerCfg) -> None:
        """Start an EC2 instance and wait for it to be running."""
        try:
            client = self._get_client(server.region)

            logging.info(f"Starting EC2 instance {server.instance_id}")
            await asyncio.to_thread(
                client.start_instances,
                InstanceIds=[server.instance_id],
            )

            # Wait for instance to be running
            logging.info(f"Waiting for instance {server.instance_id} to be running...")
            waiter = client.get_waiter("instance_running")
            await asyncio.to_thread(
                waiter.wait,
                InstanceIds=[server.instance_id],
                WaiterConfig={"Delay": 15, "MaxAttempts": EC2_START_TIMEOUT_SEC // 15},
            )
            logging.info(f"Instance {server.instance_id} is now running")

        except WaiterError as e:
            logging.error(f"Timeout waiting for instance {server.instance_id} to start: {e}")
            raise AWSError(f"Instance failed to start within timeout: {e}") from e
        except ClientError as e:
            logging.error(f"AWS error starting instance {server.instance_id}: {e}")
            raise AWSError(f"Failed to start instance: {e}") from e

    async def wait_for_health(
        self,
        server: ServerCfg,
        timeout: int = EC2_HEALTH_TIMEOUT_SEC,
    ) -> bool:
        """Wait for instance health checks to pass. Returns True if healthy, False on timeout."""
        logging.info(f"Waiting for health checks on {server.instance_id}...")
        elapsed = 0
        interval = 15

        while elapsed < timeout:
            try:
                health = await self.get_health(server)
                if health.is_healthy:
                    logging.info(f"Instance {server.instance_id} passed health checks")
                    return True

                logging.debug(
                    f"Health check pending for {server.instance_id}: "
                    f"system={health.system_status}, instance={health.instance_status}"
                )
            except AWSError as e:
                logging.warning(f"Health check error (will retry): {e}")

            await asyncio.sleep(interval)
            elapsed += interval

        logging.warning(f"Health check timeout for {server.instance_id} after {timeout}s")
        return False

    async def stop_instance(self, server: ServerCfg, force: bool = False) -> None:
        """Stop an EC2 instance and wait for it to stop.

        Args:
            server: Server configuration
            force: If True, forces the instance to stop without waiting for graceful shutdown
        """
        try:
            client = self._get_client(server.region)

            logging.info(f"Stopping EC2 instance {server.instance_id} (force={force})")
            await asyncio.to_thread(
                client.stop_instances,
                InstanceIds=[server.instance_id],
                Force=force,
            )

            # Wait for instance to stop
            logging.info(f"Waiting for instance {server.instance_id} to stop...")
            waiter = client.get_waiter("instance_stopped")
            await asyncio.to_thread(
                waiter.wait,
                InstanceIds=[server.instance_id],
                WaiterConfig={"Delay": 15, "MaxAttempts": 40},
            )
            logging.info(f"Instance {server.instance_id} is now stopped")

        except WaiterError as e:
            logging.error(f"Timeout waiting for instance {server.instance_id} to stop: {e}")
            raise AWSError(f"Instance failed to stop within timeout: {e}") from e
        except ClientError as e:
            logging.error(f"AWS error stopping instance {server.instance_id}: {e}")
            raise AWSError(f"Failed to stop instance: {e}") from e
