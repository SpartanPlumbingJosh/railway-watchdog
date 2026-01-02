"""
Railway Log Watchdog Service
Monitors all Railway services for errors and alerts to #war-room
"""

import os
import asyncio
import httpx
from datetime import datetime, timezone
from collections import defaultdict
import hashlib
from fastapi import FastAPI
from contextlib import asynccontextmanager

# Configuration
RAILWAY_TOKEN = os.environ.get("RAILWAY_API_TOKEN")
PROJECT_ID = os.environ.get("RAILWAY_PROJECT_ID", "e785854e-d4d6-4975-a025-812b63fe8961")
JUGGERNAUT_URL = os.environ.get("JUGGERNAUT_URL", "https://juggernaut-v3-production.up.railway.app")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL_SECONDS", "60"))
RAILWAY_API = "https://backboard.railway.app/graphql/v2"

# Track seen errors to avoid duplicates
seen_errors = set()
error_counts = defaultdict(int)
last_check = None
watchdog_running = False

app = FastAPI(title="Railway Log Watchdog")


async def graphql_query(client: httpx.AsyncClient, query: str, variables: dict = None):
    """Execute a GraphQL query against Railway API"""
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    
    response = await client.post(
        RAILWAY_API,
        json=payload,
        headers={
            "Authorization": f"Bearer {RAILWAY_TOKEN}",
            "Content-Type": "application/json"
        }
    )
    response.raise_for_status()
    return response.json()


async def get_services(client: httpx.AsyncClient):
    """Get all services and their latest deployment IDs"""
    query = """
    query GetServices($projectId: String!) {
        project(id: $projectId) {
            name
            services {
                edges {
                    node {
                        id
                        name
                        deployments(first: 1) {
                            edges {
                                node {
                                    id
                                    status
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    """
    result = await graphql_query(client, query, {"projectId": PROJECT_ID})
    
    services = []
    if result.get("data", {}).get("project"):
        for edge in result["data"]["project"]["services"]["edges"]:
            service = edge["node"]
            deployments = service.get("deployments", {}).get("edges", [])
            if deployments:
                deployment = deployments[0]["node"]
                services.append({
                    "id": service["id"],
                    "name": service["name"],
                    "deployment_id": deployment["id"],
                    "status": deployment["status"]
                })
    return services


async def get_deployment_logs(client: httpx.AsyncClient, deployment_id: str, limit: int = 100):
    """Get recent logs for a deployment"""
    query = """
    query GetLogs($deploymentId: String!, $limit: Int!) {
        deploymentLogs(deploymentId: $deploymentId, limit: $limit) {
            message
            timestamp
            severity
        }
    }
    """
    result = await graphql_query(client, query, {
        "deploymentId": deployment_id,
        "limit": limit
    })
    return result.get("data", {}).get("deploymentLogs", [])


async def post_to_war_room(client: httpx.AsyncClient, message: str, alert_type: str = "error"):
    """Post alert to #war-room via JUGGERNAUT"""
    try:
        # Use JUGGERNAUT's war room alert endpoint
        response = await client.post(
            f"{JUGGERNAUT_URL}/api/war-room/alert",
            json={
                "bot": "juggernaut",
                "alert_type": alert_type,
                "message": message
            },
            timeout=10.0
        )
        if response.status_code != 200:
            print(f"[Watchdog] War room post failed: {response.status_code}")
    except Exception as e:
        print(f"[Watchdog] Failed to post to war room: {e}")


def error_hash(service_name: str, message: str) -> str:
    """Create a hash for deduplication - ignores timestamps in messages"""
    # Remove common variable parts (timestamps, IDs, etc.)
    normalized = message
    for char in "0123456789":
        normalized = normalized.replace(char, "#")
    
    key = f"{service_name}:{normalized}"
    return hashlib.md5(key.encode()).hexdigest()[:16]


async def check_all_services():
    """Main check loop - scan all services for errors"""
    global last_check, seen_errors, error_counts
    
    async with httpx.AsyncClient() as client:
        try:
            services = await get_services(client)
            new_errors = []
            crashed_services = []
            
            for service in services:
                # Skip self-monitoring
                if service["name"].lower() == "railway-watchdog":
                    continue
                
                # Check for crashed deployments
                if service["status"] == "CRASHED":
                    crashed_services.append(service["name"])
                
                # Get logs and filter for errors
                try:
                    logs = await get_deployment_logs(client, service["deployment_id"], limit=50)
                    
                    for log in logs:
                        if log.get("severity") == "error":
                            err_hash = error_hash(service["name"], log["message"])
                            
                            # Only alert on new errors
                            if err_hash not in seen_errors:
                                seen_errors.add(err_hash)
                                error_counts[service["name"]] += 1
                                new_errors.append({
                                    "service": service["name"],
                                    "message": log["message"][:500],  # Truncate long messages
                                    "timestamp": log["timestamp"]
                                })
                                
                except Exception as e:
                    print(f"[Watchdog] Failed to get logs for {service['name']}: {e}")
            
            # Post alerts for crashed services
            for service_name in crashed_services:
                await post_to_war_room(
                    client,
                    f"🔴 **{service_name}** is CRASHED and needs attention!",
                    "error"
                )
            
            # Post alerts for new errors (batch them if many)
            if new_errors:
                if len(new_errors) <= 3:
                    # Post individual alerts
                    for err in new_errors:
                        await post_to_war_room(
                            client,
                            f"⚠️ **{err['service']}** error:\n```{err['message']}```",
                            "warning"
                        )
                else:
                    # Summarize if too many
                    summary = f"⚠️ **{len(new_errors)} new errors detected:**\n"
                    by_service = defaultdict(list)
                    for err in new_errors:
                        by_service[err["service"]].append(err["message"][:100])
                    
                    for svc, msgs in by_service.items():
                        summary += f"\n• **{svc}**: {len(msgs)} errors"
                    
                    await post_to_war_room(client, summary, "warning")
            
            last_check = datetime.now(timezone.utc)
            print(f"[Watchdog] Check complete: {len(services)} services, {len(new_errors)} new errors, {len(crashed_services)} crashed")
            
        except Exception as e:
            print(f"[Watchdog] Check failed: {e}")


async def watchdog_loop():
    """Continuous monitoring loop"""
    global watchdog_running
    watchdog_running = True
    print(f"[Watchdog] Starting - checking every {CHECK_INTERVAL}s")
    
    while watchdog_running:
        await check_all_services()
        await asyncio.sleep(CHECK_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start watchdog on startup, stop on shutdown"""
    task = asyncio.create_task(watchdog_loop())
    yield
    global watchdog_running
    watchdog_running = False
    task.cancel()
    print("[Watchdog] Stopped")


app = FastAPI(title="Railway Log Watchdog", lifespan=lifespan)


@app.get("/health")
async def health():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "watchdog_running": watchdog_running,
        "last_check": last_check.isoformat() if last_check else None,
        "errors_tracked": len(seen_errors),
        "error_counts_by_service": dict(error_counts)
    }


@app.get("/status")
async def status():
    """Detailed status endpoint"""
    async with httpx.AsyncClient() as client:
        services = await get_services(client)
    
    return {
        "project_id": PROJECT_ID,
        "services_monitored": len(services),
        "services": [
            {
                "name": s["name"],
                "status": s["status"],
                "errors_seen": error_counts.get(s["name"], 0)
            }
            for s in services
        ],
        "check_interval_seconds": CHECK_INTERVAL,
        "last_check": last_check.isoformat() if last_check else None
    }


@app.post("/check-now")
async def check_now():
    """Trigger an immediate check"""
    await check_all_services()
    return {"status": "check completed", "last_check": last_check.isoformat()}


@app.post("/clear-seen")
async def clear_seen():
    """Clear seen errors (will re-alert on existing errors)"""
    global seen_errors, error_counts
    count = len(seen_errors)
    seen_errors = set()
    error_counts = defaultdict(int)
    return {"status": "cleared", "errors_cleared": count}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
