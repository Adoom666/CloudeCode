"""Named Cloudflare tunnel manager with single persistent tunnel."""

import asyncio
import subprocess
import json
import yaml
from pathlib import Path
from typing import Optional, Dict
from datetime import datetime
import structlog

from src.config import settings
from src.models import Tunnel, TunnelStatus
from src.core.cloudflare_api import CloudflareAPI

logger = structlog.get_logger()


class NamedTunnelError(Exception):
    """Exception raised for named tunnel errors."""
    pass


class NamedTunnelManager:
    """Manages a single persistent Cloudflare named tunnel with multiple ingress rules."""

    def __init__(self, session_manager, cloudflare_api: CloudflareAPI):
        """
        Initialize the named tunnel manager.

        Args:
            session_manager: SessionManager instance
            cloudflare_api: CloudflareAPI instance for DNS management
        """
        self.session_manager = session_manager
        self.cloudflare_api = cloudflare_api
        self.tunnel_name = settings.cloudflare_tunnel_name
        self.tunnel_id = settings.cloudflare_tunnel_id
        self.domain = settings.cloudflare_domain
        self.tunnels: Dict[int, Tunnel] = {}  # port -> Tunnel mapping
        self._tunnel_process: Optional[subprocess.Popen] = None
        self._config_path = Path.home() / ".cloudflared" / f"{self.tunnel_name}.yml"

    async def initialize(self) -> bool:
        """
        Initialize the named tunnel.
        Creates tunnel if it doesn't exist, starts the tunnel process.

        Returns:
            True if initialization successful
        """
        try:
            # Check if tunnel exists
            tunnel_id = await self._get_or_create_tunnel()

            if not tunnel_id:
                raise NamedTunnelError("Failed to get or create tunnel")

            self.tunnel_id = tunnel_id
            self.cloudflare_api.tunnel_id = tunnel_id  # Update CloudflareAPI with tunnel ID
            logger.info("tunnel_id_obtained", tunnel_id=tunnel_id)

            # Create initial config
            await self._create_tunnel_config()

            # Start tunnel process
            await self._start_tunnel_process()

            # Create root domain CNAME
            root_url = await self.cloudflare_api.create_root_cname()
            if root_url:
                logger.info("root_domain_configured", url=root_url)
            else:
                logger.warning("root_domain_cname_creation_failed")

            logger.info("named_tunnel_initialized", tunnel_name=self.tunnel_name)
            return True

        except Exception as e:
            logger.error("named_tunnel_initialization_failed", error=str(e))
            return False

    async def _get_or_create_tunnel(self) -> Optional[str]:
        """
        Get existing tunnel ID or create new tunnel.

        Returns:
            Tunnel ID or None if failed
        """
        # First try to use existing tunnel ID from config
        if self.tunnel_id:
            logger.info("using_existing_tunnel_id", tunnel_id=self.tunnel_id)
            return self.tunnel_id

        try:
            # List existing tunnels
            result = subprocess.run(
                ["cloudflared", "tunnel", "list", "--output", "json"],
                capture_output=True,
                text=True,
                check=True
            )

            tunnels = json.loads(result.stdout) or []

            # Find our tunnel by name
            for tunnel in tunnels:
                if tunnel.get("name") == self.tunnel_name:
                    tunnel_id = tunnel.get("id")
                    logger.info("found_existing_tunnel", tunnel_id=tunnel_id)
                    return tunnel_id

            # Tunnel doesn't exist, create it
            logger.info("creating_new_tunnel", tunnel_name=self.tunnel_name)

            result = subprocess.run(
                ["cloudflared", "tunnel", "create", self.tunnel_name],
                capture_output=True,
                text=True,
                check=True
            )

            # Extract tunnel ID from output
            # Output format: "Created tunnel <name> with id <id>"
            for line in result.stdout.split("\n"):
                if "Created tunnel" in line and "with id" in line:
                    tunnel_id = line.split("with id")[-1].strip()
                    logger.info("tunnel_created", tunnel_id=tunnel_id)
                    return tunnel_id

            raise NamedTunnelError("Could not extract tunnel ID from creation output")

        except subprocess.CalledProcessError as e:
            logger.error("tunnel_creation_command_failed", error=e.stderr)
            raise NamedTunnelError(f"Failed to create tunnel: {e.stderr}")
        except Exception as e:
            logger.error("tunnel_creation_failed", error=str(e))
            raise

    async def _create_tunnel_config(self):
        """Create the tunnel configuration file with ingress rules."""
        config = {
            "tunnel": self.tunnel_id,
            "credentials-file": str(Path.home() / ".cloudflared" / f"{self.tunnel_id}.json"),
            "ingress": [
                # Root domain points to the FastAPI app on port 8000
                {
                    "hostname": self.domain,
                    "service": "http://127.0.0.1:8000"
                },
                # Default rule (required, must be last)
                {"service": "http_status:404"}
            ]
        }

        # Ensure config directory exists
        self._config_path.parent.mkdir(parents=True, exist_ok=True)

        # Write config
        with open(self._config_path, "w") as f:
            yaml.dump(config, f)

        logger.info("tunnel_config_created", config_path=str(self._config_path), root_domain=self.domain)

    async def _start_tunnel_process(self):
        """Start the cloudflared tunnel process."""
        try:
            logger.info("starting_tunnel_process", tunnel_name=self.tunnel_name)

            # Create log file for cloudflared output
            log_file = Path("/tmp/cloudflared-tunnel.log")

            # Use shell command with nohup to ensure process stays alive
            shell_cmd = f"nohup cloudflared tunnel --config {self._config_path} run > {log_file} 2>&1 &"

            # Execute the shell command
            result = subprocess.run(
                shell_cmd,
                shell=True,
                capture_output=True,
                text=True
            )

            if result.returncode != 0:
                raise NamedTunnelError(f"Failed to start cloudflared: {result.stderr}")

            # Wait a moment for tunnel to establish
            await asyncio.sleep(3)

            # Verify tunnel is running by checking for cloudflared process
            check_result = subprocess.run(
                ["pgrep", "-f", "cloudflared tunnel"],
                capture_output=True,
                text=True
            )

            if check_result.returncode == 0 and check_result.stdout.strip():
                pid = check_result.stdout.strip().split('\n')[0]
                logger.info("tunnel_process_started", pid=pid, log_file=str(log_file))
                # Store PID directly instead of dummy object
                self._tunnel_process = int(pid)
            else:
                with open(log_file, "r") as f:
                    log_output = f.read()
                raise NamedTunnelError(f"Tunnel process not found after start: {log_output}")

        except Exception as e:
            logger.error("tunnel_process_start_failed", error=str(e))
            raise NamedTunnelError(f"Failed to start tunnel process: {str(e)}")

    async def add_port_mapping(self, port: int) -> Optional[Tunnel]:
        """
        Add a new port mapping to the tunnel.

        Args:
            port: Local port to expose

        Returns:
            Tunnel object or None if failed
        """
        if not self.session_manager.has_active_session():
            raise ValueError("No active session")

        session = self.session_manager.session

        # Check if port already mapped
        if port in self.tunnels:
            logger.info("port_already_mapped", port=port)
            return self.tunnels[port]

        try:
            # Create CNAME record via Cloudflare API
            public_url = await self.cloudflare_api.create_cname_for_port(port)

            if not public_url:
                # Fall back to direct tunnel subdomain if CNAME creation fails
                public_url = f"https://{port}.{self.domain}"
                logger.warning("cname_creation_failed_using_fallback", url=public_url)

            # Add ingress rule to tunnel config
            await self._add_ingress_rule(port)

            # Create tunnel object
            tunnel_id = f"tun_{port}_{int(datetime.utcnow().timestamp())}"

            tunnel = Tunnel(
                id=tunnel_id,
                session_id=session.id,
                port=port,
                public_url=public_url,
                status=TunnelStatus.ACTIVE,
                process_pid=self._tunnel_process if self._tunnel_process else None
            )

            self.tunnels[port] = tunnel

            # Add to session
            session.tunnels.append(tunnel)
            self.session_manager._save_session_metadata()

            logger.info(
                "port_mapping_added",
                port=port,
                public_url=public_url,
                tunnel_id=tunnel_id
            )

            return tunnel

        except Exception as e:
            logger.error("add_port_mapping_failed", port=port, error=str(e))
            raise NamedTunnelError(f"Failed to add port mapping: {str(e)}")

    async def _add_ingress_rule(self, port: int):
        """
        Add ingress rule to tunnel configuration and reload tunnel.

        Args:
            port: Port to add
        """
        try:
            # Read current config
            with open(self._config_path, "r") as f:
                config = yaml.safe_load(f)

            # Add new ingress rule (before the catch-all 404 rule)
            new_rule = {
                "hostname": f"{port}.{self.domain}",
                "service": f"http://127.0.0.1:{port}"
            }

            # Insert before the last rule (which should be the 404 catch-all)
            config["ingress"].insert(-1, new_rule)

            # Write updated config
            with open(self._config_path, "w") as f:
                yaml.dump(config, f)

            logger.info("ingress_rule_added", port=port, hostname=new_rule["hostname"])

            # Reload tunnel configuration
            # Send SIGHUP to cloudflared process to reload config
            if self._tunnel_process:
                try:
                    import os
                    import signal
                    os.kill(self._tunnel_process, signal.SIGHUP)
                    logger.info("tunnel_config_reloaded")
                except Exception as e:
                    logger.warning("tunnel_config_reload_failed", error=str(e))

        except Exception as e:
            logger.error("add_ingress_rule_failed", port=port, error=str(e))
            raise

    async def remove_port_mapping(self, port: int) -> bool:
        """
        Remove a port mapping from the tunnel.

        Args:
            port: Port to remove

        Returns:
            True if removed successfully
        """
        if port not in self.tunnels:
            logger.warning("port_not_mapped", port=port)
            return False

        try:
            tunnel = self.tunnels[port]

            # Remove ingress rule
            await self._remove_ingress_rule(port)

            # Remove from tunnels dict
            del self.tunnels[port]

            # Remove from session
            if self.session_manager.session:
                self.session_manager.session.tunnels = [
                    t for t in self.session_manager.session.tunnels if t.id != tunnel.id
                ]
                self.session_manager._save_session_metadata()

            logger.info("port_mapping_removed", port=port)
            return True

        except Exception as e:
            logger.error("remove_port_mapping_failed", port=port, error=str(e))
            return False

    async def _remove_ingress_rule(self, port: int):
        """Remove ingress rule from tunnel configuration."""
        try:
            # Read current config
            with open(self._config_path, "r") as f:
                config = yaml.safe_load(f)

            # Remove the rule for this port
            hostname = f"{port}.{self.domain}"
            config["ingress"] = [
                rule for rule in config["ingress"]
                if rule.get("hostname") != hostname
            ]

            # Write updated config
            with open(self._config_path, "w") as f:
                yaml.dump(config, f)

            logger.info("ingress_rule_removed", port=port)

            # Reload tunnel configuration
            if self._tunnel_process:
                try:
                    import os
                    import signal
                    os.kill(self._tunnel_process, signal.SIGHUP)
                    logger.info("tunnel_config_reloaded")
                except Exception as e:
                    logger.warning("tunnel_config_reload_failed", error=str(e))

        except Exception as e:
            logger.error("remove_ingress_rule_failed", port=port, error=str(e))
            raise

    async def shutdown(self):
        """Shutdown the tunnel process."""
        logger.info("shutting_down_named_tunnel")

        if self._tunnel_process:
            try:
                import os
                import signal
                os.kill(self._tunnel_process, signal.SIGTERM)
                await asyncio.sleep(2)
                # Check if still running
                try:
                    os.kill(self._tunnel_process, 0)
                    # Still running, force kill
                    logger.warning("tunnel_process_not_terminating_killing")
                    os.kill(self._tunnel_process, signal.SIGKILL)
                except ProcessLookupError:
                    # Process already dead
                    pass
            except Exception as e:
                logger.error("tunnel_shutdown_error", error=str(e))

        logger.info("named_tunnel_shutdown_complete")

    def get_tunnel_for_port(self, port: int) -> Optional[Tunnel]:
        """Get tunnel for a specific port."""
        return self.tunnels.get(port)

    def get_all_tunnels(self) -> list[Tunnel]:
        """Get all active tunnels."""
        return list(self.tunnels.values())
