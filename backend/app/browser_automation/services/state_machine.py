import os
import httpx
import logging
from dotenv import load_dotenv
from typing import Dict

load_dotenv()
logger = logging.getLogger(__name__)

async def transition_status(application_id: str, new_status: str, metadata: Dict = {}) -> bool:
    m1_api_base_url = os.getenv("M1_API_BASE_URL", "http://localhost:8000/api")
    url = f"{m1_api_base_url}/applications/{application_id}/status"
    
    payload = {"status": new_status, "metadata": metadata}
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.patch(url, json=payload, timeout=10)
            
            if response.status_code in [200, 204]:
                logger.info(f"Application {application_id} transitioned to {new_status}")
                return True
            elif response.status_code == 422:
                logger.warning(f"Invalid state transition attempted for {application_id}: {response.text}")
                return False
            else:
                logger.error(f"Failed to transition status for {application_id} to {new_status}: "
                             f"Status Code: {response.status_code}, Response: {response.text}")
                return False
    except httpx.RequestError as e:
        logger.error(f"Connection error during status transition for {application_id}: {e}")
        return False
