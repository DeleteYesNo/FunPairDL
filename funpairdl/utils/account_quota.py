"""Query account quota/bandwidth for premium hosting services."""
from __future__ import annotations

import base64
import logging

import aiohttp

logger = logging.getLogger("funpairdl.utils.account_quota")

TIMEOUT = aiohttp.ClientTimeout(total=15)


async def query_mega_quota(sid: str) -> dict | None:
    """Query MEGA account transfer and storage quota.

    Returns: {storage_used, storage_total, transfer_used, transfer_total, tier}
    """
    if not sid:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://g.api.mega.co.nz/cs?sid={sid}",
                json=[{"a": "uq", "xfer": 1, "strg": 1}],
                timeout=TIMEOUT,
            ) as resp:
                data = await resp.json(content_type=None)

        if isinstance(data, list):
            data = data[0]
        if isinstance(data, int):
            logger.warning("MEGA quota query error: %d", data)
            return None

        mxfer = data.get("mxfer", 0)
        result = {
            "storage_used": data.get("cstrg", 0),
            "storage_total": data.get("mstrg", 0),
            "transfer_used": data.get("caxfer", 0) or data.get("csxfer", 0),
            "transfer_total": mxfer,
        }
        # uq response doesn't include utype; infer tier from max transfer quota
        if mxfer > 16 * 1024**4:
            result["tier"] = "Pro III"
        elif mxfer > 8 * 1024**4:
            result["tier"] = "Pro II"
        elif mxfer > 1 * 1024**4:
            result["tier"] = "Pro I"
        elif mxfer > 100 * 1024**3:
            result["tier"] = "Lite"
        else:
            result["tier"] = "Free"

        logger.info("MEGA quota: %s, transfer %d/%d",
                     result["tier"], result["transfer_used"], result["transfer_total"])
        return result
    except Exception as e:
        logger.warning("MEGA quota query failed: %s", e)
        return None


async def query_pixeldrain_quota(api_key: str) -> dict | None:
    """Query Pixeldrain account info.

    Returns: {bandwidth_used, bandwidth_total, tier}
    """
    if not api_key:
        return None
    try:
        token = base64.b64encode(f":{api_key}".encode()).decode()
        headers = {"Authorization": f"Basic {token}"}

        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://pixeldrain.com/api/user",
                headers=headers,
                timeout=TIMEOUT,
            ) as resp:
                if resp.status == 401:
                    logger.warning("Pixeldrain API key invalid")
                    return None
                data = await resp.json(content_type=None)

        subscription = data.get("subscription", {}) or {}
        plan = subscription.get("name", "Free")

        result = {
            "bandwidth_used": data.get("monthly_transfer_used", 0),
            "bandwidth_total": subscription.get("monthly_transfer_cap", 0),
            "tier": plan,
        }
        logger.info("Pixeldrain: %s, bandwidth %d/%d",
                     result["tier"], result["bandwidth_used"], result["bandwidth_total"])
        return result
    except Exception as e:
        logger.warning("Pixeldrain quota query failed: %s", e)
        return None


async def query_gofile_quota(token: str) -> dict | None:
    """Query GoFile account info.

    Returns: {tier, files_count, total_size}
    """
    if not token:
        return None
    try:
        headers = {"Authorization": f"Bearer {token}"}

        async with aiohttp.ClientSession() as session:
            # First get account ID
            async with session.get(
                f"https://api.gofile.io/accounts/getid",
                headers=headers,
                timeout=TIMEOUT,
            ) as resp:
                id_data = await resp.json(content_type=None)

            account_id = id_data.get("data", {}).get("id", "")
            if not account_id:
                logger.warning("GoFile: could not get account ID")
                return None

            # Get account details
            async with session.get(
                f"https://api.gofile.io/accounts/{account_id}",
                headers=headers,
                timeout=TIMEOUT,
            ) as resp:
                data = await resp.json(content_type=None)

        if data.get("status") != "ok":
            logger.warning("GoFile account query error: %s", data.get("status"))
            return None

        acct = data.get("data", {})
        tier = acct.get("tier", "standard")
        tier_display = "Premium" if tier != "standard" else "Free"

        result = {
            "tier": tier_display,
            "files_count": acct.get("filesCount", 0),
            "total_size": acct.get("totalSize", 0),
        }
        logger.info("GoFile: %s, %d files, %d bytes",
                     result["tier"], result["files_count"], result["total_size"])
        return result
    except Exception as e:
        logger.warning("GoFile quota query failed: %s", e)
        return None
