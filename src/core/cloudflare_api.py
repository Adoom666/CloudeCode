"""Cloudflare API integration for DNS management."""

from typing import Optional
import structlog
from CloudFlare import CloudFlare
from CloudFlare.exceptions import CloudFlareAPIError

from src.config import settings

logger = structlog.get_logger()


class CloudflareAPIError(Exception):
    """Exception raised for Cloudflare API errors."""
    pass


class CloudflareAPI:
    """Manages Cloudflare DNS records for tunnel subdomains."""

    def __init__(self):
        """Initialize Cloudflare API client."""
        self.api_token = settings.cloudflare_api_token
        self.zone_id = settings.cloudflare_zone_id
        self.domain = settings.cloudflare_domain
        self.tunnel_id = settings.cloudflare_tunnel_id

        if not self.api_token or not self.zone_id:
            logger.warning("cloudflare_credentials_not_configured")
            self.cf = None
        else:
            try:
                self.cf = CloudFlare(token=self.api_token)
                logger.info("cloudflare_api_initialized")
            except Exception as e:
                logger.error("cloudflare_api_initialization_failed", error=str(e))
                self.cf = None

    def is_configured(self) -> bool:
        """
        Check if Cloudflare API is properly configured.

        Returns:
            True if API is configured and ready
        """
        return self.cf is not None

    async def create_cname_for_port(self, port: int) -> Optional[str]:
        """
        Create a CNAME record for a specific port.

        Args:
            port: Port number to create subdomain for

        Returns:
            Full subdomain URL or None if failed

        Example:
            For port 3000: creates 3000.claude.mydomain.com -> <tunnel_id>.cfargotunnel.com
        """
        if not self.is_configured():
            logger.warning("cloudflare_not_configured_skipping_cname")
            return None

        if not self.tunnel_id:
            logger.error("tunnel_id_not_set_cannot_create_cname", port=port)
            return None

        subdomain = f"{port}.{self.domain}"
        tunnel_target = f"{self.tunnel_id}.cfargotunnel.com"

        try:
            # Check if CNAME already exists
            existing_record = await self._get_dns_record(subdomain)

            if existing_record:
                logger.info(
                    "cname_already_exists",
                    subdomain=subdomain,
                    record_id=existing_record['id']
                )
                return f"https://{subdomain}"

            # Create new CNAME record
            dns_record = {
                'name': subdomain,
                'type': 'CNAME',
                'content': tunnel_target,
                'ttl': 1,  # Auto TTL
                'proxied': True  # Enable Cloudflare proxy
            }

            logger.info("creating_cname", subdomain=subdomain, target=tunnel_target)

            result = self.cf.zones.dns_records.post(self.zone_id, data=dns_record)

            logger.info(
                "cname_created_successfully",
                subdomain=subdomain,
                record_id=result['id']
            )

            return f"https://{subdomain}"

        except CloudFlareAPIError as e:
            logger.error(
                "cloudflare_api_error",
                subdomain=subdomain,
                error=str(e),
                code=e.code if hasattr(e, 'code') else None
            )
            raise CloudflareAPIError(f"Failed to create CNAME: {str(e)}") from e
        except Exception as e:
            logger.error("cname_creation_failed", subdomain=subdomain, error=str(e))
            raise CloudflareAPIError(f"Failed to create CNAME: {str(e)}") from e

    async def _get_dns_record(self, subdomain: str) -> Optional[dict]:
        """
        Get existing DNS record for subdomain.

        Args:
            subdomain: Full subdomain to lookup

        Returns:
            DNS record dict or None if not found
        """
        if not self.is_configured():
            return None

        try:
            records = self.cf.zones.dns_records.get(
                self.zone_id,
                params={'name': subdomain, 'type': 'CNAME'}
            )

            if records:
                return records[0]

            return None

        except CloudFlareAPIError as e:
            if hasattr(e, 'code') and e.code == 1004:  # Record not found
                return None
            logger.error("dns_lookup_error", subdomain=subdomain, error=str(e))
            return None
        except Exception as e:
            logger.error("dns_lookup_failed", subdomain=subdomain, error=str(e))
            return None

    async def create_root_cname(self) -> Optional[str]:
        """
        Create the root CNAME record for the main domain.

        Returns:
            Full domain URL or None if failed

        Example:
            Creates claude.mydomain.com-> <tunnel_id>.cfargotunnel.com
        """
        if not self.is_configured():
            logger.warning("cloudflare_not_configured_skipping_root_cname")
            return None

        if not self.tunnel_id:
            logger.error("tunnel_id_not_set_cannot_create_root_cname")
            return None

        tunnel_target = f"{self.tunnel_id}.cfargotunnel.com"

        try:
            # Check if CNAME already exists
            existing_record = await self._get_dns_record(self.domain)

            if existing_record:
                logger.info(
                    "root_cname_already_exists",
                    domain=self.domain,
                    record_id=existing_record['id']
                )
                return f"https://{self.domain}"

            # Create new CNAME record
            dns_record = {
                'name': self.domain,
                'type': 'CNAME',
                'content': tunnel_target,
                'ttl': 1,  # Auto TTL
                'proxied': True  # Enable Cloudflare proxy
            }

            logger.info("creating_root_cname", domain=self.domain, target=tunnel_target)

            result = self.cf.zones.dns_records.post(self.zone_id, data=dns_record)

            logger.info(
                "root_cname_created_successfully",
                domain=self.domain,
                record_id=result['id']
            )

            return f"https://{self.domain}"

        except CloudFlareAPIError as e:
            logger.error(
                "cloudflare_api_error_root_cname",
                domain=self.domain,
                error=str(e),
                code=e.code if hasattr(e, 'code') else None
            )
            return None
        except Exception as e:
            logger.error("root_cname_creation_failed", domain=self.domain, error=str(e))
            return None

    async def delete_cname_for_port(self, port: int) -> bool:
        """
        Delete CNAME record for a specific port.

        Args:
            port: Port number

        Returns:
            True if deleted successfully
        """
        if not self.is_configured():
            logger.warning("cloudflare_not_configured_skipping_delete")
            return False

        subdomain = f"{port}.{self.domain}"

        try:
            # Find the record
            record = await self._get_dns_record(subdomain)

            if not record:
                logger.warning("cname_not_found_for_deletion", subdomain=subdomain)
                return True  # Already doesn't exist

            # Delete it
            logger.info("deleting_cname", subdomain=subdomain, record_id=record['id'])

            self.cf.zones.dns_records.delete(self.zone_id, record['id'])

            logger.info("cname_deleted_successfully", subdomain=subdomain)
            return True

        except CloudFlareAPIError as e:
            logger.error("cloudflare_api_delete_error", subdomain=subdomain, error=str(e))
            return False
        except Exception as e:
            logger.error("cname_deletion_failed", subdomain=subdomain, error=str(e))
            return False

    async def list_tunnel_cnames(self) -> list[str]:
        """
        List all CNAME records pointing to our tunnel.

        Returns:
            List of subdomains
        """
        if not self.is_configured():
            return []

        tunnel_target = f"{self.tunnel_id}.cfargotunnel.com"

        try:
            records = self.cf.zones.dns_records.get(
                self.zone_id,
                params={'type': 'CNAME'}
            )

            tunnel_records = [
                record['name']
                for record in records
                if record['content'] == tunnel_target
            ]

            logger.info("listed_tunnel_cnames", count=len(tunnel_records))
            return tunnel_records

        except Exception as e:
            logger.error("list_cnames_failed", error=str(e))
            return []
